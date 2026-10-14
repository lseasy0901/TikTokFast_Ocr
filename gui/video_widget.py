# -*- coding: utf-8 -*-
"""
视频显示控件 (Phase 1 - Step 4)

功能：
    接收 numpy.ndarray (BGR) 帧 → 以 BGR 布局直接包装为 QImage → QPixmap → 居中缩放绘制

约束：
    - 禁止 cv2.imshow()
    - 禁止保存图片
    - 仅依赖 PySide6 + numpy
"""

import logging
import time

import numpy as np
from PySide6.QtCore import Qt, QRect
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap, QPen
from PySide6.QtWidgets import QWidget

from utils.diag_trace import TRACE, S_SET, S_PAINT

_vw_logger = logging.getLogger("DouyinLowLatencyViewer.gui.video_widget")
_set_frame_count = 0


class VideoWidget(QWidget):
    """实时视频显示控件：numpy BGR 帧居中缩放渲染"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pixmap: QPixmap | None = None
        # 保持 numpy 数据引用，防止 QImage 底层缓冲被 GC 回收
        self._frame_data: np.ndarray | None = None
        self._roi_rect: QRect | None = None
        self._video_size: tuple[int, int] = (0, 0)  # (width, height)
        #: H1 诊断：最近一次提交的帧来源帧号（用于把 set_frame 与 paintEvent 关联到同一帧）
        self._last_seq: int | None = None
        self.setMinimumSize(320, 180)
        # Phase B-1：可选的耗时观测接收方（只记录，不参与任何渲染决策）
        self._perf = None

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def attach_monitor(self, perf) -> None:
        """挂接/解挂性能监控器（传 None 解挂）。

        只用于把 set_frame / paintEvent 的耗时汇总给既有性能面板，
        不改变任何渲染行为。仅应在 GUI 线程调用。
        """
        self._perf = perf
    def set_frame(self, frame: np.ndarray, seq: int | None = None) -> None:
        """
        设置并显示一帧视频

        :param frame: numpy.ndarray, 形状 (H, W, 3), dtype uint8, BGR 通道顺序
        :param seq:   该帧的来源帧号（FFmpegReader.frame_id）。仅用于 H1 诊断
                      关联；不参与任何渲染决策。
        """
        if frame is None or frame.size == 0:
            return
        self._last_seq = seq

        # Phase B-1：只计量耗时，不改变下面的转换/绘制顺序
        _t0 = time.perf_counter()

        global _set_frame_count
        _set_frame_count += 1
        if _set_frame_count <= 3 or _set_frame_count % 100 == 0:
            _vw_logger.info(
                "[DISPLAY] set_frame called: %dx%d count=%d",
                frame.shape[1], frame.shape[0], _set_frame_count,
            )

        # QImage 原生支持 BGR 字节序，直接按原布局包装即可：
        # 省掉整帧 BGR→RGB 拷贝（1080p 下约 5ms/帧），像素语义完全一致。
        # 数组必须是 C 连续的，QImage 才能按 bytes_per_line 正确寻址；
        # 生产路径传入的帧恒为连续数组（frombuffer → reshape），此处不触发拷贝。
        if not frame.flags["C_CONTIGUOUS"]:
            frame = np.ascontiguousarray(frame)

        # 保留引用：QImage 只引用不拥有这块内存
        self._frame_data = frame

        h, w, ch = frame.shape
        bytes_per_line = ch * w

        # QImage 引用 self._frame_data 的内存（不拷贝），但 QPixmap.fromImage 会拷贝
        qimg = QImage(
            self._frame_data.data,
            w, h,
            bytes_per_line,
            QImage.Format_BGR888,
        )

        self._pixmap = QPixmap.fromImage(qimg)
        self.update()  # 触发 paintEvent

        if self._perf is not None:
            self._perf.on_set_frame((time.perf_counter() - _t0) * 1000.0)

        # H1 诊断：set_frame 返回 = QImage/QPixmap 构造完成（尚未绘制）
        if TRACE.enabled:
            TRACE.note(S_SET, seq)
            TRACE.bump("c_set")

    def clear_frame(self) -> None:
        """清除当前画面，恢复黑色背景"""
        self._pixmap = None
        self._frame_data = None
        self._video_size = (0, 0)
        self.update()

    # ------------------------------------------------------------------
    # 只读几何查询
    # ------------------------------------------------------------------
    def visible_video_rect(self) -> QRect:
        """当前**实际显示视频画面**的矩形（本控件坐标系）。

        纯几何只读访问器：复用 paintEvent 里同一套 KeepAspectRatio 居中算法，
        但不绘制、不改状态、不参与任何帧处理，也不做像素缩放。

        用途：预览区上方的浮层（如未激活水印）需要把自身约束在**画面**内，
        而不是落在上下/左右黑边上。

        - 没有帧时返回 ``self.rect()`` —— 此时"可视预览区"就是整个容器，
          未连接直播时水印在空白背景上运动即依赖这一条。
        - 尺寸用 ``QSize.scaled(..., KeepAspectRatio)`` 计算，与
          ``QPixmap.scaled`` 内部用的是同一个函数，因此结果与 paintEvent
          里实际绘制的矩形一致（不是近似）。
        """
        if self._pixmap is None or self._pixmap.isNull():
            return self.rect()
        scaled = self._pixmap.size().scaled(self.size(), Qt.KeepAspectRatio)
        return QRect(
            (self.width() - scaled.width()) // 2,
            (self.height() - scaled.height()) // 2,
            scaled.width(),
            scaled.height(),
        )

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
        # Phase B-1：计量整个 paintEvent（进入 → 绘制完成），不只测 scaled()
        _t0 = time.perf_counter()
        _drew_frame = bool(self._pixmap and not self._pixmap.isNull())

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

                # 复用上面已经算好的缩放结果，不重复执行相同的 scaled()
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

        if self._perf is not None:
            self._perf.on_paint_event(
                (time.perf_counter() - _t0) * 1000.0, _drew_frame
            )

        # H1 诊断：本次 paintEvent 绘制的是"最近一次提交的帧"。
        # 注意：painter.end() 只代表**绘制调用完成**，不代表像素已经上屏
        # （合成由 DWM 完成，进程内不可观测）——该阶段一律标记为 UNKNOWN。
        if TRACE.enabled and self._last_seq is not None:
            # 仅记录**绘制了帧**的 paintEvent：启动期无帧重绘（_last_seq=None）
            # 不携带帧关联，计入 no_seq 会污染 assoc_fail 语义。
            TRACE.note_paint(self._last_seq)
            TRACE.note(S_PAINT, self._last_seq)
            TRACE.bump("c_paint")
