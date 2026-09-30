# -*- coding: utf-8 -*-
"""ONNX Runtime（DirectML）识别引擎 —— 与 PPOCREngine / OCREngine 并列的第三个后端。

【为什么不新建抽象层】上游（OCRWorker / RoomCodeRecognizer / 诊断）只通过三个
方法接触引擎，这是 `ocr/ppocr_engine.py:7-9` 已经写明的既有契约：

    iter_readings(image, preprocess=True) -> Iterator[(label, text)]
    recognize(image, preprocess=True, return_details=False) -> str | dict
    shutdown() -> None

本类就实现这三个方法，**不引入基类 / Protocol / ABC**，也不扩大抽象层。
`ONNXEngineError` 继承 `OCREngineError`，因此 OCRWorker 既有的
`except OCREngineError`（worker.py:523）初始化失败与推理失败路径**原样生效**。
初始化/推理失败一律显式抛出，**绝不吞成空文本** —— 空文本在上游是
「这一帧没读到东西」，与「引擎坏了」是两回事（与 PPOCREngine 同一条原则）。

【与 PPOCREngine 的关键差异：session 必须等 ROI 才能建】
DirectML 上「一个 session 里第一个 shape 满速、其后引入的 shape 永久慢 3–6 倍」
（见 STEP1_REPORT.md 第 9 节）。因此本引擎：

    一个 ROI → 一个固定 OCR shape → 一个独占 session → 整个 ROI 生命周期复用

宽度只由 ROI 图像的**宽高比**决定，在 ROI 确认时算一次并**钉死**：
之后所有输入（含识别器的 1.5×/2.0× escalation 缩放）都 pad 到同一宽度，
**绝不**让第二个 shape 进入 session。

【为什么宽度取自「图像」而不是「ROI 矩形」】
`OCRWorker._crop_roi` 的越界兜底走 `roi.scale_to(frame_w, frame_h)`
（worker.py:629-634），而 `ROI.scale_to` 用的是**独立**的 sx/sy
（roi_manager.py:61-62）。当帧宽高比与 ROI 记录的 source_* 宽高比不同时，
裁剪出的图像宽高比就与 `roi.width/roi.height` 不一致 —— 从矩形算宽度会算错。
所以宽度一律由真正要送进网络的图像推导。

【不随包投放】本后端目前只在源码环境可用：`runtime/onnx/` 下的 `.onnx` 与
`inference.yml` 靠机器投放，打包（spec / 构建闸门 / 冻结自检）留给后续阶段。
"""

import gc
import logging
import os
import threading
import time
from typing import Iterator, Optional, Tuple

import numpy as np

from .engine import (
    FROZEN_OCR_MISSING_MESSAGE,
    OCREngineError,
    _bundled_dirs,
    _is_usable,
    is_frozen,
    ocr_missing_message,
)
from .onnx_preprocess import (
    CHARSET_FINAL_SIZE,
    CTCLabelDecodeMirror,
    OCRReisizeNormImgMirror,
    REC_IMAGE_SHAPE,
    load_charset,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ONNXEngine",
    "ONNXEngineError",
    "ONNXModelNotFoundError",
    "ONNXModelLocator",
    "MODEL_NAME",
    "READING_LABEL",
]


#: 与 inference.yml 的模型名一致（沿用 PPOCREngine 的命名习惯）。
MODEL_NAME = "PP-OCRv6_medium_rec"

#: iter_readings 产出的读法标签。
#: 刻意**不复用** "ppocr"：该值被 tools/ocr_dataset_generator/teacher.py:39 镜像，
#: 复用会让诊断列把 ONNX 后端记成 Paddle 后端。
READING_LABEL = "onnx"

#: runtime/ 下的子目录名（与 runtime/ppocr/ 同构）。
_ONNX_SUBDIR = "onnx"

#: 随包模型文件名。
_MODEL_FILENAME = "PP-OCRv6_medium_rec_fp32.onnx"

#: 字符集文件名（随 ONNX 产物一起投放，与 Paddle 侧完全解耦）。
#: 内容 = PP-OCRv6 inference.yml 的 PostProcess.character_dict，一字符一行。
#: **运行时只读这个文件，绝不读 Paddle 侧的 inference.yml。**
_CHARSET_FILENAME = "charset.txt"

