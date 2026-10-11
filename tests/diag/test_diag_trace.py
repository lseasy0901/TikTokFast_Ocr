# -*- coding: utf-8 -*-
"""H1 逐帧诊断追踪的单元测试。

覆盖：
    - 默认关闭：所有方法空转，环形缓冲与计数器都不增长
    - 启用后：阶段时间点进环形缓冲、计数器自增
    - 环形缓冲固定容量与溢出（丢最旧，不扩容）
    - 跨阶段配对：同一 seq 的段耗时可计算
    - 关联失败：负耗时（时钟倒退）计为 assoc_fail
    - 合法跳帧与"消费缺口"分开计数，不混入 assoc_fail
    - 重复绘制检测
    - snapshot 的分位数与计数
"""

import os
import unittest

from utils import diag_trace
from utils.diag_trace import (
    DiagTrace, S_STORE, S_BRIDGE, S_CONSUME, S_SET, S_PAINT, PAIRS,
)


class DiagTraceTestBase(unittest.TestCase):
    """每个用例都用独立实例，避免共享单例状态。"""

    def make(self, **kw):
        t = DiagTrace(**kw)
        t._on = True          # 直接置位，不依赖环境变量
        return t


class TestDisabledByDefault(unittest.TestCase):
    def test_module_singleton_off_without_env(self):
        # 本测试进程未设置 DLV_DIAG_TRACE
        self.assertFalse(diag_trace.TRACE.enabled)

    def test_disabled_is_noop(self):
        t = DiagTrace()                    # 默认关闭
        t.note(S_STORE, 1)
        t.bump("c_stored")
        t.count_skip(5)
        t.count_consume_gap(3)
        t.note_paint(1)
        self.assertEqual(len(t._ring), 0)
        self.assertEqual(t.c_stored, 0)
        self.assertEqual(t.c_skipped, 0)
        self.assertEqual(t.c_consume_gap, 0)
        self.assertEqual(t.c_paint_repeat, 0)
        self.assertEqual(t.snapshot(), {})

    def test_start_output_returns_none_when_disabled(self):
        t = DiagTrace()
        self.assertIsNone(t.start_output("ignored.jsonl"))


class TestEnabledRecording(DiagTraceTestBase):
    def test_note_appends_and_bump_counts(self):
        t = self.make()
        for seq in range(1, 4):
            t.note(S_STORE, seq)
            t.bump("c_stored")
        self.assertEqual(len(t._ring), 3)
        self.assertEqual(t.c_stored, 3)
        self.assertEqual(list(t._ring)[0], (S_STORE, 1, list(t._ring)[0][2]))

    def test_timestamps_are_monotonic_per_stage(self):
        t = self.make()
        for seq in range(1, 6):
            t.note(S_STORE, seq)
            t.note(S_BRIDGE, seq)
        ev = list(t._ring)
        store_ts = [x[2] for x in ev if x[0] == S_STORE]
        self.assertEqual(store_ts, sorted(store_ts))


class TestRingCapacity(DiagTraceTestBase):
    def test_fixed_capacity_keeps_newest(self):
        t = self.make(ring_capacity=8)
        for seq in range(1, 21):
            t.note(S_STORE, seq)
        self.assertEqual(len(t._ring), 8)                 # 不扩容
        self.assertEqual([x[1] for x in t._ring], list(range(13, 21)))

    def test_sample_capacity_bounded(self):
        t = self.make(sample_capacity=4)
        for seq in range(1, 40):
            t.note(S_STORE, seq)
            t.note(S_BRIDGE, seq)
        t.snapshot()
        self.assertLessEqual(len(t._samples["store_bridge"]), 4)


class TestPairing(DiagTraceTestBase):
    def test_segment_deltas_computed(self):
        t = self.make()
        t.note(S_STORE, 1)
        t.note(S_BRIDGE, 1)
        t.note(S_CONSUME, 1)
        snap = t.snapshot()
        self.assertGreaterEqual(snap["segments_ms"]["store_bridge"]["count"], 1)
        self.assertGreaterEqual(snap["segments_ms"]["bridge_consume"]["count"], 1)

    def test_missing_seq_counts_as_assoc_fail(self):
        t = self.make()
        t.note(S_STORE, None)
        snap = t.snapshot()
        self.assertEqual(snap["counters"]["assoc_fail"], 1)

    def test_negative_delta_counts_as_assoc_fail(self):
        t = self.make()
        # 人为构造"终点早于起点"（时钟倒退 / 配对错误）
        t._ring.append((S_BRIDGE, 7, 100.0))
        t._ring.append((S_STORE, 7, 200.0))
        snap = t.snapshot()
        self.assertGreaterEqual(snap["counters"]["assoc_fail"], 1)
        self.assertEqual(snap["segments_ms"]["store_bridge"]["count"], 0)

    def test_all_pairs_present_in_snapshot(self):
        t = self.make()
        t.note(S_STORE, 1)
        snap = t.snapshot()
        for name, _, _ in PAIRS:
            self.assertIn(name, snap["segments_ms"])

    def test_unpaired_stage_does_not_create_sample(self):
        t = self.make()
        t.note(S_STORE, 1)          # 只有 store，没有 bridge
        snap = t.snapshot()
        self.assertEqual(snap["segments_ms"]["store_bridge"]["count"], 0)


class TestExpectedBehaviours(DiagTraceTestBase):
    def test_skip_and_gap_are_separate_from_assoc_fail(self):
        t = self.make()
        t.count_skip(7)             # 合法跳帧
        t.count_consume_gap(3)      # 单槽覆盖导致的消费缺口
        snap = t.snapshot()
        self.assertEqual(snap["counters"]["skipped"], 7)
        self.assertEqual(snap["counters"]["consume_gap"], 3)
        self.assertEqual(snap["counters"]["assoc_fail"], 0)

    def test_paint_repeat_detected(self):
        t = self.make()
        t.note_paint(42)
        t.note_paint(42)            # 同一帧被重复绘制
        t.note_paint(43)
        self.assertEqual(t.c_paint_repeat, 1)

    def test_paint_repeat_ignores_none(self):
        t = self.make()
        t.note_paint(None)
        t.note_paint(None)
        self.assertEqual(t.c_paint_repeat, 0)


class TestSnapshotShape(DiagTraceTestBase):
    def test_counters_and_percentiles(self):
        t = self.make()
        for seq in range(1, 11):
            t.note(S_STORE, seq)
            t.note(S_BRIDGE, seq)
        snap = t.snapshot()
        c = snap["counters"]
        self.assertEqual(c["stored"], 0)          # 未手动 bump
        self.assertIn("ring_events", c)
        seg = snap["segments_ms"]["store_bridge"]
        self.assertGreaterEqual(seg["p99"], seg["p50"])
        self.assertGreaterEqual(seg["max"], seg["p99"])

    def test_snapshot_does_not_block_on_empty_ring(self):
        t = self.make()
        snap = t.snapshot()
        self.assertEqual(snap["counters"]["ring_events"], 0)


if __name__ == "__main__":
    unittest.main()
