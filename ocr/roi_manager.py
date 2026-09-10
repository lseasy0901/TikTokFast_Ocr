# -*- coding: utf-8 -*-
"""
ROI 数据结构与区域管理器 (Phase 2.1)

ROI：
    矩形区域坐标，包含源帧尺寸用于未来缩放适配。

ROIManager：
    线程安全的 ROI 存取管理器。
    被主窗口和未来 OCR 引擎共用。
"""

import logging
import threading

logger = logging.getLogger("DouyinLowLatencyViewer.ocr.roi_manager")

__all__ = ["ROI", "ROIManager"]


class ROI:
    """
    OCR 识别区域（矩形）

    属性：
        x, y          — 矩形左上角坐标（像素，基于 source_width/height）
        width, height — 矩形尺寸（像素）
        source_width  — 源帧宽度（用于窗口缩放时比例换算）
        source_height — 源帧高度
    """

    def __init__(self, x: int, y: int, width: int, height: int,
                 source_width: int, source_height: int):
        self.x = int(x)
        self.y = int(y)
        self.width = int(width)
        self.height = int(height)
        self.source_width = int(source_width)
        self.source_height = int(source_height)

    def to_dict(self) -> dict:
        """序列化为 dict（用于保存/日志）"""
        return {
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "source_width": self.source_width,
            "source_height": self.source_height,
        }

    def scale_to(self, target_width: int, target_height: int) -> "ROI":
        """
        按目标尺寸等比缩放 ROI（用于显示窗口与源帧尺寸不一致时）

        :return: 新的 ROI 实例，坐标按比例换算到 target 尺寸
        """
        if self.source_width <= 0 or self.source_height <= 0:
            return ROI(self.x, self.y, self.width, self.height,
                       target_width, target_height)
        sx = target_width / self.source_width
        sy = target_height / self.source_height
        return ROI(
            x=int(self.x * sx),
            y=int(self.y * sy),
            width=int(self.width * sx),
            height=int(self.height * sy),
            source_width=target_width,
            source_height=target_height,
        )

    def __repr__(self):
        return (f"ROI(x={self.x}, y={self.y}, w={self.width}, h={self.height}, "
                f"src={self.source_width}x{self.source_height})")


class ROIManager:
    """
    线程安全的 ROI 管理器

    - set_roi()   — 设置/更新 ROI
    - get_roi()   — 获取当前 ROI（无则返回 None）
    - clear_roi() — 清除 ROI
    - has_roi     — 是否已设置 ROI
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._roi: ROI | None = None

    def set_roi(self, roi: ROI) -> None:
        """设置 ROI（覆盖旧值）"""
        with self._lock:
            self._roi = roi
        logger.info("ROI set: %s", roi)

    def get_roi(self) -> ROI | None:
        """获取当前 ROI"""
        with self._lock:
            return self._roi

    def clear_roi(self) -> None:
        """清除 ROI"""
        with self._lock:
            old = self._roi
            self._roi = None
        if old is not None:
            logger.info("ROI cleared (was: %s)", old)

    @property
    def has_roi(self) -> bool:
        """是否已设置 ROI"""
        with self._lock:
            return self._roi is not None
