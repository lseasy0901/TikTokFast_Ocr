# -*- coding: utf-8 -*-
"""识别到新二维码时的置顶弹窗 —— 复刻原程序 QRPopupDialog [0x140015100]。

原版行为：
  - QDialog 600×700 固定尺寸，加 WindowStaysOnTopHint
  - 深色样式（#0c1018 底 + 金边）
  - 标题「识别到新二维码」（20px 粗体）
  - QrImageWidget：qrencode 重新生成的清晰二维码，500px（int(dpr*500+0.5)）
  - 内容文本（13px，自动换行）
  - 一个渐变主按钮（点击复制内容；内容是链接时提供「打开」）
  - QTimer 自动关闭

原版未完全还原的点（做成参数）：
  - 自动关闭时长（原版 singleShot 的毫秒数未能从反汇编唯一确定）
  - 按钮文字（原版字面量嵌在代码里未提取干净）
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QDialog, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from .encode_backend import encode_qr_matrix, matrix_to_image

STYLE_DIALOG = (
    "QDialog {background-color: #0c1018;border: 1px solid rgba(212, 175, 55, 0.25);"
    "border-radius: 12px;}"
)
STYLE_TITLE = (
    "font-size: 20px; font-weight: 700; color: #f1f5f9;"
    "background: transparent; border: none; padding: 8px; letter-spacing: 0.02em;"
)
STYLE_CONTENT = (
    "font-size: 13px; color: #cbd5e1;background-color: rgba(15, 23, 42, 0.95);"
    "border: 1px solid rgba(148, 163, 184, 0.25);border-radius: 10px; padding: 12px 14px;"
)
STYLE_BUTTON = (
    "QPushButton {background: qlineargradient(x1:0, y1:0, x2:1, y2:1, "
    "stop:0 #14b8a6, stop:1 #0d9488);color: #ecfdf5; font-size: 15px; font-weight: 700;"
    "padding: 12px 24px; border-radius: 8px;min-width: 200px;"
    "border: 1px solid rgba(45, 212, 191, 0.4);}"
    "QPushButton:hover {background: qlineargradient(x1:0, y1:0, x2:1, y2:1, "
    "stop:0 #2dd4bf, stop:1 #14b8a6);}"
)


class QrPopupDialog(QDialog):
    """识别结果弹窗。自动关闭默认 15 秒（原版时长未确定，可调）。"""

    WIDTH = 600
    HEIGHT = 700
    QR_PX = 500  # 原版 int(dpr*500+0.5)，1x 屏即 500
    DEFAULT_AUTO_CLOSE_MS = 15000

    def __init__(self, content: bytes, parent: Optional[QWidget] = None,
                 auto_close_ms: Optional[int] = DEFAULT_AUTO_CLOSE_MS,
                 show_open_button: bool = True) -> None:
        super().__init__(parent)
        text = content.decode("utf-8", "replace").strip()
        if not text:
            raise ValueError("空内容不弹窗（与原版一致）")

        self._content = content
        self._text = text

        self.setWindowTitle("新二维码")
        self.setFixedSize(self.WIDTH, self.HEIGHT)
        flags = self.windowFlags()
        self.setWindowFlags(flags | Qt.WindowStaysOnTopHint)
        self.setStyleSheet(STYLE_DIALOG)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)

        title = QLabel("识别到新二维码")
        title.setStyleSheet(STYLE_TITLE)
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        # —— 重生成的大二维码（原版 QrImageWidget + qrencode ECC-H）——
        qr_label = QLabel()
        qr_label.setAlignment(Qt.AlignCenter)
        img = self._render_qr()
        if img is None:
            qr_label.setText("生成二维码失败")
        else:
            from PySide6.QtGui import QImage, QPixmap
            import numpy as np
            arr = np.asarray(img)
            h, w, ch = arr.shape
            qimg = QImage(arr.data, w, h, w * ch, QImage.Format_RGB888)
            qr_label.setPixmap(QPixmap.fromImage(qimg).copy())  # copy 脱离 numpy 生命周期
        layout.addWidget(qr_label, 1)

        content_label = QLabel(text)
        content_label.setStyleSheet(STYLE_CONTENT)
        content_label.setWordWrap(True)
        content_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(content_label)

        copy_btn = QPushButton("复制内容")
        copy_btn.setStyleSheet(STYLE_BUTTON)
        copy_btn.setCursor(Qt.PointingHandCursor)
        copy_btn.clicked.connect(self._copy_content)
        layout.addWidget(copy_btn, 0, Qt.AlignCenter)

        if show_open_button and self._is_url(text):
            open_btn = QPushButton("在浏览器打开")
            open_btn.setStyleSheet(
                STYLE_BUTTON.replace("#14b8a6", "#3b82f6").replace("#0d9488", "#1d4ed8")
                .replace("#2dd4bf", "#60a5fa")
            )
            open_btn.setCursor(Qt.PointingHandCursor)
            open_btn.clicked.connect(self._open_url)
            layout.addWidget(open_btn, 0, Qt.AlignCenter)

        if auto_close_ms and auto_close_ms > 0:
            QTimer.singleShot(auto_close_ms, self._auto_close)

    # ------------------------------------------------------------------
    def _render_qr(self):
        ret = encode_qr_matrix(self._content)
        if ret is None:
            return None
        _version, matrix = ret
        img, _total = matrix_to_image(matrix, target_px=self.QR_PX)
        return img

    @staticmethod
    def _is_url(text: str) -> bool:
        low = text.lower()
        return low.startswith(("http://", "https://"))

    def _copy_content(self) -> None:
        QGuiApplication.clipboard().setText(self._text)
        self.setWindowTitle("已复制")

    def _open_url(self) -> None:
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl
        QDesktopServices.openUrl(QUrl(self._text))

    def _auto_close(self) -> None:
        if self.isVisible():
            self.accept()
