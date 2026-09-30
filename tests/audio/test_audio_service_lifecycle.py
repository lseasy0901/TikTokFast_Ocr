# -*- coding: utf-8 -*-
"""音频测试 1/7：AudioService 启动 / 停止"""

import unittest

from audio import AudioMode, AudioService, AudioState
from audio_test_utils import FakeOutput, SILENCE_100MS, wait_for


class TestServiceLifecycle(unittest.TestCase):
    def setUp(self):
        self.outputs = []
        self.states = []

    def _factory(self):
        out = FakeOutput()
        self.outputs.append(out)
        return out

    def _make(self):
        svc = AudioService(output_factory=self._factory)
        svc.set_state_callback(lambda s, m: self.states.append((s, m)))
        return svc

    def test_start_stop_states(self):
        svc = self._make()
        self.assertIs(svc.state, AudioState.IDLE)

        svc.start()
        self.assertIs(svc.state, AudioState.STARTING)

        svc.feed(SILENCE_100MS)
        self.assertTrue(wait_for(lambda: svc.state is AudioState.PLAYING),
                        f"states={self.states}")

        svc.stop()
        self.assertIs(svc.state, AudioState.IDLE)
        self.assertTrue(self.outputs[0].opened)
        self.assertTrue(self.outputs[0].closed)

    def test_start_idempotent(self):
        svc = self._make()
        svc.start()
        svc.start()
        svc.stop()
        self.assertIs(svc.state, AudioState.IDLE)

    def test_stop_idempotent_and_without_start(self):
        svc = self._make()
        svc.stop()
        svc.stop()
        self.assertIs(svc.state, AudioState.IDLE)

    def test_feed_before_start_dropped(self):
        svc = self._make()
        svc.feed(SILENCE_100MS)      # 未启动：直接丢弃，不得抛异常
        svc.start()
        svc.feed(SILENCE_100MS)
        self.assertTrue(wait_for(lambda: svc.state is AudioState.PLAYING))
        svc.stop()

    def test_pcm_flows_to_output(self):
        # SYNC：验证"喂多少出多少"的完整通路（极速模式允许丢旧块）
        svc = AudioService(output_factory=self._factory, mode=AudioMode.SYNC)
        svc.set_state_callback(lambda s, m: self.states.append((s, m)))
        svc.start()
        svc.feed(SILENCE_100MS * 3)
        self.assertTrue(wait_for(
            lambda: self.outputs and len(self.outputs[0].data) >= len(SILENCE_100MS) * 2))
        svc.stop()


if __name__ == "__main__":
    unittest.main()
