# -*- coding: utf-8 -*-
"""校准引导小窗:置顶窄条显示当前步骤,不挡游戏点击。

设计:校准是全局动作(用户要到游戏里真实点击三次),引导必须是
**非模态 + 置顶 + 贴角落**:模态会拦住应用内交互,居中大窗会挡游戏;
300×72 的角落小条两边都不碍事。取消按钮之外整条无交互面,
游戏点击不会被它吃掉。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from gui.theme import Metrics, Palette

__all__ = ["JoinGuideBar"]

_STYLESHEET = f"""
#joinGuide {{
    background-color: {Palette.POPUP_BG};
    border: 1px solid {Palette.ACCENT_EDGE};
    border-radius: {Metrics.CONTROL_RADIUS}px;
}}
#joinGuideStep {{
    color: {Palette.ACCENT_TEXT};
    font-size: 12px;
    font-weight: 600;
}}
#joinGuidePrompt {{
    color: {Palette.TEXT};
    font-size: 12px;
}}
"""


class JoinGuideBar(QWidget):
    """置顶引导条:步骤序号 + 提示 + 取消。close() 即取消。"""

    #: 用户点了取消
    cancelled = None  # 由调用方直接 connect button

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent, Qt.Tool | Qt.FramelessWindowHint
                         | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_ShowWithoutActivating)   # 不抢焦点
        self.setObjectName("joinGuideRoot")
        self.setFixedWidth(360)
        self.setStyleSheet(_STYLESHEET)

        row = QHBoxLayout(self)
        row.setContentsMargins(14, 10, 10, 10)
        row.setSpacing(10)

        self._step = QLabel("1/2")
        self._step.setObjectName("joinGuideStep")
        row.addWidget(self._step)

        self._prompt = QLabel()
        self._prompt.setObjectName("joinGuidePrompt")
        self._prompt.setWordWrap(True)
        row.addWidget(self._prompt, 1)

        cancel = QPushButton("取消")
        cancel.setObjectName("statusButton")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(self.close)
        row.addWidget(cancel)

    # ------------------------------------------------------------------
    def show_step(self, index: int, prompt: str) -> None:
        """显示第 index(0 基)步并贴到主屏右上角(避开游戏主区域)。"""
        from automation.calibration import STEP_PROMPTS
        self._step.setText(f"{index + 1}/{len(STEP_PROMPTS)}")
        self._prompt.setText(prompt)
        self.adjustSize()
        screen = self.screen() or QWidget.screen(self)
        geo = screen.availableGeometry()
        self.move(geo.right() - self.width() - 16, geo.top() + 16)
        if not self.isVisible():
            self.show()
        self.raise_()

    def show_message(self, text: str) -> None:
        """临时覆盖提示(点偏了/即将结束);1.5s 后由调用方切回步骤文案。"""
        self._prompt.setText(text)
        self.adjustSize()
