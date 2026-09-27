# -*- coding: utf-8 -*-
"""
PP-OCRv6 识别后端（OCR 模型替换阶段）

把 PP-OCRv6_medium_rec 包装成与 OCREngine **相同**的面向调用方契约：

    iter_readings(image, preprocess=True) -> Iterator[(label, text)]
    recognize(image, preprocess=True, return_details=False) -> str
    shutdown() -> None

上游（OCRWorker / RoomCodeRecognizer / 诊断）只通过这三个方法接触引擎，
因此本模块是**唯一**需要知道 PP-OCRv6 存在的地方：模型定位、GPU 设备、
paddle 运行时加载、推理、输出取文本、释放，全部收在这里。识别规则
（归一化/纠正/格式校验/仲裁/时序裁决）一行都不在这里。

为什么是「同样的契约」而不是新接口
----------------------------------
RoomCodeRecognizer 只在两处接触引擎：
    ocr/room_code.py:_iter_reading_texts  -> iter_readings(image, preprocess=True)
    ocr/room_code.py（单读法回退路径）      -> recognize(image, preprocess=True)
再加 worker 退出时的 engine.shutdown()。沿用这三个方法，替换就是**换实现**，
而不是改架构：worker 调度、TemporalResolver、纠正与仲裁完全不需要知道
换过引擎。

PSM 的处理
----------
PSM 是 Tesseract 特有的概念，PP-OCRv6 **不伪造** PSM 6/7/11 语义。
iter_readings 的标签只用于诊断，本模块统一给一个中性标签 READING_LABEL，
它不参与任何判定（见 room_code.py:_note_reading：标签只进诊断记录）。
本模块每次调用**只产出一个读法**——PP-OCRv6 是单遍识别模型，
没有「换一种切分再读一遍」这回事，硬凑多个读法就是编造。

GPU 约束（无回退）
------------------
只允许 GPU：
  - paddle 必须是 CUDA 编译版，且至少有一块可见 GPU，否则构造时**显式失败**；
  - 不捕获异常回落到 CPU，不回落到 Tesseract，不做运行期换模型。
初始化/推理失败一律以 PPOCREngineError 抛出，绝不吞成空文本——
空文本在上游是「这一帧没读到东西」，与「引擎坏了」是两回事，
吞掉会让设备故障表现成识别率下降。

线程归属
--------
模型句柄**只在创建它的线程里使用**（与 engine.py 的常驻 Tesseract 同一条
约束）。OCRWorker 在工作线程内构造本引擎、并在同一线程里推理，
停机时也在同一线程释放，因此生产路径天然满足；跨线程使用会显式报错。
"""

import logging
import os
import site
import sys
import threading
from pathlib import Path
from typing import Any, Iterator, List, Optional, Tuple

from .engine import (
    FROZEN_OCR_MISSING_MESSAGE,
    OCREngineError,
    _bundled_dirs,
    _is_usable,
    is_frozen,
    ocr_missing_message,
)

logger = logging.getLogger("DouyinLowLatencyViewer.ocr.ppocr_engine")

__all__ = [
    "PPOCREngine",
    "PPOCREngineError",
    "PPOCRModelNotFoundError",
    "PPOCRModelLocator",
    "MODEL_NAME",
    "READING_LABEL",
]

#: PaddleX 官方模型名。必须与 model_dir 里的 inference.yml 一致。
MODEL_NAME = "PP-OCRv6_medium_rec"

#: iter_readings 产出的标签。中性、非 PSM，只用于诊断列。
READING_LABEL = "ppocr"

#: 模型目录名（随包目录与开发目录都用这个叶子名）。
_MODEL_DIR_NAME = "PP-OCRv6_medium_rec_infer"

#: runtime/ 下的子目录名。打包阶段需与 douyin_viewer.spec 保持一致，
#: 与 engine.py 的 _TESSERACT_SUBDIR 是同一个模式。
_PPOCR_SUBDIR = "ppocr"

#: 部署覆盖用的环境变量名（与 DLV_TESSERACT_PATH 同一模式）。
_ENV_MODEL_DIR = "DLV_PPOCR_MODEL_DIR"

#: 开发环境的模型位置。仅非冻结环境使用，见 find_model_dir()。
#: 打包阶段会把模型放进 runtime/ppocr/，届时这一条不再被走到。
_DEV_MODEL_DIR = r"D:\new_model\PP-OCRv6_medium_rec_infer"

