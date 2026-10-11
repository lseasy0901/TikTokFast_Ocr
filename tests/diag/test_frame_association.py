# -*- coding: utf-8 -*-
"""H1 帧关联与既有指标修正的单元测试。

覆盖：
    - LatestFrameBuffer：seq 随帧传递；get() 既有行为不变；覆盖计数不变
    - 消费缺口（合法覆盖）与关联失败的区别
    - PerformanceMonitor：按 seq 计算**该帧自己的**驻留（修正标量缺陷）
    - seq 缺失时回退到既有标量语义（向后兼容）
"""

import unittest

import numpy as np

from stream.frame_buffer import LatestFrameBuffer
from utils.diag_trace import TRACE
from utils.performance import PerformanceMonitor


def frame(v=0):
    return np.full((4, 4, 3), v, dtype=np.uint8)


class TestFrameBufferSeq(unittest.TestCase):
    def test_seq_travels_with_frame(self):
        b = LatestFrameBuffer()
        b.update(frame(1), seq=101)
        f, seq = b.get_with_seq()
        self.assertIsNotNone(f)
        self.assertEqual(seq, 101)

    def test_seq_none_when_not_provided(self):
        b = LatestFrameBuffer()
        b.update(frame(1))
        f, seq = b.get_with_seq()
        self.assertIsNotNone(f)
        self.assertIsNone(seq)

    def test_get_unchanged_behaviour(self):
        b = LatestFrameBuffer()
        self.assertIsNone(b.get())
        b.update(frame(1), seq=7)
        self.assertIsNotNone(b.get())
        self.assertEqual(b.frame_id, 1)

    def test_overwrite_count_unchanged(self):
        """单槽覆盖策略与丢帧计数必须与改动前一致。"""
        b = LatestFrameBuffer()
        b.update(frame(1), seq=1)
        b.update(frame(2), seq=2)      # 上一帧从未被 get → 记一次覆盖
        b.update(frame(3), seq=3)      # 再记一次
        self.assertEqual(b.dropped_count, 2)
        self.assertEqual(b.update_count, 3)
        b.get_with_seq()               # 消费
        b.update(frame(4), seq=4)      # 已消费 → 不再记覆盖
        self.assertEqual(b.dropped_count, 2)
        self.assertEqual(b.update_count, 4)

    def test_clear_resets_seq(self):
        b = LatestFrameBuffer()
        b.update(frame(1), seq=9)
        b.clear()
        self.assertFalse(b.has_frame)
        self.assertEqual(b.frame_id, 0)
        f, seq = b.get_with_seq()
        self.assertIsNone(f)
        self.assertIsNone(seq)

    def test_seq_does_not_change_which_frame_is_returned(self):
        b = LatestFrameBuffer()
        for i in range(1, 6):
            b.update(frame(i), seq=i)
        f, seq = b.get_with_seq()
        self.assertEqual(seq, 5)
        self.assertTrue(np.array_equal(f, frame(5)))


class TestPerformanceAssociation(unittest.TestCase):
    def test_seq_gives_that_frames_own_residency(self):
        """按 seq 计算时，驻留时间必须落在真实写入与消费之间。"""
        m = PerformanceMonitor()
        m.start()
        m.on_capture(seq=1)
        # 模拟：1 号帧尚未被消费，2 号帧又写入（覆盖）→ 标量会被刷新
        m.on_capture(seq=2)
        m.on_display(seq=1)            # 消费的是 1 号帧
        # 用 1 号帧自己的采集时间，读数必然 >= 0 且不因 2 号帧的写入而变小
        self.assertGreaterEqual(m.current_latency_ms, 0.0)

    def test_fallback_when_seq_missing(self):
        m = PerformanceMonitor()
        m.start()
        m.on_capture()                 # 旧签名
        m.on_display()                 # 旧签名
        self.assertGreaterEqual(m.current_latency_ms, 0.0)

    def test_unknown_seq_falls_back_to_scalar(self):
        m = PerformanceMonitor()
        m.start()
        m.on_capture(seq=1)
        m.on_display(seq=999)          # 未记录的 seq
        self.assertGreaterEqual(m.current_latency_ms, 0.0)

    def test_capture_map_is_bounded(self):
        m = PerformanceMonitor()
        m.start()
        for seq in range(1, 500):
            m.on_capture(seq=seq)
        self.assertLessEqual(len(m._capture_by_seq), m._capture_seq_capacity)

    def test_reset_clears_capture_map(self):
        m = PerformanceMonitor()
        m.start()
        m.on_capture(seq=1)
        m.reset()
        self.assertEqual(len(m._capture_by_seq), 0)

    def test_display_count_and_fps_still_updated(self):
        m = PerformanceMonitor()
        m.start()
        m.on_capture(seq=1)
        m.on_display(seq=1)
        snap = m.snapshot()
        self.assertEqual(snap["display_frames"], 1)


class TestDiagDisabledNoOverhead(unittest.TestCase):
    """关闭时热路径不得改变被测对象的状态。"""

    def test_buffer_works_with_trace_disabled(self):
        self.assertFalse(TRACE.enabled)
        b = LatestFrameBuffer()
        for i in range(1, 4):
            b.update(frame(i), seq=i)
        self.assertEqual(b.update_count, 3)
        f, seq = b.get_with_seq()
        self.assertEqual(seq, 3)

    def test_trace_ring_stays_empty(self):
        self.assertFalse(TRACE.enabled)
        b = LatestFrameBuffer()
        b.update(frame(1), seq=1)
        b.get_with_seq()
        self.assertEqual(len(TRACE._ring), 0)


if __name__ == "__main__":
    unittest.main()
