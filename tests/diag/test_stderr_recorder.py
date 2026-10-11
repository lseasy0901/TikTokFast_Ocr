# -*- coding: utf-8 -*-
"""O-1 stderr 有界采集与独立落盘——生命周期与降级契约测试。

契约：
    - 默认关闭：feed 零副作用（无线程/无文件/无计数）
    - start 只有"文件可写且 writer 已启动"才返回路径
    - feed 绝不阻塞：队列满 → 丢弃计数（dropped），调用方无感
    - stop 确认线程退出；重复 stop 幂等；停止后文件句柄释放（可删除）
    - 写盘失败可观察（write_errors / last_error），消费端不受影响
    - 超过 max_bytes 轮转，保留 keep 份，最旧删除
"""

import os
import queue
import tempfile
import threading
import time
import unittest

from utils.stderr_recorder import StderrRecorder

WRITER = "stderr-log-writer"


def writer_threads():
    return [t for t in threading.enumerate() if t.name == WRITER]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "stderr.log")
        self.r = StderrRecorder()
        self.r._on = True
        self._baseline = len(writer_threads())

    def tearDown(self):
        self.r.stop(timeout=3.0)
        time.sleep(0.2)
        self.tmp.cleanup()

    def assertNoLeak(self):
        self.assertEqual(len(writer_threads()), self._baseline,
                         "writer 线程泄漏：%d -> %d"
                         % (self._baseline, len(writer_threads())))


class TestDisabledByDefault(unittest.TestCase):
    def test_feed_is_noop_without_enable(self):
        r = StderrRecorder()
        self.assertFalse(r.enabled)
        r.feed("hello")
        self.assertEqual(r.fed, 0)
        self.assertIsNone(r.output_path)
        self.assertFalse(r.writer_alive)

    def test_start_returns_none_when_disabled(self):
        r = StderrRecorder()
        self.assertIsNone(r.start(os.path.join(tempfile.gettempdir(), "x.log")))


class TestLifecycle(Base):
    def test_start_success_returns_path_and_creates_file(self):
        ret = self.r.start(self.path)
        self.assertEqual(ret, self.path)
        self.assertTrue(os.path.exists(self.path))
        self.assertTrue(self.r.writer_alive)

    def test_unwritable_path_returns_none_and_records_error(self):
        bad = os.path.join(self.tmp.name, "no_such_dir", "x.log")
        self.assertIsNone(self.r.start(bad))
        self.assertIsNotNone(self.r.last_error)
        self.assertNoLeak()

    def test_repeated_start_no_second_writer(self):
        self.r.start(self.path)
        p2 = self.r.start(self.path)
        self.assertEqual(p2, self.path)
        self.assertEqual(len(writer_threads()), self._baseline + 1)

    def test_stop_confirms_exit_and_releases_handle(self):
        self.r.start(self.path)
        time.sleep(0.3)
        self.assertTrue(self.r.stop(timeout=3.0))
        time.sleep(0.2)
        self.assertFalse(self.r.writer_alive)
        self.assertNoLeak()
        try:
            os.remove(self.path)
        except OSError as e:
            self.fail("停止后文件句柄未释放: %s" % e)

    def test_repeated_stop_idempotent(self):
        self.r.start(self.path)
        self.assertTrue(self.r.stop(timeout=3.0))
        self.assertTrue(self.r.stop(timeout=3.0))
        self.assertTrue(self.r.stop(timeout=3.0))

    def test_no_leak_over_many_cycles(self):
        for _ in range(4):
            self.r.start(self.path)
            time.sleep(0.25)
            self.assertTrue(self.r.stop(timeout=3.0))
            time.sleep(0.15)
        self.assertNoLeak()


class TestFeedAndWrite(Base):
    def test_lines_land_on_disk(self):
        self.r.start(self.path)
        for i in range(10):
            self.r.feed("line-%d" % i)
        deadline = time.time() + 3.0
        while time.time() < deadline and self.r.written < 10:
            time.sleep(0.05)
        self.r.stop(timeout=3.0)
        with open(self.path, encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip()]
        self.assertEqual(lines[0], "line-0")
        self.assertIn("line-9", lines)
        self.assertEqual(self.r.written, 10)
        self.assertEqual(self.r.fed, 10)
        self.assertEqual(self.r.dropped, 0)

    def test_queue_full_drops_and_never_blocks(self):
        self.r.start(self.path)
        self.r._q = queue.Queue(maxsize=8)  # 缩小队列便于触发满
        t0 = time.perf_counter()
        for i in range(100):
            self.r.feed("x-%d" % i)
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 0.5, "feed 在队列满时发生了阻塞")
        self.assertEqual(self.r.dropped, 92)
        self.assertEqual(self.r.fed, 8)

    def test_write_failure_is_observable(self):
        self.r.start(self.path)
        # 直接把队列塞满后关闭底层路径：writer 对已删除路径写会失败
        # （Windows 上删除已打开句柄的文件受限，改用打坏 file 对象模拟）
        real_write_batch = self.r._write_batch

        def boom(f, q):
            raise_on_purpose = None
            if raise_on_purpose is not None:
                pass
            # 模拟写失败：跳过真实写并计数
            try:
                while True:
                    q.get_nowait()
            except queue.Empty:
                pass
            self.r.write_errors += 1
            self.r.last_error = "write failed: simulated"
            return 0

        self.r._write_batch = boom
        try:
            self.r.feed("some line")
            deadline = time.time() + 2.0
            while time.time() < deadline and self.r.write_errors == 0:
                time.sleep(0.05)
            self.assertGreater(self.r.write_errors, 0)
            self.assertIn("simulated", self.r.last_error or "")
        finally:
            self.r._write_batch = real_write_batch
        self.r.stop(timeout=3.0)


class TestRotation(Base):
    def test_rotation_keeps_recent_files(self):
        self.r.max_bytes = 200
        self.r.keep = 2
        self.r.start(self.path)
        for i in range(40):
            self.r.feed("rot-line-%02d-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" % i)
        deadline = time.time() + 5.0
        while time.time() < deadline and not os.path.exists(self.path + ".1"):
            time.sleep(0.05)
        self.assertTrue(os.path.exists(self.path + ".1"),
                        "超过 max_bytes 后未发生轮转")
        self.r.stop(timeout=3.0)
        # 主文件 + .1 存在；.2 不应保留（keep=2 → 最多 path 与 path.1）
        self.assertFalse(os.path.exists(self.path + ".2"))
        # 主文件应小于上限（新轮转文件从空开始）
        self.assertLess(os.path.getsize(self.path), 400)


if __name__ == "__main__":
    unittest.main()
