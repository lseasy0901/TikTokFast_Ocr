# -*- coding: utf-8 -*-
"""H1 writer 生命周期与失败可见性测试（FIX-2 / FIX-3）。

契约：
    - ``start_output`` 只有"文件可写且线程已启动"才返回路径，否则返回 None
    - ``stop_output`` 返回 True 表示**已确认线程退出**；超时返回 False（不假装成功）
    - 重复 start 不产生第二个 writer；重复 stop 幂等
    - 写盘失败必须可观察（output_errors / last_output_error），不得静默
"""

import json
import os
import tempfile
import threading
import time
import unittest

from utils import diag_trace
from utils.diag_trace import DiagTrace, S_STORE, S_BRIDGE

WRITER = "diag-trace-writer"


def writer_threads():
    return [t for t in threading.enumerate() if t.name == WRITER]


class WriterLifecycleBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "trace.jsonl")
        self.t = DiagTrace()
        self.t._on = True
        self._baseline = len(writer_threads())

    def tearDown(self):
        self.t.stop_output(timeout=3.0)
        time.sleep(0.3)
        self.tmp.cleanup()

    def assertNoLeak(self):
        self.assertEqual(len(writer_threads()), self._baseline,
                         "writer 线程泄漏：%d -> %d"
                         % (self._baseline, len(writer_threads())))


class TestStartOutputContract(WriterLifecycleBase):
    def test_success_returns_path_and_creates_file(self):
        ret = self.t.start_output(self.path, interval_s=0.2)
        self.assertEqual(ret, self.path)
        self.assertTrue(os.path.exists(self.path))
        self.assertTrue(self.t.writer_alive)

    def test_unwritable_path_returns_none_and_records_error(self):
        bad = os.path.join(self.tmp.name, "no_such_dir", "x.jsonl")
        ret = self.t.start_output(bad, interval_s=0.2)
        self.assertIsNone(ret, "不可写路径竟然报告成功")
        self.assertIsNotNone(self.t.last_output_error)
        self.assertFalse(os.path.exists(bad))
        time.sleep(0.4)
        self.assertFalse(self.t.writer_alive)
        self.assertNoLeak()

    def test_does_not_open_file_at_import(self):
        # 模块导入不得创建任何文件（本测试自身即验证：未调用 start_output）
        self.assertFalse(os.path.exists(self.path))

    def test_repeated_start_does_not_create_second_writer(self):
        p1 = self.t.start_output(self.path, interval_s=0.2)
        p2 = self.t.start_output(self.path, interval_s=0.2)
        self.assertEqual(p1, p2)
        self.assertEqual(len(writer_threads()), self._baseline + 1)


class TestStopOutputContract(WriterLifecycleBase):
    def test_stop_returns_true_and_thread_exits(self):
        self.t.start_output(self.path, interval_s=0.2)
        time.sleep(0.5)
        self.assertTrue(self.t.stop_output(timeout=3.0))
        time.sleep(0.2)
        self.assertFalse(self.t.writer_alive)
        self.assertNoLeak()

    def test_repeated_stop_is_idempotent(self):
        self.t.start_output(self.path, interval_s=0.2)
        self.assertTrue(self.t.stop_output(timeout=3.0))
        self.assertTrue(self.t.stop_output(timeout=3.0))
        self.assertTrue(self.t.stop_output(timeout=3.0))
        self.assertNoLeak()

    def test_no_thread_accumulation_over_many_cycles(self):
        for _ in range(4):
            self.t.start_output(self.path, interval_s=0.2)
            time.sleep(0.25)
            self.assertTrue(self.t.stop_output(timeout=3.0))
            time.sleep(0.15)
        self.assertNoLeak()

    def test_file_handle_released_after_stop(self):
        """停止后文件句柄必须释放：Windows 上能删除即证明已释放。"""
        self.t.start_output(self.path, interval_s=0.2)
        time.sleep(0.5)
        self.assertTrue(self.t.stop_output(timeout=3.0))
        time.sleep(0.3)
        try:
            os.remove(self.path)
        except OSError as e:
            self.fail("停止后文件句柄未释放: %s" % e)

    def test_stop_without_start_is_true_and_safe(self):
        self.assertTrue(self.t.stop_output(timeout=0.5))


class TestWriterOutput(WriterLifecycleBase):
    def test_writer_writes_jsonl_lines(self):
        self.t.start_output(self.path, interval_s=0.2)
        for seq in range(1, 40):
            self.t.note(S_STORE, seq)
            self.t.note(S_BRIDGE, seq)
        time.sleep(1.0)
        self.t.stop_output(timeout=3.0)
        with open(self.path, encoding="utf-8") as f:
            lines = [l for l in f if l.strip()]
        self.assertGreaterEqual(len(lines), 1)
        snap = json.loads(lines[-1])
        self.assertIn("window", snap)
        self.assertIn("cumulative", snap)
        self.assertIn("read", snap)

    def test_write_failure_is_observable(self):
        """把 json.dumps 打坏 → 必须计入 output_errors，不得静默。"""
        self.t.start_output(self.path, interval_s=0.2)
        orig = diag_trace.json.dumps

        def boom(*a, **kw):
            raise OSError("simulated disk failure")

        diag_trace.json.dumps = boom
        try:
            self.t.note(S_STORE, 1)
            time.sleep(0.9)
        finally:
            diag_trace.json.dumps = orig
        self.t.stop_output(timeout=3.0)
        self.assertGreater(self.t.output_errors, 0,
                           "写盘失败未被记录")
        self.assertIsNotNone(self.t.last_output_error)

    def test_snapshot_failure_is_observable(self):
        self.t.start_output(self.path, interval_s=0.2)
        orig = DiagTrace.snapshot

        def boom(self):
            raise RuntimeError("simulated snapshot failure")

        DiagTrace.snapshot = boom
        try:
            time.sleep(0.9)
        finally:
            DiagTrace.snapshot = orig
        self.t.stop_output(timeout=3.0)
        self.assertGreater(self.t.output_errors, 0)
        self.assertIn("snapshot", (self.t.last_output_error or ""))


if __name__ == "__main__":
    unittest.main()
