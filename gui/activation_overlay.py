# -*- coding: utf-8 -*-
"""未激活遮罩(自 gui/main_window.py 外迁,Phase 1 拆分)。

含品牌呼吸动画(挂起/恢复语义见 set_suspended:最小化与应用失活都暂停,
Paused→resume 保相位)与「立即激活 / 购买授权」入口。
"""

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Signal, Qt
from PySide6.QtWidgets import (QGraphicsOpacityEffect, QHBoxLayout, QLabel,
                               QPushButton, QVBoxLayout, QWidget)
from PySide6.QtGui import QIcon

from gui.theme import Palette
from gui.widgets import IconButton, icon_pixmap

class _ActivationOverlay(QWidget):
    """视频区上方的未激活遮罩：品牌 + 轻呼吸 + 「请激活 LiveLens」+ 激活入口。

    纯展示：只发一个 clicked 信号，不调用任何生命周期方法；呼吸动画用 Qt 原生
    属性动画，只在可见时运行，隐藏即停（不引入周期性定时器，也不碰视频线程）。
    """

    activate_clicked = Signal()
    purchase_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("activationOverlay")
        self.setAttribute(Qt.WA_StyledBackground, True)

        col = QVBoxLayout(self)
        col.setContentsMargins(24, 24, 24, 24)
        col.setSpacing(10)
        col.addStretch(1)

        self._brand = QLabel("LiveLens")
        self._brand.setObjectName("activationBrand")
        self._brand.setAlignment(Qt.AlignmentFlag.AlignCenter)
        col.addWidget(self._brand)

        self._breath = QGraphicsOpacityEffect(self._brand)
        self._brand.setGraphicsEffect(self._breath)
        self._anim = QPropertyAnimation(self._breath, b"opacity", self)
        self._anim.setDuration(2400)
        self._anim.setStartValue(0.55)
        self._anim.setEndValue(1.0)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutSine)
        self._anim.setLoopCount(-1)

        self._prompt = QLabel("请激活 LiveLens")
        self._prompt.setObjectName("activationPrompt")
        self._prompt.setAlignment(Qt.AlignmentFlag.AlignCenter)
        col.addWidget(self._prompt)

        row = QHBoxLayout()
        row.setSpacing(10)
        row.addStretch(1)
        self._activate_btn = QPushButton("立即激活")
        self._activate_btn.setFixedHeight(34)
        self._activate_btn.setMinimumWidth(120)
        self._activate_btn.setProperty("variant", "primary")
        self._activate_btn.clicked.connect(self.activate_clicked.emit)
        row.addWidget(self._activate_btn)

        # 次要入口：尚未激活的人同样需要能买到授权，因此放在「立即激活」旁边，
        # 但保持次要样式 —— 主操作仍然只有激活一个。点击直接调系统默认浏览器
        # 打开购买链接（RELEASE-01）。
        self._contact_btn = QPushButton("购买授权")
        self._contact_btn.setFixedHeight(34)
        self._contact_btn.setMinimumWidth(120)
        self._contact_btn.setIcon(
            QIcon(icon_pixmap(IconButton.CONTACT, 14, Palette.TEXT_DIM)))
        self._contact_btn.setCursor(Qt.PointingHandCursor)
        self._contact_btn.setToolTip("购买授权：用系统默认浏览器打开购买链接")
        self._contact_btn.clicked.connect(self.purchase_clicked.emit)
        row.addWidget(self._contact_btn)

        row.addStretch(1)
        col.addLayout(row)

        self._hint = QLabel("激活后即可连接直播并开始 OCR 识别")
        self._hint.setObjectName("activationHint")
        self._hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        col.addWidget(self._hint)
        col.addStretch(1)

    def show_unlocked(self) -> None:
        """解锁过渡：把遮罩切成"已激活"文案（视觉过渡，随后由调用方收起）。"""
        self._anim.stop()
        self._breath.setOpacity(1.0)
        self._brand.setText("LiveLens")
        self._prompt.setText("已激活")
        self._hint.setText("正在解锁工作台…")
        self._activate_btn.hide()

    def reset_prompt(self) -> None:
        """恢复未激活文案（下次显示遮罩时用）。"""
        self._prompt.setText("请激活 LiveLens")
        self._hint.setText("激活后即可连接直播并开始 OCR 识别")
        self._activate_btn.show()

    # -- 呼吸动画生命周期 --------------------------------------------------
    def set_suspended(self, suspended: bool) -> None:
        """窗口最小化 / 完全被遮挡时挂起呼吸动画。

        QGraphicsOpacityEffect 的重绘范围是整个品牌标签所在的遮罩区域，
        动画跑一轮就是 2.4s × 60fps 的持续重绘；窗口反正看不见，
        白烧 1~2.6% CPU（Phase 7 实测）。pause/resume 保留相位，
        恢复时从断点继续而不是跳回起点。
        """
        if suspended:
            self._anim.pause()
        elif self.isVisible():
            # 原条件 `not self._anim.state()` 在 pause 后是 Paused(非 0),
            # resume 分支永不触发(挂起一次就再也不动了)。正确语义:
            # Paused→resume(保相位),Stopped(尚未启动,如窗口刚显示)→start。
            if self._anim.state() == QPropertyAnimation.Paused:
                self._anim.resume()
            elif self._anim.state() == QPropertyAnimation.Stopped:
                self._anim.start()

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        if not getattr(self, "_suspended", False):
            self._anim.start()

    def hideEvent(self, event):  # noqa: N802
        self._anim.stop()
        self._breath.setOpacity(1.0)
        super().hideEvent(event)

