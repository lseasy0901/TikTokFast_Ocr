# -*- coding: utf-8 -*-
"""H1 逐帧诊断追踪的单元测试（FIX-1 / FIX-4 / FIX-6 契约）。

统计口径契约（本文件断言的核心）：
    - ``snapshot()`` 是**增量消费**：同一批事件重复快照不得重复计数
    - ``assoc_fail`` 是**累计失败事件数**，每个事件最多计一次
    - ``window.*`` 的分子分母同源；``cumulative.*`` 为累计量
"""

import os
import unittest

from utils import diag_trace
from utils.diag_trace import (
    DiagTrace, S_STORE, S_BRIDGE, S_CONSUME, S_SET, S_PAINT, PAIRS,
)


class Base(unittest.TestCase):
    def make(self, **kw):
        t = DiagTrace(**kw)
        t._on = True          # 直接置位，不依赖环境变量
        return t


class TestDisabledByDefault(unittest.TestCase):
    def test_module_singleton_off_without_env(self):
        self.assertFalse(diag_trace.TRACE.enabled)

    def test_disabled_is_noop(self):
        t = DiagTrace()
        t.note(S_STORE, 1)
        t.bump("c_stored")
        t.count_skip(5)
        t.count_consume_gap(3)
        t.note_paint(1)
        t.note_read(1.0, 10, "ok")
        t.note_parse(1.0)
        self.assertEqual(len(t._pending), 0)
        self.assertEqual(t.c_stored, 0)
        self.assertEqual(t.c_skipped, 0)
        self.assertEqual(t.c_consume_gap, 0)
        self.assertEqual(t.c_paint_repeat, 0)
        self.assertEqual(t.c_read_calls, 0)
        self.assertEqual(t.snapshot(), {})

    def test_start_output_returns_none_when_disabled(self):
        t = DiagTrace()
        self.assertIsNone(t.start_output("ignored.jsonl"))

    def test_stop_output_on_never_started_is_true(self):
        t = DiagTrace()
        self.assertTrue(t.stop_output())


class TestEnabledRecording(Base):
    def test_note_appends_and_bump_counts(self):
        t = self.make()
        for seq in range(1, 4):
            t.note(S_STORE, seq)
            t.bump("c_stored")
        self.assertEqual(len(t._pending), 3)
        self.assertEqual(t.c_stored, 3)
        self.assertEqual(t.c_events_noted, 3)


class TestRingCapacity(Base):
    def test_fixed_capacity_keeps_newest(self):
        t = self.make(ring_capacity=8)
        for seq in range(1, 21):
            t.note(S_STORE, seq)
        self.assertEqual(len(t._pending), 8)
        self.assertEqual([x[1] for x in t._pending], list(range(13, 21)))

    def test_lost_events_counted_not_hidden(self):
        t = self.make(ring_capacity=8)
        for seq in range(1, 21):
            t.note(S_STORE, seq)
        snap = t.snapshot()
        self.assertEqual(snap["cumulative"]["events_noted"], 20)
        self.assertEqual(snap["cumulative"]["events_drained"], 20)
        self.assertEqual(snap["cumulative"]["events_lost"], 12)


class TestFIX1NoDoubleCounting(Base):
    """FIX-1 / FIX-6 回归：重复快照不得重复计数。"""

    def test_repeated_snapshot_does_not_inflate_assoc_fail(self):
        t = self.make()
        t.note(S_STORE, None)              # 1 个永久关联失败事件
        seen = []
        for _ in range(5):
            seen.append(t.snapshot()["cumulative"]["assoc_fail"])
        self.assertEqual(seen, [1, 1, 1, 1, 1],
                         "assoc_fail 被重复快照累计了：%r" % seen)

    def test_repeated_snapshot_does_not_inflate_sample_count(self):
        t = self.make()
        t.note(S_STORE, 1)
        t.note(S_BRIDGE, 1)
        counts = [t.snapshot()["segments_ms"]["store_bridge"]["count"]
                  for _ in range(4)]
        self.assertEqual(counts, [1, 1, 1, 1],
                         "样本窗被重复摄取：%r" % counts)

    def test_second_snapshot_with_no_new_events_is_empty_window(self):
        t = self.make()
        t.note(S_STORE, 1)
        t.note(S_BRIDGE, 1)
        first = t.snapshot()
        second = t.snapshot()
        self.assertEqual(first["window"]["events"], 2)
        self.assertEqual(second["window"]["events"], 0)
        self.assertEqual(second["window"]["fail_rate"], 0.0)

    def test_cumulative_keeps_growing_with_new_events_only(self):
        t = self.make()
        t.note(S_STORE, 1); t.note(S_BRIDGE, 1)
        t.snapshot()
        t.note(S_STORE, 2); t.note(S_BRIDGE, 2)
        snap = t.snapshot()
        self.assertEqual(snap["cumulative"]["events_noted"], 4)
        self.assertEqual(snap["cumulative"]["events_drained"], 4)
        self.assertEqual(snap["segments_ms"]["store_bridge"]["count"], 2)