#: 推理批大小。识别是逐帧的，恒为 1；不做批处理。
BATCH_SIZE = 1

#: 推理设备。写死 GPU：本后端没有 CPU 路径（见模块说明）。
_DEVICE = "gpu"

#: paddle 静态图推理引擎。
_ENGINE = "paddle_static"


class PPOCREngineError(OCREngineError):
    """PP-OCRv6 后端的显式错误。

    继承 OCREngineError 是**刻意**的：OCRWorker 既有的错误处理分支
    （except OCREngineError -> 计数 + error_occurred + 诊断）与
    run() 初始化阶段的错误/回收路径都不需要任何改动，就能原样接管
    本后端的失败——「保持既有 worker 错误与拆除行为不变」是靠继承拿到的，
    而不是靠在 worker 里加分支。
    """


class PPOCRModelNotFoundError(PPOCREngineError):
    """模型目录无法定位（缺失/损坏）。"""


class PPOCRModelLocator:
    """PP-OCRv6 模型目录的唯一定位边界（与 TesseractLocator 同一套语义）。

    查找顺序：
        1. 环境变量 DLV_PPOCR_MODEL_DIR（部署覆盖）
        2. 随包目录：<base>/runtime/ppocr/PP-OCRv6_medium_rec_infer
           （<base> 在冻结环境是 _MEIPASS / exe 目录，源码环境是仓库根）
        3. 仅非冻结的开发环境：_DEV_MODEL_DIR
    冻结环境**不做**开发路径回退：客户机上没有 D:\\new_model，
    回退只会把「随包文件缺失」掩盖成含糊的找不到模型。
    """

    @staticmethod
    def _bundled_candidates() -> List[str]:
        return [
            os.path.join(base, "runtime", _PPOCR_SUBDIR, _MODEL_DIR_NAME)
            for base in _bundled_dirs()
        ]

    @staticmethod
    def is_usable_model_dir(path: str) -> bool:
        """存在且是一份完整可加载的推理模型。

        只认 inference.pdiparams 是否在位且非空 —— 与 engine.py 的 _is_usable
        同一判据：0 字节的占位文件不能算「找到」。
        """
        if not path or not os.path.isdir(path):
            return False
        return _is_usable(os.path.join(path, "inference.pdiparams"))

    @staticmethod
    def find_model_dir(explicit: Optional[str] = None) -> str:
        """定位 PP-OCRv6 模型目录，返回绝对路径。

        异常：
            PPOCRModelNotFoundError: 候选路径都不可用。
        """
        override = (explicit or os.environ.get(_ENV_MODEL_DIR) or "").strip()
        if override:
            if PPOCRModelLocator.is_usable_model_dir(override):
                return override
            raise PPOCRModelNotFoundError(
                ocr_missing_message(
                    "DLV_PPOCR_MODEL_DIR 指向的模型目录不可用或缺少 "
                    f"inference.pdiparams: {override}"
                )
            )

        for candidate in PPOCRModelLocator._bundled_candidates():
            if PPOCRModelLocator.is_usable_model_dir(candidate):
                return candidate

        if is_frozen():
            # 走到这里说明随包模型缺失：这是发布包的缺陷，不是用户环境问题。
            raise PPOCRModelNotFoundError(FROZEN_OCR_MISSING_MESSAGE)

        if PPOCRModelLocator.is_usable_model_dir(_DEV_MODEL_DIR):
            logger.info("[PPOCR] 使用开发环境模型目录: %s", _DEV_MODEL_DIR)
            return _DEV_MODEL_DIR

        raise PPOCRModelNotFoundError(
            "PP-OCRv6 模型未找到。请把 %s 放到 runtime/%s/ 下，"
            "或用环境变量 %s 指定模型目录。"
            % (_MODEL_DIR_NAME, _PPOCR_SUBDIR, _ENV_MODEL_DIR)
        )


