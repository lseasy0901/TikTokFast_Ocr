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
import threading
import time
from typing import Optional

import numpy as np
from PySide6.QtCore import QThread, Signal

from .engine import OCREngine, OCREngineError
from .profiles import AUTO, get_profile
from .roi_manager import ROI, ROIManager
from .room_code import DEFAULT_SCALE, RoomCodeRecognizer
from .temporal import TemporalResolver
# from .clipboard import ClipboardManager

logger = logging.getLogger("DouyinLowLatencyViewer.ocr.worker")

__all__ = ["OCRWorker"]


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
        - 每 100ms 识别一次
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

    def __init__(self,
                 frame_buffer_getter,
                 roi_manager: ROIManager,
                 engine: Optional[OCREngine] = None,
                 interval_ms: int = 300,
                 profile=None,
                 scale: float = DEFAULT_SCALE,
                 recognizer: Optional[RoomCodeRecognizer] = None,
                 resolver=None):
        """
        初始化 OCR 工作线程

        参数：
            frame_buffer_getter: 获取帧缓冲区的回调函数
                                 返回: LatestFrameBuffer 实例
            roi_manager: ROI 管理器实例
            engine: OCR 引擎实例（为 None 时在【工作线程内】延迟创建）
            interval_ms: 识别间隔（毫秒，默认 300）
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
        """
        super().__init__()

        # 正式路径必须由用户选定游戏：不猜、也不静默回落（先校验再改动任何状态）
        _require_explicit_game(profile)

        self._frame_buffer_getter = frame_buffer_getter
        self._roi_manager = roi_manager
        # 构造 OCRWorker 绝不构造 OCREngine：OCREngine() 会定位并探测
        # Tesseract 可执行文件，打包环境下首次启动需冷加载大量 DLL，
        # 放在 GUI 线程会让点击「识别」后界面卡死。延迟到 run() 内构造。
        self._engine = engine
        self._interval_ms = interval_ms
        self._profile = profile
        self._scale = scale
        # 同上：识别器也延迟到工作线程内构造（它依赖引擎）
        self._recognizer = recognizer
        # 候选裁决器：默认启用时序一致性（OCR V2.3），构造它是纯内存操作，
        # 不触碰 Tesseract，因此放在这里（GUI 线程）是安全的。
        self._resolver = TemporalResolver() if resolver is None else resolver

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

    def _ensure_engine(self) -> OCREngine:
        """在工作线程内延迟构造 OCR 引擎。

        引擎构造会定位 Tesseract（打包环境下首次启动需冷加载上百 MB 的 DLL，
        还可能触发杀软扫描），因此【必须】发生在工作线程，
        而不是点击「识别」的 GUI 线程调用链上。
        """
        if self._engine is None:
            self._engine = OCREngine()
            logger.info("OCR 引擎已在工作线程内创建")
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
            )
            logger.info("房间号识别器已在工作线程内创建")
        return self._recognizer

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
            logger.info("OCRWorker 主循环结束")
            return

        while self._running and not self._stopped_event.is_set():
            try:
                self._perform_recognition()

            except Exception as e:
                self._error_count += 1
                error_msg = f"OCR 识别异常: {e}"
                logger.error(error_msg, exc_info=True)
                self._last_error = error_msg
                self.error_occurred.emit(error_msg)

            # 等待指定间隔
            self._stopped_event.wait(self._interval_ms / 1000.0)

        logger.info("OCRWorker 主循环结束")

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
        try:
            candidate = self._recognizer.recognize_frame(roi_image)
            text = candidate.code if candidate is not None else ""

            self._recognition_count += 1

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
            self._error_count += 1
            self._last_error = str(e)
            logger.warning("OCR 识别失败: %s", e)
            self.error_occurred.emit(str(e))

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
                resolver=self._resolver,
            )
            logger.info("OCR 游戏档已切换（识别器已更新，未重启线程）")
        else:
            logger.info("OCR 游戏档已记录（识别器尚未创建，将按新档位创建）")
