# -*- coding: utf-8 -*-
"""H6 生产采集自动启动集成测试。

契约：
    - 在 import 前设置 DLV_DIAG_TRACE=1 / DLV_STDERR_LOG=1 的子进程中，
      模块导入即自动启动输出（此前无人调用 start_output，生产环境
      只设变量不会产生任何文件——H6 修复的真实缺口）。
    - 输出文件默认落在 %TEMP%，文件名含进程号。
    - 未启用时（默认）不创建任何文件、不建线程。
    - 启动失败（不可写路径）不抛出、不阻塞导入。
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

CHILD = r"""
import sys, time, os
sys.path.insert(0, ROOT)
os.chdir(ROOT)
import utils.diag_trace as dt
import utils.stderr_recorder as sr
# 产生事件并给 writer 线程时间落盘
for seq in range(1, 40):
    dt.TRACE.note(dt.S_STORE, seq)
sr.STDERR_RECORDER.feed("simulated stderr line")
time.sleep(1.8)
snap = dt.TRACE.snapshot()
print("TRACE_PATH=%s" % (dt.TRACE.output_path or ""))
print("SR_PATH=%s" % (sr.STDERR_RECORDER.output_path or ""))
print("TRACE_ALIVE=%s" % dt.TRACE.writer_alive)
print("SR_WRITTEN=%d" % sr.STDERR_RECORDER.written)
""".replace("ROOT", repr(ROOT))


def run_child(env_extra):
    env = dict(os.environ)
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-c", CHILD],
        capture_output=True, text=True, timeout=30, env=env, cwd=ROOT)


class TestProdAutoStart(unittest.TestCase):
    def test_enabled_trace_autostarts_and_writes(self):
        r = run_child({"DLV_DIAG_TRACE": "1"})
        out = r.stdout
        path = None
        for line in out.splitlines():
            if line.startswith("TRACE_PATH="):
                path = line.split("=", 1)[1]
        self.assertTrue(path, "TRACE 输出路径未返回:\n%s%s" % (out, r.stderr))
        self.assertTrue(os.path.exists(path), "追踪文件未创建: %s" % path)
        with open(path, encoding="utf-8") as f:
            lines = [l for l in f if l.strip()]
        self.assertGreaterEqual(len(lines), 1, "追踪文件为空")
        snap = json.loads(lines[-1])
        self.assertIn("counters", snap)

    def test_enabled_stderr_autostarts_and_writes(self):
        r = run_child({"DLV_STDERR_LOG": "1"})
        path = None
        written = -1
        for line in r.stdout.splitlines():
            if line.startswith("SR_PATH="):
                path = line.split("=", 1)[1]
            if line.startswith("SR_WRITTEN="):
                written = int(line.split("=", 1)[1])
        self.assertTrue(path, "stderr 输出路径未返回:\n%s%s" % (r.stdout, r.stderr))
        self.assertTrue(os.path.exists(path))
        self.assertGreaterEqual(written, 1, "stderr 行未落盘")

    def test_disabled_creates_nothing(self):
        r = run_child({})
        self.assertIn("TRACE_PATH=", r.stdout)
        for line in r.stdout.splitlines():
            if line.startswith("TRACE_PATH="):
                self.assertEqual(line.split("=", 1)[1], "")
            if line.startswith("SR_PATH="):
                self.assertEqual(line.split("=", 1)[1], "")

    def test_unwritable_path_does_not_crash_import(self):
        bad = os.path.join(tempfile.gettempdir(), "_no_such_dir_h6_%d" % os.getpid())
        r = run_child({"DLV_DIAG_TRACE": "1", "DLV_DIAG_TRACE_FILE": os.path.join(bad, "x.jsonl")})
        self.assertEqual(r.returncode, 0, "不可写路径导致导入崩溃: %s" % r.stderr)
        self.assertIn("TRACE_PATH=", r.stdout)


if __name__ == "__main__":
    unittest.main()
