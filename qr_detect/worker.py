# -*- coding: utf-8 -*-
"""QR 检测工作线程 —— 复刻原程序检测线程 [0x140013590] 的节奏与去重。

节奏（与原版一致）：
  - 无帧            → msleep(50)
  - 帧序号未变化     → msleep(1)
  - 有新帧          → 处理（可选 min_scan_interval_ms 限频，默认不限）

去重（与原版一致）：仅当解码内容 != 上一次**发射**的内容时才发信号；
内容相同的后续帧一律忽略。历史列表/计数由 UI 层维护。

依赖 PySide6（LiveLens 已带）。纯算法使用请直接用 qr_detect.detector。
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from PySide6.QtCore import QThread, Signal

from .detector import QrDetector, QRDetection


class QrDetectionWorker(QThread):
    """帧循环检测线程。

    参数：
        frame_getter: () -> (frame, seq) 或 None。frame 为 BGR24 numpy；
                      seq 为帧序号（LiveLens 的 LatestFrameBuffer.get_with_seq）。
        min_scan_interval_ms: 两次扫描最小间隔；0 = 每个新帧都扫（原版行为）。
    信号：
        qr_detected(object)  - QRDetection，内容与上次不同时发射
        error_occurred(str)  - 后端异常
    """

    qr_detected = Signal(object)
    error_occurred = Signal(str)

    IDLE_SLEEP_MS = 50      # 原版 msleep(50)
    SAME_FRAME_SLEEP_MS = 1  # 原版 msleep(1)

    def __init__(self, frame_getter: Callable, min_scan_interval_ms: int = 0,
                 parent=None) -> None:
        super().__init__(parent)
        self._frame_getter = frame_getter
        self._min_interval = max(int(min_scan_interval_ms), 0)
        self._stop = False
        self._last_emitted: Optional[bytes] = None

    # ------------------------------------------------------------------
    def stop(self, wait_ms: int = 45000) -> bool:
        """请求停止并等待。45 秒上限对应原版「检测线程未在 45 秒内结束」。"""
        self._stop = True
        if not self.isRunning():
            return True
        ok = self.wait(wait_ms)
        return ok

    def run(self) -> None:
        detector: Optional[QrDetector] = None
        try:
            detector = QrDetector()
        except Exception as e:  # DLL 缺失等
            self.error_occurred.emit(f"二维码检测器初始化失败: {e}")
            return

        last_seq = None
        last_scan_ts = 0.0
        try:
            while not self._stop:
                got = None
                try:
                    got = self._frame_getter()
                except Exception:
                    got = None

                if got is None:
                    self.msleep(self.IDLE_SLEEP_MS)
                    continue

                frame, seq = got
                if frame is None:
                    self.msleep(self.IDLE_SLEEP_MS)
                    continue
                if seq is not None and seq == last_seq:
                    self.msleep(self.SAME_FRAME_SLEEP_MS)
                    continue
                last_seq = seq

                if self._min_interval:
                    now = time.monotonic()
                    if (now - last_scan_ts) * 1000.0 < self._min_interval:
                        self.msleep(self.SAME_FRAME_SLEEP_MS)
                        continue
                    last_scan_ts = now

                try:
                    det: Optional[QRDetection] = detector.detect(frame)
                except Exception as e:
                    self.error_occurred.emit(f"二维码检测异常: {e}")
                    self.msleep(self.IDLE_SLEEP_MS)
                    continue

                if det is not None and det.content:
                    # 原版去重：与上一次发射内容全等比较
                    if det.content != self._last_emitted:
                        self._last_emitted = det.content
                        self.qr_detected.emit(det)
        finally:
            if detector:
                detector.close()
