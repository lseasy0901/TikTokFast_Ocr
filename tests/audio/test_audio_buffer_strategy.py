# -*- coding: utf-8 -*-
"""音频测试 4/7：buffer 策略（水位 / 上限 / 欠载 / 丢块）"""

import unittest

from audio.models import BYTES_PER_SECOND, AudioMode, ms_to_bytes
from audio.strategy import LowLatencyStrategy, SyncStrategy
from audio_test_utils import SILENCE_100MS, wait_for, FakeOutput
from audio import AudioService, AudioState

CHUNK = SILENCE_100MS          # 100ms = 19200B


class TestSyncStrategy(unittest.TestCase):
    def test_safety_cap(self):
        s = SyncStrategy()
        s.on_chunk(CHUNK * 30)                  # 3s > 2s 上限
        self.assertEqual(s.playable(), ms_to_bytes(SyncStrategy.MAX_MS))

    def test_take_fifo(self):
        s = SyncStrategy()
        a, b = b"\x01" * 1000, b"\x02" * 1000
        s.on_chunk(a)
        s.on_chunk(b)
        self.assertEqual(s.take(1000), a)
        self.assertEqual(s.take(1000), b)

    def test_take_partial(self):
        s = SyncStrategy()
        s.on_chunk(b"\x01" * 100)
        self.assertEqual(len(s.take(1000)), 100)
        self.assertEqual(s.playable(), 0)

    def test_pushfront_preserves_order(self):
        """设备缓冲满回退时未写部分必须放回头部（乱序=爆音，历史 bug）"""
        s = SyncStrategy()
        s.on_chunk(b"AA")
        a = s.take(2)
        s.pushfront(a[1:])              # 模拟只写出了 1 字节
        s.on_chunk(b"BBB")              # 后续新数据照常追加
        self.assertEqual(bytes(s.take(10)), b"A" + b"BBB")

    def test_pushfront_with_backlog(self):
        """缓冲中还有积压时回退：回退数据必须插在积压**之前**"""
        s = SyncStrategy()
        s.on_chunk(b"abcdef")
        a = s.take(4)                   # 取 abcd，缓冲剩 ef
        s.pushfront(a[2:])              # 只有 cd 写出失败，回到头部
        self.assertEqual(bytes(s.take(10)), b"cdef")


class TestLowLatencyStrategy(unittest.TestCase):
    def test_drop_old_beyond_max(self):
        s = LowLatencyStrategy()
        s.on_chunk(CHUNK * 5)                   # 500ms
        # 上限 120ms：旧块全部被丢
        self.assertEqual(s.playable(), ms_to_bytes(LowLatencyStrategy.MAX_MS))

    def test_underrun_no_rearm(self):
        """极速模式欠载不清空缓冲（保留已收数据），同步模式清空重灌"""
        s = LowLatencyStrategy()
        s.on_chunk(CHUNK)
        s.take(s.playable())
        s.on_chunk(CHUNK[:40])
        s.on_underrun()
        self.assertEqual(s.playable(), 40)      # 数据保留

        s2 = SyncStrategy()
        s2.on_chunk(CHUNK[:40])
        s2.on_underrun()
        self.assertEqual(s2.playable(), 0)      # 清空等水位


class TestQueueDropPolicy(unittest.TestCase):
    """队列满时丢最旧块：消费停滞不反压上游（保护视频链路）"""

    def test_feed_drops_oldest_when_full(self):
        svc = AudioService(output_factory=FakeOutput)
        svc.start()
        # 直接向 worker 队列塞满后再塞：容量 48 块
        for _ in range(200):
            svc.feed(CHUNK)
        self.assertTrue(wait_for(lambda: svc.stats.chunks_dropped > 0, timeout=5))
        svc.stop()
        self.assertGreater(svc.stats.chunks_dropped, 0)


class TestPartialWritePump(unittest.TestCase):
    """设备缓冲有限的端到端泵测试：输出字节必须是输入流的无乱序前缀"""

    def test_output_is_ordered_prefix_when_device_fills(self):
        # accept_all=False：write 只收一半，逼出"写满回退"路径
        holder = []

        def factory():
            out = FakeOutput(accept_all=False)
            holder.append(out)
            return out

        # 显式 SYNC：本测试验证泵的保序性，要求"无丢块"（极速模式的
        # 丢旧块是策略行为，会破坏前缀假设，与本测试无关）
        svc = AudioService(output_factory=factory, mode=AudioMode.SYNC)
        svc.start()
        # 20 块递增标记：第 i 块全部字节为 i
        fed = b"".join(bytes([i]) * len(CHUNK) for i in range(1, 21))
        for i in range(1, 21):
            svc.feed(bytes([i]) * len(CHUNK))
        self.assertTrue(wait_for(lambda: len(holder[0].data) >= len(fed) - 2 * len(CHUNK),
                                 timeout=8))
        svc.stop()
        out = bytes(holder[0].data)
        # 输出必须是 fed 的前缀（顺序无乱序），且字节标记单调不减
        self.assertEqual(out, fed[:len(out)], "输出与输入前缀不一致：存在 PCM 乱序")
        marks = list(out)
        self.assertEqual(marks, sorted(marks), "字节标记非单调：PCM 乱序")


if __name__ == "__main__":
    unittest.main()
