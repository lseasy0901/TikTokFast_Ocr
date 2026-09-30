# -*- coding: utf-8 -*-
"""音频测试 2/7：模式切换（Sync ↔ LowLatency）"""

import unittest

from audio import AudioMode, AudioService, AudioState
from audio.models import ms_to_bytes
from audio.strategy import LowLatencyStrategy, SyncStrategy, create_strategy
from audio_test_utils import FakeOutput, wait_for

CHUNK = SILENCE_100MS = None   # 在 setUpModule 里赋值（依赖 BYTES_PER_SECOND）


def setUpModule():
    from audio_test_utils import SILENCE_100MS
    global CHUNK
    CHUNK = SILENCE_100MS      # 100ms 一块


class TestStrategyObjects(unittest.TestCase):
    def test_create_strategy(self):
        self.assertIsInstance(create_strategy(AudioMode.SYNC), SyncStrategy)
        self.assertIsInstance(create_strategy(AudioMode.LOW_LATENCY),
                              LowLatencyStrategy)

    def test_sync_watermark_and_rearm(self):
        s = SyncStrategy()
        s.on_chunk(CHUNK[:40])                  # 不足 80ms 水位
        self.assertFalse(s.should_start())
        s.on_chunk(CHUNK)                       # 补足
        self.assertTrue(s.should_start())
        s.take(s.playable())
        s.on_underrun()                         # 欠载 → 重新等水位
        self.assertFalse(s.should_start())
        s.on_chunk(CHUNK)
        self.assertTrue(s.should_start())

    def test_low_latency_caps_and_keeps_newest(self):
        s = LowLatencyStrategy()
        for i in range(10):                     # 1s >> 120ms 上限
            s.on_chunk(bytes([i]) * len(CHUNK))
        cap = ms_to_bytes(LowLatencyStrategy.MAX_MS)
        self.assertLessEqual(s.playable(), cap)
        data = s.take(s.playable())
        # 保留的必须是最新的尾部：最后一块完整 + 前一块的尾部
        self.assertEqual(data[-len(CHUNK):], bytes([9]) * len(CHUNK))
        self.assertEqual(data[:cap - len(CHUNK)], bytes([8]) * (cap - len(CHUNK)))


class TestServiceModeSwitch(unittest.TestCase):
    def test_switch_mode_mid_stream(self):
        holder = []

        def factory():
            out = FakeOutput()
            holder.append(out)
            return out

        svc = AudioService(output_factory=factory)
        svc.start()
        for _ in range(3):
            svc.feed(CHUNK)
        self.assertTrue(wait_for(lambda: svc.state is AudioState.PLAYING))
        svc.set_mode(AudioMode.LOW_LATENCY)
        for _ in range(5):
            svc.feed(CHUNK)
        svc.set_mode(AudioMode.SYNC)
        svc.feed(CHUNK)
        self.assertTrue(wait_for(lambda: svc.stats.chunks_received >= 9))
        svc.stop()
        self.assertEqual(svc.mode, AudioMode.SYNC)


if __name__ == "__main__":
    unittest.main()