#: 部署覆盖用的环境变量名（与 DLV_PPOCR_MODEL_DIR / DLV_TESSERACT_PATH 同一模式）。
_ENV_MODEL_PATH = "DLV_ONNX_MODEL_PATH"
_ENV_CHARSET = "DLV_ONNX_CHARSET_PATH"

#: 仅**非冻结**环境的开发兜底。冻结环境绝不回退到这里
#: （客户机上不存在这个路径，静默回退只会把「随包文件缺失」变成含糊的报错）。
#: 正常路径是 runtime/onnx/（随机器投放，见 .gitignore），这里只是源码环境的安全网。
_DEV_BASE_DIR = r"D:\TIikTok_Ocr\_onnx_export_lab"
_DEV_MODEL_PATH = os.path.join(_DEV_BASE_DIR, _MODEL_FILENAME)
_DEV_CHARSET = os.path.join(_DEV_BASE_DIR, _CHARSET_FILENAME)

#: 模型文件的字节下限。只用来挡「下载被截断但非空」这种明显损坏 ——
#: 精确的完整性校验由 scripts/verify_onnx_model.py 的 sha256 负责。
_MIN_MODEL_BYTES = 10 * 1024 * 1024

#: DirectML 为主路径；不可用时才回退 CPU。
_PROVIDER_DML = "DmlExecutionProvider"
_PROVIDER_CPU = "CPUExecutionProvider"

#: 建 session 后的 warmup 次数。DirectML 首次推理要编译 kernel，
#: 不预热就会把这一次的代价算到用户的第一帧上。
WARMUP_RUNS = 5

#: 与镜像 OCRReisizeNormImgMirror.max_imgW 保持一致。
_MAX_IMG_W = 3200


class ONNXEngineError(OCREngineError):
    """ONNX 识别引擎异常。

    继承 ``OCREngineError`` 是**刻意**的：OCRWorker 的初始化失败路径与
    推理失败路径都只 catch ``OCREngineError``（worker.py:523），
    继承之后这两条既有权路径无需任何改动即可接管本引擎的失败。
    """


class ONNXModelNotFoundError(ONNXEngineError):
    """ONNX 模型（或字典）未找到 / 不可用。"""


class ONNXModelLocator:
    """定位 ONNX 产物：模型文件 + 字符集文件（都在 runtime/onnx/ 下）。

    优先级（与 PPOCRModelLocator 同构）：
        ① 显式参数
        ② 环境变量（DLV_ONNX_MODEL_PATH / DLV_ONNX_CHARSET_PATH）
        ③ <base>/runtime/onnx/<file>，base 取自 engine._bundled_dirs()
           （冻结环境 = _MEIPASS / exe 目录；源码环境 = 仓库根）
        ④ **仅非冻结**环境的开发兜底

    **本类不引用 runtime/ppocr/ 或任何 Paddle 侧文件** —— ONNX 后端的运行时
    依赖只有 runtime/onnx/ 下的这两个文件。

    与 PPOCRModelLocator 的一处重要差别：那边是**目录**谓词
    （检查 inference.pdiparams），这边是**文件**谓词 —— ONNX 产物是单个文件。
    因此复用 engine._is_usable（存在且非空）而不是 is_usable_model_dir。
    """

    @staticmethod
    def is_usable_model_file(path: str, min_bytes: int = 0) -> bool:
        """存在、非空、且不小于 min_bytes。"""
        if not _is_usable(path):
            return False
        try:
            return os.path.getsize(path) >= int(min_bytes)
        except OSError:
            return False

    @staticmethod
    def _bundled_candidates(filename: str):
        return [
            os.path.join(base, "runtime", _ONNX_SUBDIR, filename)
            for base in _bundled_dirs()
        ]

    @classmethod
    def _resolve(
        cls, filename: str, env_name: str, explicit: Optional[str],
        dev_fallback: str, min_bytes: int, what: str,
    ) -> str:
        override = (explicit or os.environ.get(env_name) or "").strip()
        if override:
            if cls.is_usable_model_file(override, min_bytes):
                return override
            raise ONNXModelNotFoundError(
                ocr_missing_message(
                    "%s 指向的 %s 不可用或已损坏: %s" % (env_name, what, override)
                )
            )

        for candidate in cls._bundled_candidates(filename):
            if cls.is_usable_model_file(candidate, min_bytes):
                return candidate

        if is_frozen():
            # 走到这里说明随包产物缺失：这是发布包的缺陷，不是用户环境问题。
            raise ONNXModelNotFoundError(FROZEN_OCR_MISSING_MESSAGE)

        if cls.is_usable_model_file(dev_fallback, min_bytes):
            # 刻意用 WARNING 而不是 INFO：这是一条**仓库外**路径，
            # 冻结构建里永远不会走这里。若它被静默命中，就意味着
            # runtime/onnx/ 没有投放，而开发环境正在用另一个文件跑模型
            # —— 那会让「bench 过的权重」与「实际用的权重」悄悄分叉。
            logger.warning(
                "[ONNX] runtime/%s/ 下没有%s，回退到开发环境路径: %s "
                "（这不是随包路径，请把产物投放好）",
                _ONNX_SUBDIR, what, dev_fallback,
            )
            return dev_fallback

        raise ONNXModelNotFoundError(
            "ONNX 后端的%s未找到。请把 %s 放到 runtime/%s/ 下，"
            "或用环境变量 %s 指定。"
            % (what, filename, _ONNX_SUBDIR, env_name)
        )

    @classmethod
    def find_model_path(cls, explicit: Optional[str] = None) -> str:
        return cls._resolve(
            _MODEL_FILENAME, _ENV_MODEL_PATH, explicit, _DEV_MODEL_PATH,
            _MIN_MODEL_BYTES, "模型文件",
        )

    @classmethod
    def find_charset_path(cls, explicit: Optional[str] = None) -> str:
        return cls._resolve(
            _CHARSET_FILENAME, _ENV_CHARSET, explicit, _DEV_CHARSET,
            0, "字符集文件",
        )