class TestPairing(Base):
    def test_segment_deltas_computed(self):
        t = self.make()
        t.note(S_STORE, 1)
        t.note(S_BRIDGE, 1)
        t.note(S_CONSUME, 1)
        snap = t.snapshot()
        self.assertEqual(snap["segments_ms"]["store_bridge"]["count"], 1)
        self.assertEqual(snap["segments_ms"]["bridge_consume"]["count"], 1)

    def test_pair_split_across_snapshots_still_pairs_once(self):
        """store 与 bridge 落在不同快照批次时，仍应恰好配对一次。"""
        t = self.make()
        t.note(S_STORE, 7)
        self.assertEqual(t.snapshot()["segments_ms"]["store_bridge"]["count"], 0)
        t.note(S_BRIDGE, 7)
        self.assertEqual(t.snapshot()["segments_ms"]["store_bridge"]["count"], 1)
        self.assertEqual(t.snapshot()["segments_ms"]["store_bridge"]["count"], 1)

    def test_missing_seq_counts_as_assoc_fail_once(self):
        t = self.make()
        t.note(S_STORE, None)
        t.snapshot(); t.snapshot()
        self.assertEqual(t.snapshot()["cumulative"]["assoc_fail"], 1)

    def test_negative_delta_counts_as_assoc_fail_once(self):
        t = self.make()
        t._pending.append((S_BRIDGE, 7, 100.0))
        t._pending.append((S_STORE, 7, 200.0))
        self.assertEqual(t.snapshot()["cumulative"]["assoc_fail"], 1)
        self.assertEqual(t.snapshot()["cumulative"]["assoc_fail"], 1)
        self.assertEqual(t.snapshot()["segments_ms"]["store_bridge"]["count"], 0)

    def test_all_pairs_present_in_snapshot(self):
        t = self.make()
        t.note(S_STORE, 1)
        for name, _, _ in PAIRS:
            self.assertIn(name, t.snapshot()["segments_ms"])

    def test_open_seq_map_is_bounded(self):
        t = self.make(open_seq_capacity=4)
        for seq in range(1, 40):
            t.note(S_STORE, seq)      # 只有 store，永不配对
        t.snapshot()
        self.assertLessEqual(len(t._open), 4)


class TestWindowVsCumulative(Base):
    def test_window_rate_uses_same_batch(self):
        t = self.make()
        t.note(S_STORE, None)          # 1 坏
        t.note(S_STORE, 1)             # 1 好
        snap = t.snapshot()
        self.assertEqual(snap["window"]["events"], 2)
        self.assertEqual(snap["window"]["assoc_fail"], 1)
        self.assertAlmostEqual(snap["window"]["fail_rate"], 0.5)

    def test_cumulative_rate_uses_total(self):
        t = self.make()
        t.note(S_STORE, None)
        t.snapshot()
        t.note(S_STORE, 1)
        snap = t.snapshot()
        self.assertEqual(snap["cumulative"]["assoc_fail"], 1)
        self.assertEqual(snap["cumulative"]["events_drained"], 2)
        self.assertAlmostEqual(snap["cumulative"]["fail_rate"], 0.5)