def _ensure_cuda_dlls() -> List[str]:
    """把 pip 安装的 NVIDIA CUDA 运行库目录加入 DLL 搜索路径。

    背景（与 Tesseract 随包运行时同理，见 engine.py 的 os.add_dll_directory）：
    paddlepaddle-gpu 依赖的 cuBLAS/cuDNN 等 DLL 由 nvidia-* wheel 提供，
    它们位于 site-packages/nvidia/cu13/bin 下，**不在系统 PATH 上**。
    不先把这个目录暴露出来，paddle 在加载 CUDA 算子时会直接失败。
    因此这是「模型/运行时加载」的一部分，属于本后端的职责范围。

    返回实际加入的目录列表（诊断用）。
    """
    added: List[str] = []
    if os.name != "nt":
        return added

    candidates = set()
    try:
        for root in site.getsitepackages():
            base = Path(root)
            for p in base.glob("nvidia/cu13/bin/*"):
                if p.is_dir():
                    candidates.add(p)
            exact = base / "nvidia" / "cu13" / "bin" / "x86_64"
            if exact.is_dir():
                candidates.add(exact)
    except Exception:  # noqa: BLE001 - 定位失败只影响能否加载，交给 paddle 自己报错
        logger.debug("[PPOCR] CUDA DLL 目录扫描失败", exc_info=True)

    for path in sorted(candidates):
        s = str(path)
        if s not in os.environ.get("PATH", "").split(os.pathsep):
            os.environ["PATH"] = s + os.pathsep + os.environ.get("PATH", "")
        try:
            os.add_dll_directory(s)
        except Exception:  # noqa: BLE001 - 老平台没有该 API，PATH 已足够
            pass
        added.append(s)
    return added


def _result_text(item: Any) -> str:
    """从 PaddleX 结果对象里取识别文本（不同版本字段名不同，尽力而为）。"""
    value = None
    if hasattr(item, "get"):
        try:
            value = item.get("rec_text")
        except Exception:  # noqa: BLE001
            value = None
    if value is None:
        value = getattr(item, "rec_text", None)
    if value is None and hasattr(item, "json"):
        data = getattr(item, "json", None)
        if isinstance(data, dict):
            res = data.get("res", data)
            if isinstance(res, dict):
                value = res.get("rec_text")
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return "" if value is None else str(value).strip()


def _result_score(item: Any) -> Optional[float]:
    """取识别置信度（诊断用；拿不到就返回 None，不编造）。"""
    for getter in (lambda o: o.get("rec_score") if hasattr(o, "get") else None,
                   lambda o: getattr(o, "rec_score", None)):
        try:
            value = getter(item)
        except Exception:  # noqa: BLE001
            continue
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    return None


