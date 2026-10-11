# -*- coding: utf-8 -*-
"""
OCR 工作线程 (Phase 2.3)

实现实时 OCR 识别，不阻塞 GUI 主线程。

功能：
    - 从 LatestFrameBuffer 获取最新帧
    - 根据 ROIManager 裁剪识别区域
    - 通过 RoomCodeRecognizer（OCR V2.1 房间号识别管线）执行识别
    - 经 TemporalResolver 做时序一致性裁决（OCR V2.3，默认连续 2/3 帧一致才成立）
    - 定时识别（默认 300ms）
    - 只在结果变化时发送信号

对外只传出【严格校验通过、且时序一致】的房间号；否则传出空字符串，
因此下游（显示/剪贴板）拿到的一定是合法房间号，不会是 OCR 的原始噪声。
"""

import logging
import os
import threading
import time
from typing import Optional

import numpy as np
from PySide6.QtCore import QThread, Signal

from .engine import OCREngine, OCREngineError
from .onnx_engine import ONNXEngine
from .ppocr_engine import PPOCREngine
from .profiles import AUTO, get_profile
from .roi_manager import ROI, ROIManager
from utils.diag_trace import TRACE
from .room_code import DEFAULT_SCALE, RoomCodeRecognizer
from .temporal import TemporalResolver
# from .clipboard import ClipboardManager

logger = logging.getLogger("DouyinLowLatencyViewer.ocr.worker")

__all__ = ["OCRWorker"]

#: OCR engine 选择器（与 DLV_OCR_BACKEND / DLV_PPOCR_MODEL_DIR 同一命名模式）。
#: 注意：这是**新**的选择器，语义比 DLV_OCR_BACKEND 更严格 —— 见下。
_ENV_ENGINE = "DLV_OCR_ENGINE"

#: 可选值。
#: 默认是 **onnx**：旧的 NVIDIA-only Paddle 模型资源
#: （runtime/ppocr/PP-OCRv6_medium_rec_infer/）已在 Step 2 验收通过后删除，
#: ppocr 不再是一个可用的默认后端。
#: 显式写 DLV_OCR_ENGINE=ppocr 仍会照做（兼容分支保留），但**不会**为了让它
#: 「继续可用」而保留旧模型；也**不存在**任何从 onnx 静默回落到 ppocr 的路径。
_ENGINE_PPOCR = "ppocr"
_ENGINE_ONNX = "onnx"
_ENGINE_DEFAULT = _ENGINE_ONNX


def _selected_engine_kind() -> str:
    """决定构造哪种 OCR 引擎：``"onnx"``（默认）或 ``"ppocr"``（兼容分支）。

    未知取值**硬报错**，不静默回落。理由：这是一次明确的 backend 选择，
    用户显式写了 onnx 却因拼写错误悄悄跑在 Paddle 上，会制造
    「程序看起来一切正常、实际根本没走 ONNX」的假象 —— 那是最难查的一类问题。
    （`DLV_OCR_BACKEND` 的历史语义是 WARNING + 默认值；本函数刻意不沿用，
     也**不修改** `DLV_OCR_BACKEND` 自身的行为。）

    Tesseract 刻意**不在**可选值里：`_ensure_engine` 的函数体被
    test_ppocrv6_backend.py [B] 静态扫描，断言其中不得出现 ``OCREngine(``。
    Tesseract 仍可经 ``OCRWorker(engine=...)`` 显式注入（对照 / 诊断 / 人工回退）。

    异常处理刻意放在这个**模块级函数**里，而不是 `_ensure_engine` 内 ——
    后者的函数体被静态断言要求「不得出现 except」。
    """
    value = (os.environ.get(_ENV_ENGINE) or "").strip().lower()
    if not value:
        return _ENGINE_DEFAULT
    if value in (_ENGINE_PPOCR, _ENGINE_ONNX):
        return value
    raise OCREngineError(
        "未知的 %s=%r：可选 %r / %r（留空即默认 %r）"
        % (_ENV_ENGINE, value, _ENGINE_PPOCR, _ENGINE_ONNX, _ENGINE_DEFAULT)
    )


def _require_explicit_game(profile):
    """校验档位确实是【用户明确选定的某一款游戏】。

    正式 OCR 接入必须由用户选择游戏：None 与 "auto" 都会落到
    「同时尝试两款游戏的格式」，那等于用识别内容反推游戏类型，
    而两款游戏的 format/correction 规则必须隔离（见 ocr/profiles.py）。
    因此这里直接拒绝，而不是静默回落到默认档——
    静默回落会让「没选游戏」表现为「识别不出」，既难排查也违反隔离要求。
    """
    resolved = get_profile(profile)
    if resolved.profile_id == AUTO.profile_id:
        raise ValueError(
            "OCR 必须指定具体游戏档（delta_force / valorant），"
            f"不接受 {profile!r}：auto 会同时套用两款游戏的格式，"
            "相当于用 OCR 内容猜游戏类型。请先在界面选择游戏再启动识别。"
        )
    return resolved