class TestExpectedBehaviours(Base):
    def test_skip_and_gap_are_separate_from_assoc_fail(self):
        t = self.make()
        t.count_skip(7)
        t.count_consume_gap(3)
        snap = t.snapshot()
        self.assertEqual(snap["counters"]["skipped"], 7)
        self.assertEqual(snap["counters"]["consume_gap"], 3)
        self.assertEqual(snap["cumulative"]["assoc_fail"], 0)

    def test_paint_repeat_detected(self):
        t = self.make()
        t.note_paint(42); t.note_paint(42); t.note_paint(43)
        self.assertEqual(t.c_paint_repeat, 1)

    def test_paint_repeat_ignores_none(self):
        t = self.make()
        t.note_paint(None); t.note_paint(None)
        self.assertEqual(t.c_paint_repeat, 0)


class TestReadAndParseStats(Base):
    """FIX-4：read 阻塞与 parse 分开计量，并区分 EOF/半帧/异常。"""

    def test_read_outcomes_counted_separately(self):
        t = self.make()
        t.note_read(1.0, 100, "ok")
        t.note_read(2.0, 100, "ok")
        t.note_read(0.1, 0, "eof")
        t.note_read(0.2, 50, "partial")
        t.note_read(0.3, 0, "error")
        snap = t.snapshot()
        r = snap["read"]
        self.assertEqual(r["calls"], 5)
        self.assertEqual(r["eof"], 1)
        self.assertEqual(r["partial"], 1)
        self.assertEqual(r["error"], 1)
        self.assertEqual(r["block_ms"]["count"], 5)
        self.assertAlmostEqual(r["block_ms"]["max"], 2.0)

    def test_parse_stats_separate_from_read(self):
        t = self.make()
        t.note_read(50.0, 100, "ok")
        t.note_parse(0.5)
        snap = t.snapshot()
        self.assertEqual(snap["read"]["block_ms"]["count"], 1)
        self.assertEqual(snap["parse_ms"]["count"], 1)
        self.assertAlmostEqual(snap["parse_ms"]["p50"], 0.5)

    def test_read_stats_not_polluted_by_disabled(self):
        t = DiagTrace()
        t.note_read(9.9, 9, "ok")
        self.assertEqual(t.c_read_calls, 0)


class TestEventObservation(Base):
    """FIX-7：EOF/断流/看门狗重启等非帧级事件观测。"""

    def test_events_counted_and_exposed(self):
        t = self.make()
        t.note_event("stream_eof")
        t.note_event("stream_eof")
        t.note_event("stall_restart")
        snap = t.snapshot()
        ev = snap["counters"]["events"]
        self.assertEqual(ev["stream_eof"], 2)
        self.assertEqual(ev["stall_restart"], 1)
        self.assertIsNotNone(snap["counters"]["last_event"])
        self.assertEqual(snap["counters"]["last_event"][0], "stall_restart")

    def test_unknown_event_counted_on_first_occurrence(self):
        t = self.make()
        t.note_event("restart_spawn_failed")
        snap = t.snapshot()
        self.assertEqual(snap["counters"]["events"]["restart_spawn_failed"], 1)

    def test_events_disabled_noop(self):
        t = DiagTrace()
        t.note_event("stream_eof")
        snap = t.snapshot()
        self.assertEqual(snap, {})
        self.assertEqual(t._events, {})


class TestSnapshotShape(Base):
    def test_percentile_ordering(self):
        t = self.make()
        for seq in range(1, 11):
            t.note(S_STORE, seq)
            t.note(S_BRIDGE, seq)
        seg = t.snapshot()["segments_ms"]["store_bridge"]
        self.assertGreaterEqual(seg["p99"], seg["p50"])
        self.assertGreaterEqual(seg["max"], seg["p99"])

    def test_empty_snapshot_is_well_formed(self):
        t = self.make()
        snap = t.snapshot()
        self.assertEqual(snap["window"]["events"], 0)
        self.assertEqual(snap["cumulative"]["events_noted"], 0)
        self.assertIn("read", snap)
        self.assertIn("parse_ms", snap)


if __name__ == "__main__":
    unittest.main()
