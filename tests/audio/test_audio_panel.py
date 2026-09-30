# -*- coding: utf-8 -*-
"""音频测试（附）：AudioPanel 信号与状态渲染"""

import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

from gui.audio_panel import AudioPanel        # noqa: E402
from audio import AudioMode, AudioState, AudioService       # noqa: E402


class TestAudioPanel(unittest.TestCase):
    def setUp(self):
        self.panel = AudioPanel()
        self.volumes = []
        self.modes = []
        self.panel.volumeChanged.connect(self.volumes.append)
        self.panel.modeChanged.connect(self.modes.append)

    def test_volume_signal(self):
        self.panel._slider.setValue(50)
        self.assertEqual(self.volumes, [0.5])

    def test_default_mode_is_low_latency(self):
        """需求三：极速模式为默认（下拉第一项）"""
        self.assertEqual(self.panel._mode_box.currentIndex(), 0)
        self.assertEqual(self.panel._mode_box.itemText(0), "极速模式")
        self.assertEqual(AudioService().mode.value, "low_latency")

    def test_mode_signal(self):
        self.panel._mode_box.setCurrentIndex(1)
        self.assertEqual(self.modes, [AudioMode.SYNC])

    def test_mode_combo_adapts_to_content(self):
        """需求二：宽度自适应内容，不写死像素（未来新增模式不截断）"""
        from PySide6.QtWidgets import QComboBox
        self.assertEqual(self.panel._mode_box.sizeAdjustPolicy(),
                         QComboBox.SizeAdjustPolicy.AdjustToContents)

    def test_state_render(self):
        self.panel.on_service_state(AudioState.NO_AUDIO_TRACK)
        self.panel.on_service_state(AudioState.PLAYING)
        self.assertEqual(self.panel._status.text(), "音频 播放中")

    def test_silent_setters(self):
        self.panel.set_volume_silent(0.3)
        self.assertEqual(self.volumes, [])
        self.panel.set_mode_silent(AudioMode.LOW_LATENCY)
        self.assertEqual(self.modes, [])


if __name__ == "__main__":
    unittest.main()
