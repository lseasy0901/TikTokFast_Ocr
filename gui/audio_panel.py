# -*- coding: utf-8 -*-
"""
AudioPanel：音频控制条（音量滑块 + 模式选择 + 状态显示）

只做展示与输入转发，不含任何音频逻辑；状态由 AudioService 推送，
经本组件的 Signal 转回 GUI 线程渲染（服务回调来自音频线程）。

刻意不包含：播放按钮（随直播启停）、进度条、延迟数字。
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QSlider, QWidget

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

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        self._icon = QLabel("🔊")
        self._icon.setToolTip("直播声音")
        row.addWidget(self._icon)

        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setObjectName("audioSlider")
        self._slider.setRange(0, 100)
        self._slider.setValue(80)
        self._slider.setFixedWidth(110)
        self._slider.setToolTip("音量")
        self._slider.valueChanged.connect(self._on_slider)
        row.addWidget(self._slider)

        self._mode_box = QComboBox()
        self._mode_box.setObjectName("audioModeBox")
        for text, _mode in _MODE_ITEMS:
            self._mode_box.addItem(text)
        # 宽度按内容自适应（Qt 最佳实践：AdjustToContents + 最少可见字符数），
        # 不写死像素 —— "极速模式"/"同步模式"完整显示，未来新增更长名称的
        # 模式也不会被截断或撑破布局。
        self._mode_box.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents)
        self._mode_box.setMinimumContentsLength(5)   # "极速模式" 4 字 + 余量
        self._mode_box.setToolTip("极速模式（默认）：最新声音优先；同步模式：声音连贯优先")
        self._mode_box.currentIndexChanged.connect(self._on_mode_index)
        row.addWidget(self._mode_box)

        self._status = QLabel("音频 --")
        self._status.setObjectName("audioStatus")
        row.addWidget(self._status)

        self.stateArrived.connect(self._render_state)
        self._building = False

    # ---- GUI 交互（GUI 线程） ----
    def _on_slider(self, value: int) -> None:
        if not self._building:
            self.volumeChanged.emit(value / 100.0)

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
        self._slider.setValue(int(round(value * 100)))
        self._building = False

    def set_mode_silent(self, mode: AudioMode) -> None:
        for index, (_text, m) in enumerate(_MODE_ITEMS):
            if m is mode:
                self._building = True
                self._mode_box.setCurrentIndex(index)
                self._building = False
                return
