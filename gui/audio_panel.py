# -*- coding: utf-8 -*-
"""
AudioPanel：音频控制条（音量滑块 + 模式选择 + 状态显示）

只做展示与输入转发，不含任何音频逻辑；状态由 AudioService 推送，
经本组件的 Signal 转回 GUI 线程渲染（服务回调来自音频线程）。

交互约定（Phase UI 玻璃 2.0 改版，回应"音量必须一下一下点"的反馈）：
    - 滑块加宽到 170px、手柄 14px 圆点（QSS ``#audioSlider``），
      按住即可连续拖动；滚轮直接增减；点击轨道按 PageStep(4) 细步跳转。
    - 扬声器图标是自绘几何字形（不再是 emoji），点击 = 静音 / 恢复，
      静音时切换为带斜杠的字形。
    - 状态文字（音频 -- / 播放中 / 无声音…）排在最右，属于"读数"。

刻意不包含：播放按钮（随直播启停）、进度条、延迟数字。
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QButtonGroup, QFrame, QHBoxLayout,
                               QLabel, QPushButton, QSlider, QWidget)

from gui.widgets import IconButton, icon_pixmap
from audio.models import AudioMode, AudioState

__all__ = ["AudioPanel"]

#: 模式下拉框条目 → AudioMode（第一项 = 默认：极速模式，见需求三）
_MODE_ITEMS = [
    ("极速模式", AudioMode.LOW_LATENCY),
    ("同步模式", AudioMode.SYNC),
]

#: 状态 → 显示文案
_STATE_TEXT = {
    AudioState.IDLE: "音频 --",
    AudioState.STARTING: "音频 就绪",
    AudioState.PLAYING: "音频 播放中",
    AudioState.NO_AUDIO_TRACK: "当前直播无声音",
    AudioState.ERROR: "音频 不可用",
    AudioState.STOPPED: "音频 已停止",
}

#: 静音前的记忆音量（应用启动后第一次静音时恢复到这个值）
_DEFAULT_VOLUME = 0.8


class AudioPanel(QWidget):
    """视频工具栏上的音频控制条"""

    #: 用户拖动音量（0.0~1.0）
    volumeChanged = Signal(float)
    #: 用户切换模式
    modeChanged = Signal(object)
    #: 服务状态推送（音频线程 emit，自动排队回 GUI 线程渲染）
    stateArrived = Signal(object, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._building = True   # 程序化 setValue/setCurrentIndex 不触发信号
        self._last_volume = _DEFAULT_VOLUME

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        # 静音键：自绘扬声器字形；点击在"静音 ↔ 恢复"间切换
        self._mute_btn = QPushButton()
        self._mute_btn.setObjectName("audioMuteBtn")
        self._mute_btn.setFlat(True)
        self._mute_btn.setFixedSize(28, 28)
        self._mute_btn.setCursor(Qt.PointingHandCursor)
        self._mute_btn.setToolTip("静音 / 恢复（点击图标）")
        self._mute_btn.clicked.connect(self._toggle_mute)
        self._set_mute_icon(False)
        row.addWidget(self._mute_btn)

        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setObjectName("audioSlider")
        self._slider.setRange(0, 100)
        self._slider.setValue(80)
        # PageStep=4：点轨道一下只跳 4%，不会从 80 直接跳到 90
        self._slider.setPageStep(4)
        self._slider.setFixedWidth(170)
        self._slider.setToolTip("音量：拖动 / 滚轮 / 点击轨道")
        self._slider.valueChanged.connect(self._on_slider)
        row.addWidget(self._slider)

        # 模式分段控件：两个互斥选项用分段按钮而不是下拉 —— 选项全部可见、
        # 单击即切、选中态一眼可辨（LightC 设置页同款交互语汇）。
        self._mode_box = QFrame()
        self._mode_box.setObjectName("audioSegment")
        seg_lay = QHBoxLayout(self._mode_box)
        seg_lay.setContentsMargins(2, 2, 2, 2)
        seg_lay.setSpacing(2)
        self._mode_buttons: list[QPushButton] = []
        # 互斥组：普通布局里的可勾选按钮不会自动互斥（QSS 只管观感），
        # 缺了这组就会出现「两个模式同时高亮」。
        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)
        for text, _mode in _MODE_ITEMS:
            btn = QPushButton(text)
            btn.setObjectName("segmentBtn")
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFocusPolicy(Qt.TabFocus)
            btn.setToolTip("极速模式：最新声音优先；同步模式：声音连贯优先")
            btn.clicked.connect(self._on_mode_clicked)
            self._mode_group.addButton(btn)
            seg_lay.addWidget(btn)
            self._mode_buttons.append(btn)
        self._mode_index = 0
        self._mode_buttons[0].setChecked(True)
        row.addWidget(self._mode_box)

        self._status = QLabel("音频 --")
        self._status.setObjectName("audioStatus")
        row.addWidget(self._status)

        self.stateArrived.connect(self._render_state)
        self._building = False

    # ---- GUI 交互（GUI 线程） ----
    def _on_slider(self, value: int) -> None:
        if not self._building:
            if value > 0:
                self._last_volume = value / 100.0
            self._set_mute_icon(value == 0)
            self.volumeChanged.emit(value / 100.0)

    def _toggle_mute(self) -> None:
        """点击扬声器：有声 → 静音（记忆当前值）；已静音 → 恢复记忆值。"""
        if self._slider.value() > 0:
            self._slider.setValue(0)
        else:
            self._slider.setValue(int(round(self._last_volume * 100)))

    def _set_mute_icon(self, muted: bool) -> None:
        self._mute_btn.setIcon(
            QIcon(icon_pixmap(IconButton.SPEAKER_MUTE if muted
                              else IconButton.SPEAKER, 16)))
        self._mute_btn.setToolTip("取消静音" if muted else "静音（点击图标）")

    def _on_mode_clicked(self) -> None:
        """分段按钮点击：互斥由 QButtonGroup 保证，这里只在模式变化时转发。"""
        sender = self.sender()
        if sender not in self._mode_buttons:
            return
        index = self._mode_buttons.index(sender)
        if not self._building and index != self._mode_index:
            self._mode_index = index
            self.modeChanged.emit(_MODE_ITEMS[index][1])

    def _on_mode_index(self, index: int) -> None:
        if not self._building and 0 <= index < len(_MODE_ITEMS):
            self.modeChanged.emit(_MODE_ITEMS[index][1])

    # ---- 服务状态（可从音频线程经 Signal 调用） ----
    def on_service_state(self, state: AudioState, message: str = "") -> None:
        """AudioService 状态回调入口：转 GUI 线程渲染"""
        self.stateArrived.emit(state, message)

    def _render_state(self, state: AudioState, message: str) -> None:
        text = _STATE_TEXT.get(state, f"音频 {state.value}")
        if state is AudioState.ERROR and message:
            text = "音频 不可用"
        self._status.setText(text)
        # 无声/错误时控件保持可用（切模式/调音量无副作用），不禁用

    # ---- 配置回显（未来从设置恢复时用） ----
    def set_volume_silent(self, value: float) -> None:
        self._building = True
        if value > 0:
            self._last_volume = value
        self._slider.setValue(int(round(value * 100)))
        self._set_mute_icon(value == 0)
        self._building = False

    def set_mode_silent(self, mode: AudioMode) -> None:
        for index, (_text, m) in enumerate(_MODE_ITEMS):
            if m is mode:
                self._building = True
                for i, btn in enumerate(self._mode_buttons):
                    btn.setChecked(i == index)
                self._mode_index = index
                self._building = False
                return