class PPOCREngine:
    """PP-OCRv6_medium_rec 的 OCR 引擎封装（GPU，无回退）。

    生命周期与归属
    --------------
    构造（在工作线程内）时就完成模型加载，随后本会话常驻，直到 shutdown()。
    为什么不做成「第一次推理时才加载」：初始化失败必须是**一次性**的显式错误。
    OCRWorker.run() 在进入识别循环前有一个 try 包住引擎/识别器构造，
    失败就报错一次并结束线程（避免每 300ms 重复报错）；把加载放在构造里，
    这条既有路径原样生效。若改成首次推理时加载，同样的失败会退化成
    每个识别周期报一次错。
    引擎对象由 OCRWorker 持有：停止识别即释放，重新开始是新对象新模型，
    不存在跨会话常驻的全局模型。
    """

    def __init__(self, model_dir: Optional[str] = None):
        """
        参数：
            model_dir: 显式模型目录（测试/诊断用）。None（默认）走
                       PPOCRModelLocator 的定位顺序。

        异常：
            PPOCRModelNotFoundError: 模型目录不可用。
            PPOCREngineError: 运行时不满足 GPU 约束，或模型加载失败。
        """
        self._model_dir = PPOCRModelLocator.find_model_dir(model_dir)
        self._owner_thread = threading.get_ident()
        self._closed = False

        logger.info("[PPOCR] model=%s dir=%s device=%s batch=%d",
                    MODEL_NAME, self._model_dir, _DEVICE, BATCH_SIZE)

        # 这些属性在 _load_model() 里赋值；先置 None，便于 shutdown 幂等。
        self._model = None
        self._paddle = None
        self._init_ms: Optional[float] = None

        self._load_model()

    # ------------------------------------------------------------------
    # 模型加载（GPU 强制，无回退）
    # ------------------------------------------------------------------
    def _load_model(self) -> None:
        """加载 GPU 运行时与模型。任何失败都以 PPOCREngineError 抛出。"""
        import time

        dll_dirs = _ensure_cuda_dlls()
        if dll_dirs:
            logger.debug("[PPOCR] CUDA DLL 目录: %s", dll_dirs)

        try:
            import paddle
        except Exception as e:  # noqa: BLE001 - 缺运行时是显式失败，不是回退信号
            raise PPOCREngineError("PP-OCRv6 需要 paddle 运行时，导入失败: %s" % e) from e

        from paddlex import create_model

        self._paddle = paddle
        self._assert_gpu_available(paddle)

        t0 = time.perf_counter()
        try:
            self._model = create_model(
                model_name=MODEL_NAME,
                model_dir=self._model_dir,
                device=_DEVICE,
                engine=_ENGINE,
                batch_size=BATCH_SIZE,
            )
        except Exception as e:  # noqa: BLE001 - 显式失败，不回落 CPU / Tesseract
            raise PPOCREngineError(
                "PP-OCRv6 模型加载失败 (dir=%s, device=%s): %s"
                % (self._model_dir, _DEVICE, e)
            ) from e
        self._init_ms = (time.perf_counter() - t0) * 1000.0
        logger.info("[PPOCR] 模型已就绪: init=%.0fms", self._init_ms)

    @staticmethod
    def _assert_gpu_available(paddle) -> None:
        """确认 GPU 可用；否则显式失败（绝不静默走 CPU）。"""
        try:
            compiled = bool(paddle.device.is_compiled_with_cuda())
        except Exception as e:  # noqa: BLE001
            raise PPOCREngineError("无法确认 paddle 是否为 CUDA 编译版: %s" % e) from e
        if not compiled:
            raise PPOCREngineError(
                "当前 paddle 不是 CUDA 编译版，PP-OCRv6 只支持 GPU（不提供 CPU 回退）。"
            )
        try:
            count = int(paddle.device.cuda.device_count())
        except Exception as e:  # noqa: BLE001
            raise PPOCREngineError("无法枚举 CUDA 设备: %s" % e) from e
        if count <= 0:
            raise PPOCREngineError(
                "未检测到可用的 NVIDIA GPU，PP-OCRv6 只支持 GPU（不提供 CPU 回退）。"
            )

    @property
    def model_dir(self) -> str:
        return self._model_dir

    @property
    def init_ms(self) -> Optional[float]:
        return self._init_ms

    @property
    def backend_status(self) -> dict:
        """后端状态快照（诊断用，不参与识别）。"""
        return {
            "model_name": MODEL_NAME,
            "model_dir": self._model_dir,
            "device": _DEVICE,
            "batch_size": BATCH_SIZE,
            "init_ms": self._init_ms,
            "closed": self._closed,
        }

    # ------------------------------------------------------------------
    # 输入契约（把非法输入挡在推理之前）
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_image(image) -> None:
        """校验输入符合生产契约：uint8 的 BGR (H, W, 3)。

        为什么必须显式校验：PaddleX 的 ``predict`` 收到 2-D 灰度输入时只抛
        ``IndexError: tuple index out of range``。那与本层真正要暴露的推理失败
        （缺 CUDA 库、权重损坏、显存不足）在错误信息上无法区分，会被误读成
        「模型坏了」。生产路径上的 ROI 是 ``stream/ffmpeg_reader.py`` 的
        ``uint8 (H, W, 3)`` 帧切片（``ocr/worker.py:_crop_roi``），所以这个
        契约与真实输入一致。

        只校验、不转换：不做灰度、不复制、不改 dtype、不加任何预处理
        （PP-OCRv6 的尺寸归一化由模型内部完成，见 _infer）。
        """
        if image is None or getattr(image, "size", 0) == 0:
            raise PPOCREngineError("输入图像为空")

        shape = getattr(image, "shape", None)
        dtype = getattr(image, "dtype", None)
        dtype_name = getattr(dtype, "name", None) or str(dtype)
        described = "shape=%s dtype=%s" % (
            tuple(shape) if shape is not None else None, dtype_name)

        if shape is None or len(shape) != 3 or shape[2] != 3:
            raise PPOCREngineError(
                "输入图像通道数不受支持：期望 uint8 BGR (H, W, 3)，实际 %s。"
                "请传入原始帧（BGR）的 ROI 切片；本后端不做灰度/通道自动转换。"
                % described
            )

        if dtype_name != "uint8":
            raise PPOCREngineError(
                "输入图像 dtype 不受支持：期望 uint8，实际 %s。"
                "请传入原始帧（BGR）的 ROI 切片；本后端不做 dtype 自动转换。"
                % described
            )

    def _check_usable(self) -> None:
        """确认本实例可用且是在属主线程里被调用。"""
        if self._closed:
            raise PPOCREngineError("PP-OCRv6 引擎已释放，不能继续使用")
        current = threading.get_ident()
        if current != self._owner_thread:
            # 模型句柄不跨线程使用（与 engine.py 的常驻 Tesseract 同一条约束）。
            raise PPOCREngineError(
                "PP-OCRv6 引擎只能在其属主线程内使用"
                "（owner=%s, current=%s）" % (self._owner_thread, current)
            )

    def _infer(self, image) -> Tuple[str, Optional[float]]:
        """跑一次识别，返回 (文本, 置信度)。

        输入是调用方给的 ROI 数组，**原样传入、不修改**：PP-OCRv6 的
        预处理（尺寸归一化等）由模型内部完成，本层不做灰度/二值化/缩放，
        也没有可省的格式转换。
        """
        try:
            results = list(self._model.predict(image, batch_size=BATCH_SIZE))
        except Exception as e:  # noqa: BLE001 - 推理失败显式上报，不吞成空文本
            raise PPOCREngineError("PP-OCRv6 推理失败: %s" % e) from e

        if not results:
            return "", None
        item = results[0]
        return _result_text(item), _result_score(item)

    def iter_readings(self, image, preprocess: bool = True) -> Iterator[Tuple[str, str]]:
        """产出本帧的读法 ``(label, text)``。

        PP-OCRv6 是单遍模型：一帧**只用一种方式读一次**，因此这里最多产出一个
        读法。label 是中性标签 READING_LABEL，仅供诊断列使用，不参与识别、
        提前停止、仲裁、纠正或确认（那些都由 RoomCodeRecognizer 用自己的
        规则决定，见 room_code.py）。

        空结果是「这一帧没读到文本」的空串，**不产出**读法——与既有
        iter_readings 的语义一致（只产出非空读法）。
        引擎错误则向上抛 PPOCREngineError，绝不降级成空文本。

        参数：
            image: 输入图像 (BGR 的 numpy.ndarray)，不会被修改
            preprocess: 保留该参数以匹配既有契约；PP-OCRv6 的预处理在模型内部，
                        本层实现与 OCREngine.recognize 一样不额外做预处理
        """
        self._check_usable()
        self._validate_image(image)

        text, _score = self._infer(image)
        if text:
            logger.debug("PPOCR 读法: '%s'", text)
            yield (READING_LABEL, text)

    def recognize(self, image, preprocess: bool = True,
                  return_details: bool = False):
        """单读法入口（返回本帧识别到的文本）。

        生产路径走 iter_readings()；本方法保留是为了维持与 OCREngine 相同的
        引擎契约（识别器在引擎缺少 iter_readings 时会退回调用它），
        行为与 iter_readings 的第一个读法一致。

        参数：
            image: 输入图像 (BGR 的 numpy.ndarray)
            preprocess: 保留参数，同上
            return_details: True 时返回字典（诊断用），False 返回字符串

        异常：
            PPOCREngineError: 输入为空或推理失败。
        """
        self._check_usable()
        self._validate_image(image)

        text, score = self._infer(image)
        if not return_details:
            return text
        return {
            "text": text,
            "confidence": score if score is not None else 0,
            # PP-OCRv6 没有 PSM 概念：如实留 None，不编造编号。
            "psm_used": None,
            "details": None,
        }

    def shutdown(self) -> None:
        """释放模型与显存（幂等；由 OCRWorker 在停机时于工作线程内调用）。

        OCR 会话结束语义就在这里：模型不再常驻、显存归还，
        下一次「开始识别」会构造全新实例重新加载（不存在跨会话的全局模型）。
        """
        if self._closed:
            return
        self._closed = True

        model, paddle = self._model, self._paddle
        self._model = None
        if model is not None:
            try:
                del model
            except Exception:  # noqa: BLE001 - 释放失败不影响线程退出
                pass
        try:
            import gc
            gc.collect()
        except Exception:  # noqa: BLE001
            pass
        if paddle is not None:
            try:
                # 把 paddle 的显存缓存归还驱动，避免停机后仍占着 GPU。
                paddle.device.cuda.empty_cache()
            except Exception:  # noqa: BLE001 - 不支持时忽略
                logger.debug("[PPOCR] empty_cache 不可用", exc_info=True)
        logger.info("[PPOCR] 模型已释放")
