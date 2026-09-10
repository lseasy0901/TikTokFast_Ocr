# -*- coding: utf-8 -*-
"""
视频显示控件 (Phase 1 - Step 4)

功能：
    接收 numpy.ndarray (BGR) 帧 → 转为 RGB → QImage → QPixmap → 居中缩放绘制

约束：
    - 禁止 cv2.imshow()
    - 禁止保存图片
    - 仅依赖 PySide6 + numpy
"""

import logging

import numpy as np
from PySide6.QtCore import Qt, QRect
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap, QPen
from PySide6.QtWidgets import QWidget

_vw_logger = logging.getLogger("DouyinLowLatencyViewer.gui.video_widget")
_set_frame_count = 0


class VideoWidget(QWidget):
    """实时视频显示控件：numpy BGR 帧居中缩放渲染"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pixmap: QPixmap | None = None
        # 保持 numpy 数据引用，防止 QImage 底层缓冲被 GC 回收
        self._rgb_data: np.ndarray | None = None
        self._roi_rect: QRect | None = None
        self._video_size: tuple[int, int] = (0, 0)  # (width, height)
        self.setMinimumSize(320, 180)

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def set_frame(self, frame: np.ndarray) -> None:
        """
        设置并显示一帧视频

        :param frame: numpy.ndarray, 形状 (H, W, 3), dtype uint8, BGR 通道顺序
        """
        if frame is None or frame.size == 0:
            return

        global _set_frame_count
        _set_frame_count += 1
        if _set_frame_count <= 3 or _set_frame_count % 100 == 0:
            _vw_logger.info(
                "[DISPLAY] set_frame called: %dx%d count=%d",
                frame.shape[1], frame.shape[0], _set_frame_count,
            )

        # BGR → RGB（[::-1] 产生非连续视图，.copy() 确保连续内存）
        self._rgb_data = frame[:, :, ::-1].copy()

        h, w, ch = self._rgb_data.shape
        bytes_per_line = ch * w

        # QImage 引用 self._rgb_data 的内存（不拷贝），但 QPixmap.fromImage 会拷贝
        qimg = QImage(
            self._rgb_data.data,
            w, h,
            bytes_per_line,
            QImage.Format_RGB888,
        )

        self._pixmap = QPixmap.fromImage(qimg)
        self.update()  # 触发 paintEvent

    def clear_frame(self) -> None:
        """清除当前画面，恢复黑色背景"""
        self._pixmap = None
        self._rgb_data = None
        self._video_size = (0, 0)
        self.update()

    # ------------------------------------------------------------------
    # ROI 显示接口
    # ------------------------------------------------------------------
    def update_roi(self, roi):
        """更新ROI框（None表示隐藏）"""
        if roi is None:
            self._roi_rect = None
        else:
            # 将ROI转换为QRect
            self._roi_rect = QRect(roi.x, roi.y, roi.width, roi.height)
            # 记录视频尺寸
            self._video_size = (roi.source_width, roi.source_height)
        self.update()

    def clear_roi(self):
        """清除ROI框"""
        self._roi_rect = None
        self.update()

    # ------------------------------------------------------------------
    # 绘制
    # ------------------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)

        # 黑色背景
        painter.fillRect(self.rect(), QColor(0, 0, 0))

        if self._pixmap and not self._pixmap.isNull():
            # 保持宽高比缩放，适应控件尺寸
            scaled = self._pixmap.scaled(
                self.size(),
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
            # 居中绘制
            x = (self.width() - scaled.width()) // 2
            y = (self.height() - scaled.height()) // 2
            painter.drawPixmap(x, y, scaled)

            # 绘制ROI框（如果有）
            if self._roi_rect:
                # 获取原始帧尺寸
                source_w, source_h = self._video_size

                # 计算保持宽高比缩放后的实际绘制尺寸
                scaled = self._pixmap.scaled(
                    self.size(),
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation,
                )
                scaled_w = scaled.width()
                scaled_h = scaled.height()

                # 计算视频居中后的偏移量
                offset_x = x
                offset_y = y

                # 计算ROI在显示坐标系的矩形
                roi_x = offset_x + (self._roi_rect.x() * scaled_w) // source_w
                roi_y = offset_y + (self._roi_rect.y() * scaled_h) // source_h
                roi_w = (self._roi_rect.width() * scaled_w) // source_w
                roi_h = (self._roi_rect.height() * scaled_h) // source_h

                # 绘制红色矩形框
                pen = QPen(QColor(255, 0, 0))
                pen.setWidth(2)
                painter.setPen(pen)
                painter.drawRect(roi_x, roi_y, roi_w, roi_h)

        painter.end()
