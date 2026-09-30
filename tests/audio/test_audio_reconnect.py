# -*- coding: utf-8 -*-
"""音频测试 7/7：直播断线恢复（restart 接口）与配置回显"""

import unittest

from audio import AudioService, AudioState
from audio_test_utils import FakeOutput, SILENCE_100MS, wait_for

CHUNK = SILENCE_100MS


class TestReconnect(unittest.TestCase):
    def test_restart_replays(self):
        outputs = []

        def factory():
            out = FakeOutput()
            outputs.append(out)
            return out

        svc = AudioService(output_factory=factory)
        svc.start()
        svc.feed(CHUNK)
        self.assertTrue(wait_for(lambda: svc.state is AudioState.PLAYING))
        svc.restart()                            # 断线重连预留接口
        self.assertTrue(wait_for(lambda: len(outputs) >= 2))
        self.assertTrue(wait_for(lambda: svc.state is AudioState.STARTING))
        svc.feed(CHUNK)
        self.assertTrue(wait_for(lambda: svc.state is AudioState.PLAYING))
        svc.stop()
        self.assertTrue(all(o.closed for o in outputs))


if __name__ == "__main__":
    unittest.main()
