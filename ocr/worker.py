# -*- coding: utf-8 -*-
"""
OCR 工作线程 (Phase 2.3)

实现实时 OCR 识别，不阻塞 GUI 主线程。

功能：
    - 从 LatestFrameBuffer 获取最新帧
    - 根据 ROIManager 裁剪识别区域
    - 调用 OCREngine 执行识别
    - 定时识别（默认 100ms）
    - 只在文本变化时发送信号
"""

import logging
import threading
import time
from typing import Optional

import numpy as np
from PySide6.QtCore import QThread, Signal

from .engine import OCREngine, OCREngineError
from .roi_manager import ROI, ROIManager
# from .clipboard import ClipboardManager

logger = logging.getLogger("DouyinLowLatencyViewer.ocr.worker")

__all__ = ["OCRWorker"]


class OCRWorker(QThread):
    """
    OCR 识别工作线程

    在后台线程中定期执行 OCR 识别，避免阻塞 GUI。
    识别完成后通过信号通知主线程。

    信号：
        text_updated(str) - 当识别的文本发生变化时发送

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

    def __init__(self,
                 frame_buffer_getter,
                 roi_manager: ROIManager,
                 engine: Optional[OCREngine] = None,
                 interval_ms: int = 300):
        """
        初始化 OCR 工作线程

        参数：
            frame_buffer_getter: 获取帧缓冲区的回调函数
                                 返回: LatestFrameBuffer 实例
            roi_manager: ROI 管理器实例
            engine: OCR 引擎实例（为 None 时自动创建）
            interval_ms: 识别间隔（毫秒，默认 300）
        """
        super().__init__()

        self._frame_buffer_getter = frame_buffer_getter
        self._roi_manager = roi_manager
        self._engine = engine if engine else OCREngine()
        self._interval_ms = interval_ms

        # 注释掉剪切板管理器，改为由GUI线程处理
        # self._clipboard_manager = ClipboardManager()

        # 线程控制
        self._running = False
        self._stopped_event = threading.Event()

        # 状态跟踪
        self._last_text: Optional[str] = None
        self._recognition_count: int = 0
        self._error_count: int = 0
        self._last_error: Optional[str] = None

        logger.info(
            "OCRWorker 初始化: 间隔=%dms, 引擎=%s",
            interval_ms, "自定义" if engine else "默认"
        )

    def start_recognition(self) -> None:
        """启动 OCR 识别"""
        if self._running:
            logger.warning("OCRWorker 已经在运行")
            return

        self._running = True
        self._stopped_event.clear()
        self._last_text = None
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

    def run(self) -> None:
        """OCR 识别主循环（在后台线程中执行）"""
        logger.info("OCRWorker 主循环开始")

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

        # 5. 执行 OCR 识别
        try:
            text = self._engine.recognize(roi_image, preprocess=True)
            text = text.strip()

            self._recognition_count += 1

            # 6. 只在文本变化时发送信号
            if text != self._last_text:
                self._last_text = text
                self.text_updated.emit(text)

                # 7. 请求复制到剪切板（由GUI线程执行）
                if text:
                    logger.info("[CLIPBOARD] copy request")
                    self.copy_requested.emit(text)

                # 记录识别结果
                if text:
                    logger.info(
                        "[OCR] 识别结果: '%s' (第%d次)",
                        text, self._recognition_count
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
