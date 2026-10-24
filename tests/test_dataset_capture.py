#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
数据集采集旁路（Dataset Capture）—— 验证脚本

开关（单一入口）：
    ``ocr.dataset_capture.DATASET_CAPTURE_ENABLED``，默认 ``True``。
    ``gui.main_window`` 在构造时读它一次：False 时数据集入口不创建、
    Recorder 不构造、不建队列 / 不起 writer 线程 / 不落任何文件。

覆盖：
    A. Recorder 本体（不需要 GUI）：合法读法落盘、非法读法进 rejected、
       三种格式计数、完全相同图片只记 duplicate、队列满丢帧不阻塞、
       stop() 之后 dataset.json 与 manifest.sha256 齐备且可核验。
    B. 开关 True：入口可见、Recorder 被构造、capture_sink 是转发对象、
       起停一次会真的创建目录 / 打开诊断 / 停止后恢复诊断原状。
    C. 开关 False：入口不存在、Recorder 为 None、capture_sink 就是原来的
       SampleCapture、没有 DatasetWriter 线程、不产生任何数据集目录
       —— 除了开关本身，就像从来没有增加过这个功能。
    D. 生产旁路没有被改坏：SampleCapture 仍然照常工作（两个 sink 都在转发）。

约定与仓库其它 test_*.py 一致：扁平脚本、check()/section() 自行统计，
直接 ``python test_dataset_capture.py`` 运行，不依赖 pytest。
APPDATA / LOCALAPPDATA 指向独立临时目录，退出时精确还原；不读写真实用户数据。
"""

import csv
import glob
import os
import shutil
import sys
import tempfile
import threading
import time

import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# ----------------------------------------------------------------------
# 测试环境隔离 —— 必须在导入任何项目模块之前完成。
# MainWindow 会创建 LicenseManager / FirstRunStore / StreamHistory，
# 分别读写 %APPDATA% 与 %LOCALAPPDATA%。
# 两个变量都**直接赋值**（不用 setdefault：Windows 上本来就有值，会静默失效）。
# ----------------------------------------------------------------------
_TEMP_ROOT = tempfile.mkdtemp(prefix="dlv-dscap-")
_TEMP_APPDATA = os.path.join(_TEMP_ROOT, "appdata")
_TEMP_LOCALAPPDATA = os.path.join(_TEMP_ROOT, "localappdata")
os.makedirs(_TEMP_APPDATA, exist_ok=True)
os.makedirs(_TEMP_LOCALAPPDATA, exist_ok=True)
_SAVED_ENV = {k: os.environ.get(k) for k in ("APPDATA", "LOCALAPPDATA")}
os.environ["APPDATA"] = _TEMP_APPDATA
os.environ["LOCALAPPDATA"] = _TEMP_LOCALAPPDATA
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

import gui.main_window as mw  # noqa: E402
from ocr.dataset_capture import (  # noqa: E402
    CaptureFanout, DatasetRecorder, match_format,
    raw_text_from_readings,
)
from ocr.sample_capture import SampleCapture  # noqa: E402

app = QApplication.instance() or QApplication([])

_PASSED = 0
_FAILED = 0
_WINDOWS = []
_TMP_DIRS = []


def check(condition, message, extra=""):
    global _PASSED, _FAILED
    if condition:
        _PASSED += 1
        print("  [ok]   %s" % message, flush=True)
    else:
        _FAILED += 1
        print("  [FAIL] %s %s" % (message, extra), flush=True)
    return bool(condition)


def section(title):
    print("\n" + title, flush=True)


def pump(rounds: int = 20, delay: float = 0.005) -> None:
    for _ in range(rounds):
        app.processEvents()
        time.sleep(delay)


def tmpdir(prefix="dscap"):
    d = tempfile.mkdtemp(prefix="%s-" % prefix)
    _TMP_DIRS.append(d)
    return d


def roi_image(seed=0, shape=(60, 200, 3)):
    """一张合成的 ROI 图。**只用于测试夹具**；真实采集走 FFmpegReader。"""
    rng = np.random.default_rng(seed)
    return rng.integers(0, 255, shape, dtype="uint8")


class _StubActivation:
    """替掉激活对话框，避免构造主窗口时弹模态窗。"""

    def __init__(self, *a, **k):
        pass

    def exec(self):
        return 0

    def result_data(self):
        return None


def build_window(enabled: bool):
    """按指定开关值构造一个主窗口。开关在构造时被读取。"""
    real_flag = mw.DATASET_CAPTURE_ENABLED
    real_dialog = mw.ActivationDialog
    mw.DATASET_CAPTURE_ENABLED = enabled
    mw.ActivationDialog = _StubActivation
    try:
        window = mw.MainWindow()
        window.resize(1100, 660)
    finally:
        mw.DATASET_CAPTURE_ENABLED = real_flag
        mw.ActivationDialog = real_dialog
    _WINDOWS.append(window)
    pump(10)
    return window


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


# ======================================================================
# A. Recorder 本体
# ======================================================================
def section_a():
    section("A. Recorder 本体（不需要 GUI）")

    root = os.path.join(tmpdir("rec"), "LiveLens-OCR-Dataset-TEST")
    rec = DatasetRecorder(root=root, sample_interval_ms=0)
    rec.start()

    # 合法读法 ×3 格式
    rec.capture(roi_image(1), readings=[("ppocr", "AEL893")])      # aaa123
    rec.capture(roi_image(2), readings=[("ppocr", "BMQ867")])      # aaa123
    rec.capture(roi_image(3), readings=[("ppocr", "SAFI9935")])    # aaaa1234
    rec.capture(roi_image(4), readings=[("ppocr", "ABCD123")])     # bbbb123
    time.sleep(0.4)
    rec.stop()

    check(rec.stats.per_format.get("aaa123") == 2, "aaa123 计数 = 2",
          rec.stats.per_format)
    check(rec.stats.per_format.get("bbbb123") == 1, "bbbb123 计数 = 1")
    check(rec.stats.per_format.get("aaaa1234") == 1, "aaaa1234 计数 = 1")
    check(rec.stats.total == 4, "Total = 4")
    check(rec.stats.rejected == 0, "合法读法不进 rejected")

    samples = read_csv(os.path.join(root, "samples.csv"))
    labels = read_csv(os.path.join(root, "labels.csv"))
    teacher = read_csv(os.path.join(root, "teacher_raw.csv"))
    check(len(samples) == 4, "samples.csv 有 4 行", len(samples))
    check(list(samples[0].keys()) == ["sample_id", "image", "format_id",
                                      "timestamp_ms"], "samples.csv 列正确")
    check(len(labels) == 4 and labels[0]["ground_truth"] == "AEL893",
          "labels.csv 记录的是**原始读法**")
    check(len(teacher) == 4 and "confidence" in teacher[0],
          "teacher_raw.csv 有 confidence 列")
    check(all(r["image"] and os.path.isfile(
        os.path.join(root, r["image"].replace("/", os.sep))) for r in samples),
        "4 张 PNG 全部落盘")
    check(len(glob.glob(os.path.join(root, "images", "*.png"))) == 4,
          "images/ 里正好 4 张")

    # ---- 非法读法 ----
    root2 = os.path.join(tmpdir("rec2"), "ds")
    rec2 = DatasetRecorder(root=root2, sample_interval_ms=0)
    rec2.start()
    rec2.capture(roi_image(11), readings=[("ppocr", "abc123")])   # 小写
    rec2.capture(roi_image(12), readings=[("ppocr", "31:32")])    # 符号
    rec2.capture(roi_image(13), readings=[("ppocr", "ABC12")])    # 长度错
    rec2.capture(roi_image(14), readings=[("ppocr", "")])         # 空结果
    rec2.capture(roi_image(15), readings=None)                    # 诊断没开 -> 无原文
    time.sleep(0.4)
    rec2.stop()
    rej = read_csv(os.path.join(root2, "rejected.csv"))
    check(rec2.stats.total == 0, "非法读法一张都不进正式数据集")
    check(rec2.stats.rejected == 5, "5 条进 rejected", rec2.stats.rejected)
    check(len(rej) == 5, "rejected.csv 有 5 行", len(rej))
    check(any(r["reason"] == "empty" for r in rej), "空读法记为 empty")
    check(any(r["raw_text"] == "abc123" for r in rej),
          "小写原文被拒绝且**未被纠正**成合法码")
    check(os.path.isfile(os.path.join(root2, "labels.csv"))
          and len(read_csv(os.path.join(root2, "labels.csv"))) == 0,
          "labels.csv 只有表头")

    # ---- 完全相同图片 ----
    root3 = os.path.join(tmpdir("rec3"), "ds")
    rec3 = DatasetRecorder(root=root3, sample_interval_ms=0)
    rec3.start()
    same = roi_image(99)
    rec3.capture(same, readings=[("ppocr", "AEL893")])
    rec3.capture(same.copy(), readings=[("ppocr", "AEL893")])
    time.sleep(0.4)
    rec3.stop()
    check(rec3.stats.total == 1, "完全相同的图片只进数据集一次")
    check(rec3.stats.duplicate == 1, "重复被计数")
    rej3 = read_csv(os.path.join(root3, "rejected.csv"))
    check(any("duplicate_exact" in r["reason"] for r in rej3),
          "重复原因写进 rejected.csv")

    # ---- 采样间隔 ----
    root4 = os.path.join(tmpdir("rec4"), "ds")
    rec4 = DatasetRecorder(root=root4, sample_interval_ms=5000)
    rec4.start()
    for i in range(20):
        rec4.capture(roi_image(200 + i), readings=[("ppocr", "AEL893")])
    time.sleep(0.3)
    rec4.stop()
    check(rec4.stats.total == 1, "采样间隔生效：只接受第一帧",
          rec4.stats.total)
    check(rec4.stats.skipped_interval >= 15, "其余被间隔跳过",
          rec4.stats.skipped_interval)

    # ---- stop() 之后齐备且可核验 ----
    for name in ("dataset.json", "manifest.sha256", "README.md"):
        check(os.path.isfile(os.path.join(root, name)), "stop() 后存在 %s" % name)
    listed = [l for l in open(os.path.join(root, "manifest.sha256"),
                              encoding="utf-8") if l.strip()]
    check(len(listed) >= 5, "manifest 覆盖多个文件", len(listed))
    check(all("manifest.sha256" not in l for l in listed), "manifest 不自指")

    # ---- 队列满不阻塞 ----
    root5 = os.path.join(tmpdir("rec5"), "ds")
    rec5 = DatasetRecorder(root=root5, sample_interval_ms=0, queue_size=2)
    rec5.start()          # 不 start 的话 offer() 会走"未运行"早退，测不出丢帧
    # 图必须**预先**造好：在循环里现造 200x60x3 随机数组会比 writer 还慢，
    # 队列永远填不满，就测不出"满了会丢帧"这件事（第一版就是这么误判的）。
    pool = [roi_image(300 + i) for i in range(6)]
    t0 = time.perf_counter()
    accepted = 0
    for i in range(2000):
        if rec5.offer(pool[i % 6], "AEL893"):
            accepted += 1
    dt = (time.perf_counter() - t0) * 1000.0
    rec5.stop()
    check(dt < 2000, "2000 次 offer 在 2s 内返回（队列满只丢帧不阻塞）",
          "%.0f ms" % dt)
    check(rec5.stats.dropped > 1000, "队列满时大量丢帧且被计数",
          "dropped=%d accepted=%d" % (rec5.stats.dropped, accepted))
    check(accepted + rec5.stats.dropped == 2000, "接受 + 丢弃 == 投递总数",
          "%d + %d" % (accepted, rec5.stats.dropped))


# ======================================================================
# B. 开关 True
# ======================================================================
def section_b():
    section("B. DATASET_CAPTURE_ENABLED = True")
    w = build_window(True)

    check(getattr(w, "_dataset_btn", None) is not None, "数据集采集入口已创建")
    check(w._dataset_btn.isVisibleTo(w), "入口可见")
    check(w._dataset_btn.text() == "数据集采集", "入口文案正确")
    check(w._dataset_recorder is not None, "Recorder 已创建")
    check(isinstance(w._capture_sink, CaptureFanout), "capture_sink 是转发对象")
    check(any(isinstance(s, SampleCapture) for s in w._capture_sink.sinks),
          "转发对象仍包含原有 SampleCapture（生产旁路未被替换掉）")
    check(any(s is w._dataset_recorder for s in w._capture_sink.sinks),
          "转发对象包含 DatasetRecorder")
    check(not w._dataset_recorder.running, "构造时不启动（惰性）")
    check(w._dataset_recorder.root is None, "构造时不建目录")

    # 起停一次
    rc_root = os.path.join(tmpdir("gui"), "LiveLens-OCR-Dataset-GUI")
    w._dataset_recorder._explicit_root = rc_root
    diag_before = w._diagnostics.enabled
    w._on_dataset_capture_toggled()
    pump(6)
    check(w._dataset_recorder.running, "点击后开始采集")
    check(os.path.isdir(os.path.join(rc_root, "images")), "目录被创建")
    check(w._diagnostics.enabled, "采集期间诊断被打开（原始读法才拿得到）")
    check(w._dataset_btn.text() == "停止采集", "按钮文案切换")

    # 走一遍真实喂数据：worker 用的就是这个签名
    w._capture_sink.capture(roi_image(7), profile_id="valorant",
                            readings=[("ppocr", "AEL893")], candidate="AEL893",
                            note="")
    time.sleep(0.4)
    w._refresh_dataset_status()
    check(w._dataset_recorder.stats.total == 1, "经 capture_sink 的数据被记录")
    status = w._dataset_status.text()
    check("AAA123" in status and "Total" in status,
          "UI 显示三种格式与 Total 计数", repr(status))
    check(w._dataset_recorder.stats.snapshot()["per_format"]["aaa123"] == 1,
          "UI 读到的计数里 aaa123 = 1")

    w._on_dataset_capture_toggled()
    pump(6)
    check(not w._dataset_recorder.running, "再次点击停止采集")
    check(os.path.isfile(os.path.join(rc_root, "dataset.json")),
          "停止后写了 dataset.json")
    check(w._diagnostics.enabled == diag_before, "诊断恢复到采集前的状态")


# ======================================================================
# C. 开关 False
# ======================================================================
def section_c():
    section("C. DATASET_CAPTURE_ENABLED = False")
    before_threads = {t.name for t in threading.enumerate()}
    docs_probe = os.path.join(os.path.expanduser("~"), "Documents")
    before_dirs = set(glob.glob(os.path.join(docs_probe, "LiveLens-OCR-Dataset*")))

    w = build_window(False)
    pump(6)

    check(not hasattr(w, "_dataset_btn"), "入口控件根本不存在（不是隐藏）")
    check(w._dataset_recorder is None, "Recorder 为 None")
    check(w._capture_sink is w._sample_capture,
          "capture_sink 就是原来那个 SampleCapture（worker 侧无感）")
    check(not isinstance(w._capture_sink, CaptureFanout), "没有引入转发对象")
    after_threads = {t.name for t in threading.enumerate()}
    check("DatasetWriter" not in (after_threads - before_threads),
          "没有起 DatasetWriter 线程")
    check(not any(t.name == "DatasetWriter" for t in threading.enumerate()),
          "进程里没有 DatasetWriter 线程")
    after_dirs = set(glob.glob(os.path.join(docs_probe, "LiveLens-OCR-Dataset*")))
    check(after_dirs == before_dirs, "没有创建任何数据集目录",
          after_dirs - before_dirs)

    # 关闭时生产旁路照常工作：SampleCapture 仍能被调用
    tmp = tmpdir("bypass")
    plain = SampleCapture(tmp)
    plain.set_enabled(True)
    plain.capture(roi_image(31), profile_id="valorant", readings=[], candidate="")
    check(len(plain.records) == 1, "关闭时 SampleCapture 仍照常采集（旁路未坏）")


# ======================================================================
# D. 纯函数
# ======================================================================
def section_d():
    section("D. 格式判定与原文提取")
    for text, want in (("AEL893", "aaa123"), ("BBBB123", "bbbb123"),
                       ("ABCD123", "bbbb123"), ("SAFI9935", "aaaa1234"),
                       ("abc123", None), ("ABCDEF", None), ("31:32", None),
                       ("", None), ("ABCD12345", None), ("AEL89", None)):
        check(match_format(text) == want, "match_format(%r) == %r" % (text, want))
    check(raw_text_from_readings([("ppocr", "AEL893")]) == "AEL893",
          "从 readings 取原始读法")
    check(raw_text_from_readings(None) == "",
          "readings 为 None 时返回空（不拿候选冒充原文）")


def main():
    print("=" * 68)
    print("数据集采集旁路 —— 验证")
    print("=" * 68)
    section_a()
    section_b()
    section_c()
    section_d()
    print("\n" + "=" * 68)
    print("PASS %d / FAIL %d" % (_PASSED, _FAILED))
    print("=" * 68)
    return 1 if _FAILED else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        for w in _WINDOWS:
            try:
                w.close()
            except Exception:
                pass
        app.processEvents()
        for d in _TMP_DIRS:
            shutil.rmtree(d, ignore_errors=True)
        shutil.rmtree(_TEMP_ROOT, ignore_errors=True)
        for k, v in _SAVED_ENV.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    sys.exit(code)
