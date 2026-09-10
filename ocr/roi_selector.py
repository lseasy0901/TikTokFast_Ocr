# -*- coding: utf-8 -*-
"""
ROI 区域选择器 (Phase 2.1)

功能：
    全屏弹出窗口，显示当前直播帧，用户鼠标拖动矩形框选 OCR 区域。

操作：
    - 左键拖动：框选区域
    - Enter：确认选择
    - ESC：取消选择（不改变旧 ROI）

信号：
    roi_selected(ROI)       — 确认选择时发出
    roi_selection_canceled  — 取消选择时发出
"""

import logging

import numpy as np
from PySide6.QtCore import Qt, Signal, QRect, QPoint
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QWidget

from ocr.roi_manager import ROI

logger = logging.getLogger("DouyinLowLatencyViewer.ocr.roi_selector")

__all__ = ["ROISelector"]


class ROISelector(QWidget):
    """
    ROI 区域选择窗口

    显示一帧视频画面，用户鼠标拖动框选矩形区域。
    选择完成后发出 roi_selected 信号。
    """

    # 信号
    roi_selected = Signal(object)       # 发出 ROI 实例
    roi_selection_canceled = Signal()

    # 选择框样式
    _BORDER_COLOR = QColor(0, 255, 128, 220)   # 绿色边框
    _FILL_COLOR = QColor(0, 255, 128, 40)      # 半透明填充

    def __init__(self, frame: np.ndarray, parent=None):
        """
        :param frame: numpy.ndarray (H, W, 3) BGR 帧
        :param parent: 父窗口
        """
        super().__init__(parent)

        self._source_frame = frame
        self._source_h, self._source_w = frame.shape[:2]

        # 选择状态
        self._selecting = False
        self._start_pos: QPoint | None = None
        self._end_pos: QPoint | None = None
        self._confirmed = False

        # 显示换算参数（paintEvent 中更新）
        self._display_offset = (0, 0)
        self._display_scale = (1.0, 1.0)

        # 显示用 QPixmap
        self._pixmap: QPixmap | None = None
        self._rgb_data: np.ndarray | None = None

        self._init_ui()
        self._render_frame()

    def _init_ui(self):
        """初始化窗口属性"""
        self.setWindowTitle("选择 OCR 识别区域 - 拖动框选 | Enter确认 | ESC取消")
        self.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint)
        self.setWindowState(Qt.WindowState.WindowMaximized)
        self.setCursor(Qt.CrossCursor)
        self.setFocusPolicy(Qt.StrongFocus)

    def _render_frame(self):
        """将 BGR 帧转为 QPixmap"""
        if self._source_frame is None:
            return
        # BGR → RGB
        self._rgb_data = self._source_frame[:, :, ::-1].copy()
        h, w, ch = self._rgb_data.shape
        bytes_per_line = ch * w
        qimg = QImage(
            self._rgb_data.data, w, h,
            bytes_per_line, QImage.Format_RGB888,
        )
        self._pixmap = QPixmap.fromImage(qimg)

    # ------------------------------------------------------------------
    # 鼠标事件
    # ------------------------------------------------------------------
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._selecting = True
            self._start_pos = event.position().toPoint()
            self._end_pos = self._start_pos
            self.update()

    def mouseMoveEvent(self, event):
        if self._selecting:
            self._end_pos = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._selecting:
            self._selecting = False
            self._end_pos = event.position().toPoint()
            self.update()

    # ------------------------------------------------------------------
    # 键盘事件
    # ------------------------------------------------------------------
    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            logger.info("ROI selection canceled (ESC)")
            self.roi_selection_canceled.emit()
            self.close()
        elif event.key() in (Qt.Key_Return, Qt.Key_Enter):
            roi = self._build_roi()
            if roi is not None:
                logger.info("ROI confirmed (Enter): %s", roi)
                self._confirmed = True
                self.roi_selected.emit(roi)
                self.close()

    # ------------------------------------------------------------------
    # 绘制
    # ------------------------------------------------------------------
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0))

        if self._pixmap and not self._pixmap.isNull():
            # 保持宽高比缩放
            scaled = self._pixmap.scaled(
                self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation,
            )
            x = (self.width() - scaled.width()) // 2
            y = (self.height() - scaled.height()) // 2
            self._display_offset = (x, y)
            self._display_scale = (
                scaled.width() / self._source_w,
                scaled.height() / self._source_h,
            )
            painter.drawPixmap(x, y, scaled)

            # 绘制选择框
            sel_rect = self._get_selection_rect()
            if sel_rect is not None:
                # 半透明填充
                painter.fillRect(sel_rect, self._FILL_COLOR)
                # 边框
                pen = QPen(self._BORDER_COLOR, 2, Qt.SolidLine)
                painter.setPen(pen)
                painter.drawRect(sel_rect)

                # 尺寸标注（源帧坐标）
                src_roi = self._display_to_source(sel_rect)
                if src_roi:
                    sx, sy, sw, sh = src_roi
                    label = f"{sw}x{sh} ({sx},{sy})"
                    painter.setPen(QColor(255, 255, 255, 220))
                    font = painter.font()
                    font.setPointSize(12)
                    painter.setFont(font)
                    painter.drawText(sel_rect.x() + 4, sel_rect.y() - 6, label)

            # 底部提示
            hint = "Enter 确认 | ESC 取消"
            painter.setPen(QColor(200, 200, 200, 180))
            font = painter.font()
            font.setPointSize(11)
            painter.setFont(font)
            painter.drawText(self.width() // 2 - 80, self.height() - 20, hint)

        painter.end()

    # ------------------------------------------------------------------
    # 坐标换算
    # ------------------------------------------------------------------
    def _get_selection_rect(self) -> QRect | None:
        """归一化选择矩形（确保宽高为正，最小 5px）"""
        if self._start_pos is None or self._end_pos is None:
            return None
        x1, y1 = self._start_pos.x(), self._start_pos.y()
        x2, y2 = self._end_pos.x(), self._end_pos.y()
        x, y = min(x1, x2), min(y1, y2)
        w, h = abs(x2 - x1), abs(y2 - y1)
        if w < 5 or h < 5:
            return None
        return QRect(x, y, w, h)

    def _display_to_source(self, rect: QRect) -> tuple | None:
        """显示坐标 → 源帧坐标 (x, y, w, h)"""
        ox, oy = self._display_offset
        sx, sy = self._display_scale
        if sx <= 0 or sy <= 0:
            return None
        x = int((rect.x() - ox) / sx)
        y = int((rect.y() - oy) / sy)
        w = int(rect.width() / sx)
        h = int(rect.height() / sy)
        # 钳位
        x = max(0, min(x, self._source_w - 1))
        y = max(0, min(y, self._source_h - 1))
        w = max(1, min(w, self._source_w - x))
        h = max(1, min(h, self._source_h - y))
        return (x, y, w, h)

    def _build_roi(self) -> ROI | None:
        """根据当前选择矩形构建 ROI"""
        sel_rect = self._get_selection_rect()
        if sel_rect is None:
            return None
        src = self._display_to_source(sel_rect)
        if src is None:
            return None
        x, y, w, h = src
        return ROI(
            x=x, y=y, width=w, height=h,
            source_width=self._source_w,
            source_height=self._source_h,
        )
