# -*- coding: utf-8 -*-
"""帧通知与桥接(自 gui/main_window.py 外迁,Phase 1 拆分)。

_FrameNotifier:frameReady 单槽通知(处理期间到达的帧合并成一次唤醒);
_FrameBridge:reader→buffer 的搬运循环(后台线程)。
兼容说明见 gui/playback_workers.py 的模块 docstring。
"""

import logging
import threading
import time

from PySide6.QtCore import QObject, Signal

from stream.ffmpeg_reader import FFmpegReader
from stream.frame_buffer import LatestFrameBuffer
from utils.performance import PerformanceMonitor

logger = logging.getLogger("DouyinLowLatencyViewer.gui.frame_bridge")

# ======================================================================
# 帧桥接线程：FFmpegReader → LatestFrameBuffer
# ======================================================================
class _FrameNotifier(QObject):
    """跨线程"帧已到达"通知器（Phase B-2）

    由 FrameBridge 子线程在写入 LatestFrameBuffer 之后调用 notify()，
    经 Qt 队列连接把 GUI 唤到 GUI 线程消费。设计要点：

    - **可合并**：上一通知未被消费前，后续通知只计数不再发信号，
      因此事件队列里最多只有一个待处理通知，不会无限堆积。
    - **不携带内容**：信号不带帧数据，GUI 被唤醒后自己重读
      LatestFrameBuffer 的当前最新帧，因此永远不会消费到旧帧。
    - 只发信号、不触碰任何 QWidget，子线程不会直接操作 GUI。
    """

    frameReady = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lock = threading.Lock()
        self._pending = False
        self.notify_count = 0      # 实际发出的通知数
        self.coalesced_count = 0   # 因 pending 未清而被合并掉的通知数

    def notify(self) -> None:
        """帧已写入 buffer：请求一次 GUI 唤醒（子线程调用，不阻塞）"""
        with self._lock:
            if self._pending:
                self.coalesced_count += 1
                return
            self._pending = True
            self.notify_count += 1
        self.frameReady.emit()

    def clear_pending(self) -> None:
        """GUI 线程的消费者在处理通知前调用，允许下一帧再次唤醒"""
        with self._lock:
            self._pending = False


class _FrameBridge:
    """
    后台线程：持续从 FFmpegReader 读取最新帧，写入 LatestFrameBuffer

    运行频率 1ms 轮询，保证 buffer 始终持有最新帧，
    即使 GUI 消费间隔内有多个新帧，buffer 只保留最新的，
    天然实现"宁可丢帧，不要累计延迟"。

    每次写入 buffer 后调用 perf.on_capture() 记录采集时间戳，
    用于后续计算 capture → display 延迟；随后通过 notifier 唤醒 GUI
    （不再依赖 GUI 侧的固定周期轮询）。
    """

    def __init__(
        self,
        reader: FFmpegReader,
        buffer: LatestFrameBuffer,
        perf: PerformanceMonitor,
    ):
        self._reader = reader
        self._buffer = buffer
        self._perf = perf
        self._running = False
        self._thread: threading.Thread | None = None
        self._last_reader_frame_id: int = -1
        # 每次连接新建一个：旧 bridge 停止后其通知器一并废弃，
        # 因此重连不会产生重复的信号连接。
        self.notifier = _FrameNotifier()

    def start(self):
        """启动桥接线程"""
        self._running = True
        self._last_reader_frame_id = -1
        self._thread = threading.Thread(
            target=self._run,
            name="frame-bridge",
            daemon=True,
        )
        self._thread.start()
        logger.info("FrameBridge 已启动")

    def stop(self):
        """停止桥接线程"""
        self._running = False
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._thread = None
        logger.info("FrameBridge 已停止")

    def _run(self):
        """桥接循环：reader → buffer + perf.on_capture()"""
        _log_count = 0
        while self._running:
            current_id = self._reader.frame_id
            if current_id != self._last_reader_frame_id and current_id > 0:
                frame = self._reader.read_frame()
                if frame is not None:
                    self._buffer.update(frame)
                    # 记录采集时间戳（在 buffer 写入后立即记录）
                    self._perf.on_capture()
                    # 唤醒 GUI 消费（可合并、不携带帧数据、不阻塞本线程）
                    self.notifier.notify()
                    self._last_reader_frame_id = current_id
                    _log_count += 1
                    if _log_count <= 3 or _log_count % 100 == 0:
                        logger.info(
                            "[BRIDGE] frame %d -> buffer: shape=%s dtype=%s",
                            _log_count, frame.shape, frame.dtype,
                        )

            time.sleep(0.001)


# ======================================================================