# -*- coding: utf-8 -*-
"""轻量 toast 通知（右下角浮出，自动消退）。

统一"用户需要知道但不需要被打断"的事件反馈：连接成功 / 失败、复制成功、
导出完成等。此前这类信息只有状态栏左侧的瞬时文字（不显眼、无语气区分）
或散落的自定义浮层；本组件是它们的标准载体。

设计约束：
    - 不抢焦点（Qt.ToolTip）、不进任务栏、不挡状态栏按钮的点击。
    - 同一时刻只有一条：重复调用替换内容并重置计时（不做队列，
      连接类事件低频，排队反而积压旧消息）。
    - 语气 = 状态圆点颜色（复用 theme.tone_color），与状态栏芯片同一语汇。
"""

from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, QTimer, Qt
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QWidget

from gui.theme import tone_color

__all__ = ["Toast"]

_SHOW_MS = 2400
_FADE_MS = 220


class Toast(QFrame):
    """单条 toast。由主窗口持有一只实例，调 :meth:`popup` 即可。"""

    def __init__(self, parent: QWidget):
        super().__init__(parent, Qt.ToolTip | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self._label = QLabel()
        self._label.setObjectName("toastLabel")
        self._label.setProperty("tone", "neutral")
        lay.addWidget(self._label)

        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(_FADE_MS)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._fade_out)

        self.hide()

    def popup(self, text: str, tone: str = "neutral") -> None:
        """显示一条 toast；已在显示则原位替换并重新计时。"""
        color = tone_color(tone)
        self._label.setText(f'<span style="color:{color};">●</span>  {text}')
        self._label.setProperty("tone", tone)
        self._label.style().unpolish(self._label)
        self._label.style().polish(self._label)
        self._label.adjustSize()
        self.adjustSize()
        self._place()
        self._fade.stop()
        self.setWindowOpacity(0.0)
        self.show()
        self.raise_()
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()
        self._hide_timer.start(_SHOW_MS + _FADE_MS)

    def _place(self) -> None:
        """贴在宿主窗口右下角、状态栏上方 12px。"""
        host = self.parent()
        if host is None:
            return
        x = host.x() + host.width() - self.width() - 20
        y = host.y() + host.height() - self.height() - 44
        screen = QApplication.screenAt(QPoint(x + self.width() // 2,
                                              y + self.height() // 2))
        if screen is not None:
            g = screen.geometry()
            x = max(g.left() + 8, min(x, g.right() - self.width() - 8))
            y = max(g.top() + 8, y)
        self.move(x, y)

    def _fade_out(self) -> None:
        self._fade.stop()
        self._fade.setStartValue(1.0)
        self._fade.setEndValue(0.0)
        try:
            self._fade.finished.disconnect()
        except RuntimeError:
            pass
        self._fade.finished.connect(self.hide)
        self._fade.start()