class OCRWorker(QThread):
    """
    OCR 识别工作线程

    在后台线程中定期执行 OCR 识别，避免阻塞 GUI。
    识别完成后通过信号通知主线程。

    信号：
        text_updated(str) - 当识别的文本发生变化时发送
        copy_requested(str) - 请求把成立过的房间号复制到剪贴板
        game_mismatch() - 所选游戏与识别内容明显冲突，请用户重新选择
                          （不带参数；worker 不会因此换档或重启）

    特性：
        - 稳态 interval_ms（默认 300ms）识别一次；确认窗口内用更短的间隔
        - 只在文本变化时发送信号
        - 支持动态启停
        - 完善的错误处理
    """

    # 信号：文本更新（仅在文本变化时发送）
    text_updated = Signal(str)
    # 信号：错误通知
    error_occurred = Signal(str)
    # 信号：请求复制到剪切板（由GUI线程执行）
    copy_requested = Signal(str)
    # 信号：所选游戏与识别内容明显冲突，需要用户重新选择。
    # 刻意不带任何参数：文案属于 UI，且绝不能把游戏档/位数等内部信息传出去。
    # 本信号只用于提醒——worker 不会因为收到冲突而换档、重启或自动选择。
    game_mismatch = Signal()
    # 信号：OCR 引擎已构造完成（运行时 + 模型 + 字典都已就绪）。
    # 与帧、ROI 无关，run() 初始化成功后**无条件**发一次 —— 若把它绑到「首次成功
    # warmup」，那么流卡住 / ROI 为空 / 裁剪失败都会让 UI 永久停在「加载中」
    # 且没有任何错误，那正是该信号要消除的谎言。
    # 由工作线程发出，Qt 跨线程投递到 GUI 线程。
    engine_ready = Signal()
    # 信号：引擎的会话已建成/重建并完成 warmup，此刻才**真的**可以 OCR。
    # 只有提供 ensure_session_for_roi 的引擎（ONNXEngine）会发；
    # 其它引擎从不发出，行为与今天完全一致。
    session_ready = Signal()

    def __init__(self,
                 frame_buffer_getter,
                 roi_manager: ROIManager,
                 engine: Optional[OCREngine] = None,
                 interval_ms: int = 300,
                 profile=None,
                 scale: float = DEFAULT_SCALE,
                 recognizer: Optional[RoomCodeRecognizer] = None,
                 resolver=None,
                 pending_interval_ms: int = 100,
                 diagnostics=None,
                 observe_all_psm: bool = False,
                 length_escalation: bool = False,
                 sample_capture=None):
        """
        初始化 OCR 工作线程

        参数：
            frame_buffer_getter: 获取帧缓冲区的回调函数
                                 返回: LatestFrameBuffer 实例
            roi_manager: ROI 管理器实例
            engine: OCR 引擎实例（为 None 时在【工作线程内】延迟创建）
            interval_ms: 稳态识别间隔（毫秒，默认 300）
            profile: 游戏档（GameProfile 或 profile_id）。
                     【必须】是具体某一款游戏（delta_force / valorant）：
                     None 或 "auto" 会抛 ValueError，见 _require_explicit_game()
            scale: 预处理缩放（OCR V2.1 实验能力，默认 1.0 不放大）
            recognizer: 房间号识别器（为 None 时在【工作线程内】构造，
                        包裹上面的引擎；构造它不会触碰 Tesseract）
            resolver: 候选裁决器（OCR V2.3）。None 表示默认的时序裁决
                        TemporalResolver(2/3)：同一房间号连续两帧一致才算数。
                        需要单帧直通时显式传 SingleFrameResolver()。
                        裁决器只做时序判定，不参与格式校验/纠正。
            pending_interval_ms: 【确认窗口】内的采样间隔（毫秒，默认 100）。
                        当裁决器报告"有一个新候选正在被确认"时，下一次采样
                        用这个更短的间隔，好让连续一致的那几次观测尽快凑齐；
                        一旦成立/没有待确认候选，立刻回到 interval_ms。
                        它**只改采样节奏**，不改任何判定规则（见 _interval_for_wait）。
                        设为 >= interval_ms 即等价于关闭该行为。
            diagnostics: 可选的诊断记录器（ocr/diagnostics.py）。None（默认）
                        表示不诊断——识别路径上只多一次 None 判断，其余行为
                        逐字不变。诊断只记录**已经发生**的事实（执行序号、
                        耗时、各读法原文与候选、仲裁结论、连续确认结论、
                        已请求/已成功复制的房间码），不参与任何判定。
            observe_all_psm: **全 PSM 观测**（默认 False）。开启后，本来会提前
                        停止的那一帧会继续跑完其余 PSM，但补跑的读法只写诊断
                        记录（标签带「观测」），不参与候选仲裁、选错游戏判定、
                        时序裁决，因此复制结果与关闭时完全一致。
                        它只在诊断记录器存在且已开启时才产生额外 OCR 调用；
                        记录器不存在或已关闭时，本开关不产生任何额外开销。
                        参数在构造时固定，构造识别器的两条路径（首次创建与
                        换档重建）都从这里取值，所以停机重启不会残留旧状态。
            length_escalation: **长度异常复看**（默认 False）。开启后，只有
                        「本帧无候选且读法长度不属于本档任何格式」的帧才会
                        多跑一次 1.5x 复看，因此不影响正常帧的 PSM 调度与
                        采样节奏。复看结果同样经过格式校验与时序裁决，
                        不直接输出、不直接复制。关闭时行为与从前逐字相同。
                        参数在构造时固定，两条识别器构造路径都从这里取值。
            sample_capture: 可选的 ROI 样本采集器（ocr/sample_capture.py）。
                        None（默认）或关闭时，识别路径上只多一次布尔判断，
                        不写盘、不占内存。它采的是识别器**真正看到的**那一帧
                        ROI 像素，只落盘、不参与任何判定，写盘失败也不会
                        影响识别（见该模块说明）。
        """
        super().__init__()

        # 正式路径必须由用户选定游戏：不猜、也不静默回落（先校验再改动任何状态）
        _require_explicit_game(profile)

        self._frame_buffer_getter = frame_buffer_getter
        self._roi_manager = roi_manager
        # 构造 OCRWorker 绝不构造引擎：无论当前后端是 PP-OCRv6（要加载
        # paddle/CUDA 运行库与模型）还是 Tesseract（要定位可执行文件并冷加载
        # DLL），初始化都很重，放在 GUI 线程会让点击「识别」后界面卡死。
        # 一律延迟到 run() 内、在工作线程里构造。
        self._engine = engine
        self._interval_ms = interval_ms
        # 确认窗口内的采样间隔（只影响"隔多久再采一次"，不影响任何判定）
        self._pending_interval_ms = int(pending_interval_ms)
        self._profile = profile
        self._scale = scale
        # 同上：识别器也延迟到工作线程内构造（它依赖引擎）
        self._recognizer = recognizer
        # 候选裁决器：默认启用时序一致性（OCR V2.3），构造它是纯内存操作，
        # 不触碰 Tesseract，因此放在这里（GUI 线程）是安全的。
        self._resolver = TemporalResolver() if resolver is None else resolver
        # 诊断记录器（None = 不诊断）。同一实例也交给识别器，用于记录各读法。
        self._diagnostics = diagnostics
        # 全 PSM 观测（默认关闭）。构造时固定，两条识别器构造路径都从它取值，
        # 因此不会出现「上一次运行开过、重启后还开着」的残留状态。
        self._observe_all_psm = bool(observe_all_psm)
        # 长度异常复看（默认关闭）。同样在构造时固定，两条识别器构造路径都
        # 从这里取值，因此不存在「上一次开过、重启后还开着」的残留状态。
        self._length_escalation = bool(length_escalation)
        # ROI 样本采集（默认 None = 不采集）。纯旁路：只把识别器看到的那一帧
        # 落盘，不参与任何判定，采集器内部自带开关与上限。
        self._sample_capture = sample_capture

        # 注释掉剪切板管理器，改为由GUI线程处理
        # self._clipboard_manager = ClipboardManager()

        # 线程控制
        self._running = False
        self._stopped_event = threading.Event()

        # 「选错游戏」提醒：一次冲突只提醒一次，直到用户重新选择或冲突解除
        self._mismatch_notified = False

        # 状态跟踪
        self._last_text: Optional[str] = None
        self._recognition_count: int = 0
        self._error_count: int = 0
        self._last_error: Optional[str] = None
        # 最近一次见到的引擎 session 世代号（只有 ONNXEngine 有）。
        # 用它判断「会话刚建成/重建过」，从而准确地发 session_ready。
        self._last_session_generation: int = 0

        logger.info(
            "OCRWorker 初始化: 间隔=%dms, 引擎=%s, 识别器=%s, 缩放=%.1fx",
            interval_ms,
            "自定义" if engine else "延迟创建(工作线程内)",
            "自定义" if recognizer else "延迟创建(工作线程内)",
            float(scale),
        )

    def start_recognition(self) -> None:
        """启动 OCR 识别"""
        if self._running:
            logger.warning("OCRWorker 已经在运行")
            return

        self._running = True
        self._stopped_event.clear()
        self._last_text = None
        self._mismatch_notified = False
        self._recognition_count = 0
        self._error_count = 0
        self._last_error = None

        self.start()  # 启动 QThread
        logger.info("OCRWorker 已启动")

    def stop_recognition(self) -> None:
        """停止 OCR 识别"""
        if not self._running:
            return

        self._running = False
        self._stopped_event.set()

        # 等待线程结束（最多等待 2 秒）
        if self.isRunning():
            self.wait(2000)

        logger.info(
            "OCRWorker 已停止: 识别次数=%d, 错误次数=%d",
            self._recognition_count, self._error_count
        )

    def is_recognizing(self) -> bool:
        """是否正在识别"""
        return self._running and self.isRunning()

    @property
    def last_text(self) -> Optional[str]:
        """最后一次识别的文本"""
        return self._last_text

    @property
    def recognition_count(self) -> int:
        """累计识别次数"""
        return self._recognition_count

    @property
    def error_count(self) -> int:
        """累计错误次数"""
        return self._error_count

    def _ensure_engine(self) -> "PPOCREngine | OCREngine":
        """在工作线程内延迟构造 OCR 引擎。

        引擎构造会加载 OCR 运行时（PP-OCRv6 要加载 paddle + CUDA 运行库 + 模型，
        实测约 2.2s；Tesseract 要定位可执行文件并冷加载 DLL），因此【必须】
        发生在工作线程，而不是点击「识别」的 GUI 线程调用链上。

        默认后端是 PP-OCRv6_medium_rec（GPU）；可用 DLV_OCR_ENGINE=onnx 切到
        ONNX（DirectML）后端。两者构造失败都抛 OCREngineError 的子类
        （PPOCREngineError / ONNXEngineError），因此 run() 既有的初始化失败
        路径（报错一次 + error_occurred + 回收 + 结束线程）原样生效：
        既不回退 CPU，也不回退 Tesseract。

        ONNX 的 session **不在这里建**：它的 shape 要等 ROI 才能确定，
        由 _ensure_engine_session() 在首轮拿到 ROI 时建立并 warmup。

        Tesseract 引擎仍然可以显式注入（engine= 参数）用于对照、诊断与人工
        回退，但那是一条**显式**路径，不存在任何运行期自动回落。
        """
        if self._engine is None:
            if _selected_engine_kind() == _ENGINE_ONNX:
                self._engine = ONNXEngine()
                logger.info("OCR 引擎（ONNX/DirectML）已在工作线程内创建")
            else:
                self._engine = PPOCREngine()
                logger.info("OCR 引擎（PP-OCRv6）已在工作线程内创建")
        return self._engine

    def _ensure_recognizer(self) -> RoomCodeRecognizer:
        """在工作线程内延迟构造房间号识别器（OCR V2.1）。

        识别器本身只是格式/纠正逻辑的持有者，不触碰 Tesseract；
        但它包裹引擎，因此和 _ensure_engine 一样【只能】在工作线程调用。
        """
        if self._recognizer is None:
            self._recognizer = RoomCodeRecognizer(
                self._ensure_engine(),
                profile=self._profile,
                scale=self._scale,
                resolver=self._resolver,
                diagnostics=self._diagnostics,
                observe_all_psm=self._observe_all_psm,
                length_escalation=self._length_escalation,
            )
            logger.info("房间号识别器已在工作线程内创建")
        return self._recognizer

    def _ensure_engine_session(self, roi, roi_image) -> None:
        """让引擎的会话与当前 ROI 对齐（ONNX 后端特有；其它引擎是空操作）。

        鸭子类型能力探测，与 room_code.py:595 的
        ``getattr(self._engine, "iter_readings", None)`` 同一风格 ——
        不引入 ABC / 登记表，也不要求既有引擎实现任何新方法。

        对 PPOCREngine / OCREngine：它们没有 ``ensure_session_for_roi``，
        本方法直接返回，识别链路**一字不变**。
        对 ONNXEngine：ROI 未变时是廉价 no-op；ROI 真正变化时在**本线程**
        （工作线程 = 引擎属主线程）关旧 session、按新的 pinned width 重建并
        warmup。失败抛 ONNXEngineError —— 它是 OCREngineError 的子类，
        调用点的 ``except OCREngineError`` 既有权路径原样接管。

        调用点必须在 _perform_recognition 的 try **内部**：放在 try 外面的话，
        异常会绕过 warning 级的 OCREngineError 分支，落进 run() 的 error 级
        宽处理里，每 300ms 刷一次。
        """
        # 读 self._engine（不是 recognizer）：session 状态属于引擎。
        # 放进 recognizer 会被 set_profile() 的重建静默丢掉。
        ensure = getattr(self._engine, "ensure_session_for_roi", None)
        if ensure is None:
            return
        ensure(roi, roi_image)

        # 会话刚刚建成/重建过（generation 变了）→ 此刻才真的可以 OCR，如实上报。
        # 其它引擎没有该属性，也就从不发 session_ready，行为与今天一致。
        generation = getattr(self._engine, "session_generation", None)
        if generation is not None and generation != self._last_session_generation:
            self._last_session_generation = generation
            self.session_ready.emit()

    def run(self) -> None:
        """OCR 识别主循环（在后台线程中执行）"""
        logger.info("OCRWorker 主循环开始")

        # 引擎/识别器初始化：失败则只报错一次并结束线程（避免每 300ms 重复报错）
        try:
            self._ensure_engine()
            self._ensure_recognizer()
        except Exception as e:
            self._error_count += 1
            self._last_error = str(e)
            logger.error("OCR 引擎初始化失败: %s", e, exc_info=True)
            self.error_occurred.emit(str(e))
            self._note_worker_error(f"引擎初始化失败: {e}")
            logger.info("OCRWorker 主循环结束")
            self._shutdown_engine()      # 引擎可能已建好而识别器构造失败：一并回收
            return

        # 引擎已就绪：运行时 + 模型 + 字典都已加载完成。
        # 与帧、ROI 无关，**无条件**发一次 —— 若绑到「首次成功 warmup」，
        # 流卡住 / ROI 为空 / 裁剪失败都会让 UI 永久停在「加载中」却不报错。
        self.engine_ready.emit()

        while self._running and not self._stopped_event.is_set():
            if TRACE.enabled:
                TRACE.mark_activity("ocr_infer_start")
            try:
                self._perform_recognition()

            except Exception as e:
                self._error_count += 1
                error_msg = f"OCR 识别异常: {e}"
                logger.error(error_msg, exc_info=True)
                self._last_error = error_msg
                self.error_occurred.emit(error_msg)
                self._note_worker_error(error_msg)

            # H6 负载归因：在诊断追踪时间线上标记每轮 OCR 活动边界
            # （ocr_infer_start → ocr_round 的间隔 ≈ 本轮推理+取帧耗时），
            # 用于与 GUI 慢帧离群做时间相关性分析。
            # 纯旁路：TRACE 关闭时是一次布尔判断，绝不影响识别路径。
            if TRACE.enabled:
                TRACE.mark_activity("ocr_round")
                TRACE.bump("c_ocr_rounds")

            # 等待间隔：稳态用 interval_ms；有新候选待确认时用更短的间隔
            self._stopped_event.wait(self._interval_for_wait() / 1000.0)

        logger.info("OCRWorker 主循环结束")
        # 会话结束：把最终的丢帧统计写进诊断（只读计数，纯旁路）
        self._note_frame_drops()
        self._shutdown_engine()

    def _note_worker_error(self, text: str) -> None:
        """把 worker 级异常写进诊断记录（未接线/未开启时是空操作）。

        纯旁路：调用方的 logger 与 error_occurred 信号、以及异常处理流程
        都保持不变，这里只是在既有处理之外多写一条诊断。
        """
        diagnostics = self._diagnostics
        if diagnostics is None:
            return
        try:
            diagnostics.note_worker_error(text)
        except Exception:  # noqa: BLE001 - 诊断绝不冒泡进识别/退出路径
            logger.debug("worker 异常写诊断失败", exc_info=True)

    def _note_frame_drops(self) -> None:
        """把 frame_buffer 的最终丢帧统计写进诊断（只读，纯旁路）。

        丢帧数是 frame_buffer 单槽覆盖的累计计数（见 LatestFrameBuffer
        .dropped_count）。拿不到缓冲区、或缓冲区没有该计数时静默跳过——
        诊断缺一条记录，绝不影响识别与退出。
        """
        diagnostics = self._diagnostics
        if diagnostics is None:
            return
        try:
            buffer = self._frame_buffer_getter()
            dropped = getattr(buffer, "dropped_count", None)
            total = getattr(buffer, "update_count", None)
            if dropped is None or total is None:
                return
            diagnostics.note_frame_drops(dropped, total)
        except Exception:  # noqa: BLE001 - 诊断绝不冒泡进识别/退出路径
            logger.debug("丢帧统计写诊断失败", exc_info=True)

    def _shutdown_engine(self) -> None:
        """释放本线程持有的 OCR 引擎资源（OCR-3：常驻引擎句柄随 worker 退出释放）。

        只做资源回收：不改变任何识别/判定行为，失败也只记日志。
        引擎是延迟在工作线程内创建的，因此释放也必须发生在这里。
        """
        engine = self._engine
        if engine is None:
            return
        try:
            engine.shutdown()
        except Exception as e:  # noqa: BLE001 - 回收失败不影响线程退出
            logger.warning("OCR 引擎资源释放失败（不影响识别）: %s", e)

    def _interval_for_wait(self) -> int:
        """本次循环结束后的等待间隔（毫秒）。

        唯一目的是**采样节奏**：待确认的新候选需要尽快凑齐连续一致的那几次
        观测，所以确认窗口内采样密一些；一旦成立或没有待确认候选，立刻回到
        稳态间隔。

        刻意**不在这里做任何判定**：
          - "是否待确认"完全由裁决器从它自己的观测窗口推导（TemporalResolver
            .has_pending），本层不另存候选/已确认状态，也就不可能出现两套
            状态不一致的情况；
          - 是否成立、是否复制，仍由裁决器与下面的既有逻辑决定，本方法
            只返回一个等待时长。
        稳态间隔 <= 确认窗口间隔时取稳态值，因此 set_interval() 调小之后
        不会出现"确认期间反而等得更久"。
        """
        # has_pending 是裁决器的**可选**能力（单帧直通等无状态裁决器没有它）：
        # 拿不到就按"没有待确认候选"处理，即稳态间隔。
        if getattr(self._resolver, "has_pending", False):
            return min(self._pending_interval_ms, self._interval_ms)
        return self._interval_ms

    def _perform_recognition(self) -> None:
        """执行一次 OCR 识别"""
        # 1. 获取帧缓冲区
        frame_buffer = self._frame_buffer_getter()
        if frame_buffer is None:
            logger.debug("帧缓冲区不可用")
            return

        # 2. 获取 ROI
        roi = self._roi_manager.get_roi()
        if roi is None:
            logger.debug("ROI 未设置")
            return

        # 3. 获取最新帧
        frame = frame_buffer.get()
        if frame is None:
            logger.debug("无可用帧")
            return

        # 4. 裁剪 ROI 区域
        try:
            roi_image = self._crop_roi(frame, roi)
        except Exception as e:
            logger.warning("ROI 裁剪失败: %s", e)
            return

        # 5. 房间号识别管线：OCR 引擎 -> 清洗 -> 候选提取 -> 逐位纠正 -> 严格校验
        #    对外契约：只有严格校验通过的房间号才会被传出，其余一律为空字符串。
        #
        #    诊断（默认关闭）在这一层只做三件事：给这次执行起序号/计时、
        #    记下连续确认结论、记下"已请求复制"。识别本身完全不受影响：
        #    begin() 关闭时返回 None，下面的 note_* 在 trace 为 None 时全是空操作。
        trace = self._begin_diagnostics()
        started = time.perf_counter()
        elapsed_ms: Optional[float] = None
        diag_note = ""
        try:
            # ONNX 后端：把会话对齐到当前 ROI（其它引擎无此方法 → 空操作）。
            # 位置：裁剪之后、识别之前；且必须在 try **内部**，
            # 理由见 _ensure_engine_session 的 docstring。
            self._ensure_engine_session(roi, roi_image)
            candidate = self._recognizer.recognize_frame(roi_image)
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            text = candidate.code if candidate is not None else ""

            # 5a. ROI 样本采集（默认关闭）：采的正是上面这一步喂给识别器的
            #     那一帧像素，离线基准重跑引擎时与线上完全一致。
            #     纯旁路：采集器自己消化所有异常，绝不冒泡到这里。
            if self._sample_capture is not None:
                self._sample_capture.capture(
                    roi_image,
                    profile_id=getattr(self._profile, "profile_id", None)
                    or str(self._profile),
                    readings=(None if trace is None else
                              [(n.psm, n.raw_text) for n in trace.readings]),
                    candidate=text,
                    note=("游戏冲突"
                          if self._recognizer.last_conflict is not None else ""),
                )

            self._recognition_count += 1
            self._note_confirmation(trace, candidate)

            # 8. 选错游戏：识别器已经拦下这一帧（返回 None），因此 text 必为空，
            #    下面的剪贴板分支不可能走到。这里只负责提醒用户重新选择——
            #    不换档、不重启线程、不替用户做任何选择。
            if self._recognizer.last_conflict is not None:
                self._notify_game_mismatch()
            elif candidate is not None:
                # 本档认出了合法房间号：冲突已解除，允许下一次冲突再提醒
                self._mismatch_notified = False

            # 6. 只在文本变化时发送信号
            if text != self._last_text:
                self._last_text = text
                self.text_updated.emit(text)

                # 7. 请求复制到剪切板（由GUI线程执行）
                #    重复结果不重复触发剪贴板：同一个房间号已经成立过，
                #    只有真的换成另一个成立过的房间号才值得再复制一次。
                if text and candidate is not None and candidate.repeated:
                    logger.debug("[CLIPBOARD] 跳过重复结果: %s", text)
                elif text:
                    logger.info("[CLIPBOARD] copy request")
                    # 诊断：只记"请求了复制"，是否真的写进剪贴板由 GUI 线程
                    # 回填（见 MainWindow._on_copy_requested）。
                    if self._diagnostics is not None:
                        self._diagnostics.note_copy_requested(trace, text)
                    self.copy_requested.emit(text)

                # 记录识别结果
                if candidate is not None:
                    if candidate.corrected:
                        logger.info(
                            "[OCR] 识别结果: '%s' (第%d次, %s, 纠正%d处: %s)",
                            text, self._recognition_count, candidate.format_name,
                            candidate.correction_count, candidate.correction_summary(),
                        )
                    else:
                        logger.info(
                            "[OCR] 识别结果: '%s' (第%d次, %s)",
                            text, self._recognition_count, candidate.format_name,
                        )
                else:
                    logger.debug("[OCR] 识别结果: 空 (第%d次)", self._recognition_count)

            else:
                # 文本未变化，降低日志频率
                if self._recognition_count % 10 == 0:
                    logger.debug(
                        "[OCR] 文本未变化: '%s' (第%d次)",
                        text if text else "(空)", self._recognition_count
                    )

        except OCREngineError as e:
            diag_note = f"异常:{e}"
            self._error_count += 1
            self._last_error = str(e)
            logger.warning("OCR 识别失败: %s", e)
            self.error_occurred.emit(str(e))
            # 引擎进入不可恢复状态（例如 ONNX session 建不出来）：结束主循环，
            # 否则每 300ms 会重试一次昂贵且必然失败的初始化。
            # 其它引擎没有 is_fatal，行为与今天完全一致。
            if getattr(self._engine, "is_fatal", False):
                logger.error("OCR 引擎进入致命状态，停止主循环")
                self._running = False

        finally:
            # 无论成功、异常还是提前返回，这次执行都要收尾入库；
            # 关闭诊断时 trace 为 None，finish 直接返回。
            if self._diagnostics is not None:
                if elapsed_ms is None:
                    # 上面没走到计时那一步（异常）：用整段墙钟时间兜底，
                    # 标注为异常的同时给出"这次卡了多久"。
                    elapsed_ms = (time.perf_counter() - started) * 1000.0
                self._diagnostics.finish(trace, elapsed_ms=elapsed_ms, note=diag_note)

    def _begin_diagnostics(self):
        """开始一次诊断记录；诊断未接线或未开启时返回 None。

        游戏配置取自识别器已经解析好的 GameProfile（worker 里存的 _profile
        可能是 GameProfile 也可能是 id，识别器那边已经统一了）。
        """
        diagnostics = self._diagnostics
        if diagnostics is None:
            return None
        profile = None
        recognizer = self._recognizer
        if recognizer is not None:
            profile = getattr(recognizer, "profile", None)
        if profile is None:
            # 识别器还没建好（run() 会在进主循环前建好，理论上不会走到）：
            # 记不下游戏配置，但这次执行仍然照记，缺的字段留空。
            return diagnostics.begin()
        return diagnostics.begin(
            profile_id=profile.profile_id,
            formats=", ".join(
                f"{fmt.name}({fmt.describe()})" for fmt in profile.formats
            ),
        )

    def _note_confirmation(self, trace, candidate) -> None:
        """把这次执行的「连续确认结论 + 观测窗口快照」交给诊断。

        结论全部来自裁决器**已有的公开状态**，这里不重新推导一遍判定规则
        （否则就有了第二份"是否成立"的逻辑，迟早与裁决器不一致）：
            - 裁决器交出了候选  -> 成立；candidate.repeated 表示是已成立过的同一个码
            - 没有候选但有 pending_code -> 正在确认，连续一致次数还不够
            - 其余              -> 本帧没有合法候选可确认
        """
        diagnostics = self._diagnostics
        if diagnostics is None or trace is None:
            return

        resolver = self._resolver
        if candidate is not None:
            verdict = "重复成立" if candidate.repeated else "成立"
        elif getattr(resolver, "pending_code", None):
            verdict = "确认中"
        else:
            verdict = "未成立"

        # observations 是裁决器（TemporalResolver）的可选只读属性；
        # 无状态裁决器没有它就留空，不编造窗口。
        diagnostics.note_confirmation(
            trace, verdict, getattr(resolver, "observations", None))

    def _notify_game_mismatch(self) -> None:
        """提醒用户「游戏选错了」。

        一次冲突只提醒一次：提醒是给人看的，不是按帧刷的（识别间隔 300ms，
        每帧都弹一次会把界面废掉）。冲突解除（本档认出了合法房间号）或
        用户重新选择（set_profile）之后才允许再次提醒。

        只发信号，不带任何内部信息；文案由 GUI 决定。
        """
        if self._mismatch_notified:
            return
        self._mismatch_notified = True
        logger.warning("[OCR] 所选游戏与识别内容明显冲突，已提示用户重新选择")
        self.game_mismatch.emit()

    def _crop_roi(self, frame: np.ndarray, roi: ROI) -> np.ndarray:
        """
        从帧中裁剪 ROI 区域

        参数：
            frame: 原始帧
            roi: ROI 区域

        返回：
            裁剪后的图像
        """
        # 检查 ROI 是否在帧范围内
        frame_height, frame_width = frame.shape[:2]

        if (roi.x < 0 or roi.y < 0 or
            roi.x + roi.width > frame_width or
            roi.y + roi.height > frame_height):
            # ROI 超出范围，按比例缩放
            logger.warning(
                "ROI 超出帧范围: ROI=(%d,%d,%d,%d) Frame=%dx%d",
                roi.x, roi.y, roi.width, roi.height, frame_width, frame_height
            )

            # 尝试按比例缩放 ROI
            if roi.source_width > 0 and roi.source_height > 0:
                scaled_roi = roi.scale_to(frame_width, frame_height)
                return frame[
                    scaled_roi.y:scaled_roi.y + scaled_roi.height,
                    scaled_roi.x:scaled_roi.x + scaled_roi.width
                ]
            else:
                # 直接裁剪，可能部分超出
                x = max(0, roi.x)
                y = max(0, roi.y)
                width = min(roi.width, frame_width - x)
                height = min(roi.height, frame_height - y)
                return frame[y:y + height, x:x + width]

        # 正常裁剪
        return frame[roi.y:roi.y + roi.height, roi.x:roi.x + roi.width]

    @property
    def observe_all_psm(self) -> bool:
        """全 PSM 观测是否开启（默认 False）。只影响诊断，不影响任何判定。

        构造时固定、不提供运行期 setter：GUI 在识别运行期间会锁住该开关，
        改动一律通过「停止 → 改 → 重新开始」生效，避免识别器与界面状态不一致。
        """
        return self._observe_all_psm

    @property
    def length_escalation(self) -> bool:
        """长度异常复看是否开启（默认 False）。关闭时行为与从前逐字相同。

        与 observe_all_psm 一样构造时固定、不提供运行期 setter：改动一律通过
        「停止 → 改 → 重新开始」生效，避免识别器与界面状态不一致。
        """
        return self._length_escalation

    def set_interval(self, interval_ms: int) -> None:
        """
        设置识别间隔

        参数：
            interval_ms: 新的间隔时间（毫秒）
        """
        self._interval_ms = interval_ms
        logger.info("OCRWorker 间隔已更新: %dms", interval_ms)

    def set_profile(self, profile) -> None:
        """切换游戏档，只影响后续识别。

        刻意不重启线程、不重建引擎：
            - 不碰视频流（视频管道与本线程无关）
            - 引擎已存在时，重建识别器只是换一套格式/纠正规则，
              不加载 Tesseract，也不阻塞调用方（GUI 线程）
        若识别器尚未创建（线程刚启动还没到这一步），只记下档位，
        _ensure_recognizer() 会用新的档位构造。
        无论哪条路径都会清空时序裁决器的观测（旧格式的观测在换档后没有意义）。

        参数：
            profile: GameProfile 或 profile_id 字符串，必须是具体某一款游戏
                     （None / "auto" 抛 ValueError，见 _require_explicit_game()）

        异常：
            ValueError: 传入的不是具体游戏档。此时不改动任何状态，
                        线程继续按原档位识别。
        """
        # 先校验再改状态：切换失败不能让线程落进「没有档位」的中间态
        _require_explicit_game(profile)

        self._profile = profile

        # 换档后旧的时序状态不再有意义（房间号长度/格式都变了），
        # 一律清空，避免用上一款游戏的观测拖慢/污染新档的成立。
        self._resolver.reset()

        # 用户已经重新选择了：这是新的一轮，允许对新的选择再提醒一次
        self._mismatch_notified = False

        if self._recognizer is not None and self._engine is not None:
            # 先构造再赋值：工作线程最坏只会多识别一帧旧档
            self._recognizer = RoomCodeRecognizer(
                self._engine, profile=profile, scale=self._scale,
                resolver=self._resolver, diagnostics=self._diagnostics,
                observe_all_psm=self._observe_all_psm,
                length_escalation=self._length_escalation,
            )
            logger.info("OCR 游戏档已切换（识别器已更新，未重启线程）")
        else:
            logger.info("OCR 游戏档已记录（识别器尚未创建，将按新档位创建）")
