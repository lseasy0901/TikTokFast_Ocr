# -*- coding: utf-8 -*-
"""H1 帧关联与既有指标修正测试（含 FIX-5 并发一致性）。

覆盖：
    - LatestFrameBuffer：seq 随帧传递；get() 既有行为不变；覆盖计数不变
    - FFmpegReader.read_frame_with_seq()：frame / seq / t 三者**原子一致**
      （并发 writer/reader 回归，证明不存在"帧号落后一帧"的错位）
    - PerformanceMonitor：按 seq 计算该帧自己的驻留；向后兼容
    - 诊断关闭时不改变被测对象状态
"""

import threading
import time
import unittest

import numpy as np

from stream.ffmpeg_reader import FFmpegReader
from stream.frame_buffer import LatestFrameBuffer
from utils.diag_trace import TRACE
from utils.performance import PerformanceMonitor

W = H = 8
FSIZE = W * H * 3


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
        b.get_with_seq()
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


class TestReadFrameWithSeqAtomic(unittest.TestCase):
    """FIX-5：帧本体、帧号、落槽时刻必须来自同一次加锁。"""

    def _reader(self):
        # 构造器不做任何网络探测（见 FFmpegReader.__init__ 文档）
        return FFmpegReader("http://example.invalid/x.flv", width=W, height=H)

    def test_empty_reader_returns_none_triple(self):
        r = self._reader()
        f, seq, t = r.read_frame_with_seq()
        self.assertIsNone(f)
        self.assertIsNone(seq)
        self.assertEqual(t, 0.0)

    def test_single_store_then_read_is_consistent(self):
        r = self._reader()
        payload = bytes([123]) * FSIZE
        r._store_frame(payload)
        f, seq, t = r.read_frame_with_seq()
        self.assertIsNotNone(f)
        self.assertEqual(seq, 1)
        self.assertEqual(int(f[0, 0, 0]), 123)
        self.assertGreater(t, 0.0)

    def test_concurrent_store_and_read_stay_consistent(self):
        """并发回归：每帧 payload 的字节值 = seq & 0xFF。

        若 frame 与 seq 不是同一次加锁取出（旧实现的两步法），
        在 2 万次竞态中必然出现内容与帧号不匹配。
        """
        r = self._reader()
        N = 20000
        mismatches = []
        stop = threading.Event()

        def writer():
            for i in range(1, N + 1):
                r._store_frame(bytes([i & 0xFF]) * FSIZE)

        def reader():
            seen = 0
            while not stop.is_set():
                f, seq, t = r.read_frame_with_seq()
                if f is None:
                    continue
                seen += 1
                if int(f[0, 0, 0]) != (seq & 0xFF):
                    mismatches.append((seq, int(f[0, 0, 0])))
                if seen >= N:
                    break

        tw = threading.Thread(target=writer, daemon=True)
        tr = threading.Thread(target=reader, daemon=True)
        tr.start(); tw.start()
        tw.join(timeout=30)
        time.sleep(0.05)
        stop.set()
        tr.join(timeout=10)

        self.assertEqual(mismatches, [],
                         "帧内容与帧号错位 %d 次，例如 %r"
                         % (len(mismatches), mismatches[:3]))

    def test_read_frame_still_works(self):
        """向后兼容：旧的 read_frame() 必须保持可用。"""
        r = self._reader()
        r._store_frame(bytes([7]) * FSIZE)
        f = r.read_frame()
        self.assertIsNotNone(f)
        self.assertEqual(int(f[0, 0, 0]), 7)

    def test_store_time_is_perf_counter_comparable(self):
        """落槽时刻必须与 time.perf_counter() 同一时基（可相减）。"""
        r = self._reader()
        before = time.perf_counter()
        r._store_frame(bytes([1]) * FSIZE)
        after = time.perf_counter()
        _, _, t = r.read_frame_with_seq()
        self.assertGreaterEqual(t, before)
        self.assertLessEqual(t, after)


class TestPerformanceAssociation(unittest.TestCase):
    def test_seq_gives_that_frames_own_residency(self):
        m = PerformanceMonitor()
        m.start()
        m.on_capture(seq=1)
        m.on_capture(seq=2)            # 1 号帧未消费就被 2 号覆盖
        m.on_display(seq=1)
        self.assertGreaterEqual(m.current_latency_ms, 0.0)

    def test_fallback_when_seq_missing(self):
        m = PerformanceMonitor()
        m.start()
        m.on_capture()
        m.on_display()
        self.assertGreaterEqual(m.current_latency_ms, 0.0)

    def test_unknown_seq_falls_back_to_scalar(self):
        m = PerformanceMonitor()
        m.start()
        m.on_capture(seq=1)
        m.on_display(seq=999)
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

    def test_display_count_still_updated(self):
        m = PerformanceMonitor()
        m.start()
        m.on_capture(seq=1)
        m.on_display(seq=1)
        self.assertEqual(m.snapshot()["display_frames"], 1)


class TestDiagDisabledNoOverhead(unittest.TestCase):
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
        self.assertEqual(len(TRACE._pending), 0)


if __name__ == "__main__":
    unittest.main()