class ONNXEngine:
    """ONNX Runtime + DirectML 后端（PP-OCRv6_medium_rec, FP32）。

    线程归属：模型/session 句柄只在创建它的线程内使用（与 PPOCREngine、
    常驻 Tesseract 同一条约束）。构造必须发生在 OCRWorker 的工作线程里。
    """

    def __init__(
        self,
        model_path: Optional[str] = None,
        charset_path: Optional[str] = None,
    ):
        t0 = time.perf_counter()

        self._model_path = ONNXModelLocator.find_model_path(model_path)
        self._charset_path = ONNXModelLocator.find_charset_path(charset_path)
        self._owner_thread = threading.get_ident()
        self._closed = False

        # ---- session 状态（全部住在**引擎**上，绝不放到 recognizer 上）----
        # OCRWorker.set_profile() 会重建 RoomCodeRecognizer 但**不碰引擎**
        # （worker.py:707-712），放到 recognizer 上会被静默丢掉。
        self._session = None
        self._input_name: Optional[str] = None
        self._pinned_width: Optional[int] = None
        self._roi_sig: Optional[tuple] = None
        #: **当前** session 见过的输入 shape。每次建 session 清空 ——
        #: 「一个 session 只能有一个 shape」这条不变量靠它直接断言。
        self._shapes_seen = set()
        #: 累计（跨 session）见过的 shape，仅用于诊断留痕。
        self._shapes_seen_total = set()
        self._warm = False
        self._fatal = False
        #: 每次 session 建成自增。OCRWorker 靠它判断「刚才是新建/重建了」，
        #: 从而在该事件后准确地重发 session_ready。
        self._session_generation = 0

        # ---- 运行时（函数内导入！见下方注释）----
        import onnxruntime as ort  # noqa: PLC0415

        self._ort = ort

        # provider 在**构造期探测一次**，不是靠捕获建 session 失败 ——
        # 后者会随 ROI 变化反复发生，把一次性的能力判断变成每次都赌一次。
        available = list(ort.get_available_providers())
        if _PROVIDER_DML in available:
            self._provider_requested = _PROVIDER_DML
        else:
            self._provider_requested = _PROVIDER_CPU
            logger.warning(
                "[ONNX] 未检测到 %s（可用: %s），回退 %s。"
                "CPU 路径在 W=320 约 320ms/帧，OCR 节拍会明显放慢。",
                _PROVIDER_DML, available, _PROVIDER_CPU,
            )
        self._provider_actual: Optional[str] = None

        # ---- 预处理 / 解码镜像（Step 0 已验证，逐字移植）----
        self._pre = OCRReisizeNormImgMirror(REC_IMAGE_SHAPE)
        # 只读 runtime/onnx/charset.txt；CTCLabelDecodeMirror 仍按 PaddleX 语义
        # 在头部插入 blank、末尾追加空格 —— 与 Step 0 验证过的映射逐项一致。
        charset = load_charset(self._charset_path)
        self._decoder = CTCLabelDecodeMirror(charset)
        if len(self._decoder.character) != CHARSET_FINAL_SIZE:
            # 最终字符表长度不符 = 每个字符索引都错位，必须硬失败。
            raise ONNXEngineError(
                "字符表最终长度不符: 期望 %d，实际 %d (%s)"
                % (CHARSET_FINAL_SIZE, len(self._decoder.character),
                   self._charset_path)
            )

        self._init_ms: Optional[float] = (time.perf_counter() - t0) * 1000.0
        logger.info(
            "[ONNX] model=%s charset=%s provider=%s chars=%d init=%.0fms",
            self._model_path, self._charset_path, self._provider_requested,
            len(self._decoder.character), self._init_ms,
        )

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def model_path(self) -> str:
        return self._model_path

    @property
    def charset_path(self) -> str:
        return self._charset_path

    @property
    def init_ms(self) -> Optional[float]:
        return self._init_ms

    @property
    def is_ready(self) -> bool:
        """已建 session 且 warmup 完成 —— 此刻才真的可以 OCR。"""
        return self._session is not None and self._warm and not self._closed

    @property
    def is_fatal(self) -> bool:
        """引擎已进入不可恢复状态（如 session 无法建立）。

        OCRWorker 见到该标志会停止主循环，避免每 300ms 重试一次昂贵且必然失败的初始化。
        """
        return self._fatal

    @property
    def pinned_width(self) -> Optional[int]:
        return self._pinned_width

    @property
    def session_generation(self) -> int:
        """session 建成次数（0 = 尚未建）。每次 建成/重建 自增。"""
        return self._session_generation

    @property
    def backend_status(self) -> dict:
        """后端状态快照（诊断用，不参与识别）。"""
        return {
            "backend": "onnx",
            "model_name": MODEL_NAME,
            "model_path": self._model_path,
            "charset_path": self._charset_path,
            "provider_requested": self._provider_requested,
            "provider_actual": self._provider_actual,
            "pinned_width": self._pinned_width,
            "roi_sig": list(self._roi_sig) if self._roi_sig else None,
            # 当前 session 的 shape 集合：正常恰好一个，是「一 ROI 一 shape」的直接证据。
            "shapes_seen": sorted(list(s) for s in self._shapes_seen),
            # 跨 session 累计（仅诊断留痕；ROI 变化导致重建时正常会多于一个）。
            "shapes_seen_total": sorted(list(s) for s in self._shapes_seen_total),
            "warm": self._warm,
            "session_generation": self._session_generation,
            "fatal": self._fatal,
            "init_ms": self._init_ms,
            "closed": self._closed,
        }

    # ------------------------------------------------------------------
    # 守卫
    # ------------------------------------------------------------------

    def _check_usable(self) -> None:
        """确认本实例可用且是在属主线程里被调用。"""
        if self._closed:
            raise ONNXEngineError("ONNX 引擎已释放，不能继续使用")
        current = threading.get_ident()
        if current != self._owner_thread:
            # session 句柄不跨线程使用（与 engine.py 的常驻 Tesseract 同一条约束）。
            raise ONNXEngineError(
                "ONNX 引擎只能在其属主线程内使用（owner=%s, current=%s）"
                % (self._owner_thread, current)
            )

    @staticmethod
    def _validate_image(image) -> None:
        """只校验、不转换（镜像 PPOCREngine._validate_image）。

        不做灰度、不复制、不改 dtype、不加任何预处理。校验必须发生在**任何
        几何计算之前**：图像为空时算宽高比会 ZeroDivisionError。
        """
        if image is None or getattr(image, "size", 0) == 0:
            raise ONNXEngineError("输入图像为空")
        shape = getattr(image, "shape", None)
        if shape is None or len(shape) != 3 or shape[2] != 3:
            raise ONNXEngineError(
                "输入图像通道数不受支持：期望 uint8 BGR (H, W, 3)，实际 %s"
                % (shape,)
            )
        dtype_name = getattr(getattr(image, "dtype", None), "name", None)
        if dtype_name != "uint8":
            raise ONNXEngineError(
                "输入图像 dtype 不受支持：期望 uint8，实际 %s" % (dtype_name,)
            )

    # ------------------------------------------------------------------
    # session 生命周期
    # ------------------------------------------------------------------

    @staticmethod
    def width_for_image(image: np.ndarray) -> int:
        """按镜像规则算出这张图需要的网络输入宽度。

        与 OCRReisizeNormImgMirror.resize 的 imgW 规则同式：
            imgW = int(48 * max(320/48, w/h))，超过 3200 压到 3200
        """
        h, w = image.shape[:2]
        _imgC, imgH, imgW = REC_IMAGE_SHAPE
        max_wh_ratio = max(imgW / float(imgH), w / float(h))
        width = int(imgH * max_wh_ratio)
        return _MAX_IMG_W if width > _MAX_IMG_W else width

    def _close_session(self) -> None:
        """彻底丢弃当前 session（这是「一 shape 一 session」缓解措施的前提）。"""
        session = self._session
        self._session = None
        self._input_name = None
        self._pinned_width = None
        self._warm = False
        if session is not None:
            del session
            gc.collect()

    def _run(self, image: np.ndarray):
        """钉宽 pad -> 断言宽度 -> 推理。返回原始输出列表。"""
        x = self._pre.resize_norm_img(
            image, self._pinned_width / float(REC_IMAGE_SHAPE[1])
        )[np.newaxis, ...]

        # 硬断言：图输入是动态维，宽度不符 ORT **不会报错**，只会静默重特化 ——
        # 那会让 DirectML 永久慢 3–6 倍（STEP1_REPORT.md 第 9 节），
        # 静默的性能悬崖比一个显式异常糟得多。
        if int(x.shape[3]) != self._pinned_width:
            raise ONNXEngineError(
                "ONNX 输入宽度与钉死宽度不一致：期望 %s，实际 %s"
                "（这会在 DirectML 上引入第二个 shape，必须中止）"
                % (self._pinned_width, int(x.shape[3]))
            )

        shape = tuple(int(v) for v in x.shape)
        self._shapes_seen.add(shape)
        self._shapes_seen_total.add(shape)
        try:
            return self._session.run(None, {self._input_name: x})
        except Exception as e:  # noqa: BLE001 - 推理失败显式上报，不吞成空文本
            raise ONNXEngineError("ONNX 推理失败: %s" % e) from e

    def _build_session(self, width: int, warmup_image: np.ndarray) -> None:
        """按固定宽度建 session 并 warmup。失败一律显式抛出。"""
        self._close_session()
        # 新 session = 新的「shape 集合」起点：warmup 与新会话的第一帧一起
        # 构成该 session 的 shape 记录，正常应恰好 1 个。
        self._shapes_seen.clear()

        providers = [self._provider_requested]
        try:
            session = self._ort.InferenceSession(
                self._model_path, providers=providers
            )
        except Exception as e:  # noqa: BLE001
            if self._provider_requested == _PROVIDER_DML:
                # 明确回退，并**响亮**记录：CPU 路径约 320ms/帧，节拍会显著放慢。
                logger.warning(
                    "[ONNX] %s 建 session 失败（%s），回退 %s。"
                    "CPU 路径在 W=320 约 320ms/帧，OCR 节拍会明显放慢。",
                    _PROVIDER_DML, e, _PROVIDER_CPU,
                )
                self._provider_actual = _PROVIDER_CPU
                try:
                    session = self._ort.InferenceSession(
                        self._model_path, providers=[_PROVIDER_CPU]
                    )
                except Exception as e2:  # noqa: BLE001
                    raise ONNXEngineError(
                        "ONNX session 创建失败（DML 与 CPU 都失败）: %s" % e2
                    ) from e2
            else:
                raise ONNXEngineError("ONNX session 创建失败: %s" % e) from e

        actual = list(session.get_providers())
        if self._provider_actual is None:
            self._provider_actual = (
                actual[0] if actual else self._provider_requested
            )
        # 只请求一个 provider，所以这里能查出「请求了却没用上」的静默降级。
        if self._provider_actual not in actual:
            del session
            gc.collect()
            raise ONNXEngineError(
                "ONNX 请求 %s，但实际生效的 provider 是 %s"
                % (self._provider_actual, actual)
            )

        self._session = session
        self._input_name = session.get_inputs()[0].name
        self._pinned_width = int(width)

        for _ in range(WARMUP_RUNS):
            self._run(warmup_image)
        self._warm = True
        self._session_generation += 1

        logger.info(
            "[ONNX] session 已就绪: width=%d provider=%s warmup=%d shapes=%s",
            self._pinned_width, self._provider_actual, WARMUP_RUNS,
            sorted(list(s) for s in self._shapes_seen),
        )

    def ensure_session_for_roi(self, roi, image: np.ndarray) -> None:
        """确保 session 与当前 ROI 匹配；未变化时是廉价 no-op。

        由 OCRWorker 在**工作线程内**每轮调用一次（鸭子类型探测，
        与 room_code.py:595 的 feature-detection 风格一致）。
        ROI 或宽度一变就关闭旧 session、按新宽度重建并 warmup。
        """
        self._check_usable()
        if self._fatal:
            raise ONNXEngineError("ONNX 引擎已进入致命状态，不再重试建 session")

        self._validate_image(image)

        sig = (
            int(getattr(roi, "x", 0)), int(getattr(roi, "y", 0)),
            int(getattr(roi, "width", 0)), int(getattr(roi, "height", 0)),
            int(getattr(roi, "source_width", 0)),
            int(getattr(roi, "source_height", 0)),
        )
        if self._session is not None and sig == self._roi_sig:
            return

        width = self.width_for_image(image)
        try:
            self._build_session(width, image)
        except Exception:
            # 关键顺序：**成功之前绝不写 _roi_sig**。否则下一轮会跳过 ensure，
            # _infer 撞上 _session is None，问题从「建不出来」变成「随机报错」。
            self._fatal = True
            raise
        self._roi_sig = sig

    # ------------------------------------------------------------------
    # 推理
    # ------------------------------------------------------------------

    def _infer(self, image) -> Tuple[str, Optional[float]]:
        self._check_usable()
        if self._session is None:
            # 必须是 ONNXEngineError：AttributeError 不是 OCREngineError，
            # 会绕过 worker.py:523 的 catch，落到 run() 的宽处理里每 300ms 刷一次错误。
            raise ONNXEngineError(
                "ONNX 会话尚未创建（ROI 未确定或尚未调用 ensure_session_for_roi）"
            )

        self._validate_image(image)
        outputs = self._run(image)

        # CTCLabelDecodeMirror.__call__ 返回 list[(text, score)]，**不是**两元组。
        decoded = self._decoder(outputs)
        if not decoded:
            return "", None
        text, score = decoded[0]
        return text, score

    def iter_readings(self, image, preprocess: bool = True) -> Iterator[Tuple[str, str]]:
        """产出一条读法（label, text）；空结果什么都不产出。

        preprocess: 保留该参数以匹配既有契约；本引擎的预处理在 _infer 内
        （钉宽 pad），与 OCREngine/PPOCREngine 一样不额外做调用方预处理。
        """
        text, _score = self._infer(image)
        if text:
            logger.debug("ONNX 读法: '%s'", text)
            yield (READING_LABEL, text)

    def recognize(self, image, preprocess: bool = True, return_details: bool = False):
        """识别单张 ROI。return_details=True 时返回 dict（键与其它引擎一致）。"""
        text, score = self._infer(image)
        if not return_details:
            return text
        return {
            "text": text,
            "confidence": score if score is not None else 0,
            # ONNX/PP-OCRv6 没有 PSM 概念：如实留 None，不编造编号。
            "psm_used": None,
            "details": None,
        }

    def shutdown(self) -> None:
        """释放 session（幂等）。只能在其属主线程内调用。"""
        if self._closed:
            return
        self._closed = True
        try:
            self._close_session()
        except Exception as e:  # noqa: BLE001 - 回收失败不影响线程退出
            logger.warning("[ONNX] session 释放失败（不影响识别）: %s", e)
        logger.info("[ONNX] 引擎已释放")


# `import onnxruntime` 必须留在 ONNXEngine.__init__ 内部（函数内导入），
# **不能**提到模块顶层：
#   1. 生产 venv 里没有 onnxruntime（实测），而 ocr/__init__.py 会导入本模块 ——
#      顶层导入会让任何 `import ocr` 直接 ImportError，整个应用起不来。
#   2. 与 ppocr_engine.py 对 paddle 的做法一致，也是
#      test_ppocrv6_backend.py [F] 静态检查所守的约定（模块顶层导入会让 GUI
#      启动即付出加载代价；实测 import onnxruntime 约 142ms）。
