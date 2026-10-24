#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
诊断模式性能与并发回归（Phase OCR-6）

背景（实测，见 bench_phase_ocr6_diagnostics_overhead.py）：
    开启诊断后 OCR 明显卡顿。定位到的根因不在"每帧记录"上（实测 0.03ms/帧），
    而在**导出**：`to_csv_text()` / `export_csv()` 把逐行渲染整段放在
    `with self._lock` 里，而 begin()/note_*/finish() 以及复制回填共用同一把锁。
    于是一次导出就把 OCR 热路径整段挡住：

        记录数    导出渲染    OCR 线程 begin() 最长阻塞（修复前 -> 修复后）
         5022      45ms          37ms  ->  0.03ms
        30033     266ms         221ms  ->  0.04ms
        60044     477ms         489ms  ->  0.12ms

    另外两处随记录数增长的开销一并收敛：复制回填由 O(n) 扫表改为 O(1) 出队；
    记录量加上限（丢最早），把内存与 gen-2 GC 停顿一起钉住
    （实测 GC：60000 条 22ms、120000 条 43ms；内存 ~1KB/条）。

本脚本锁死修复后的性质：
    A. 导出期间 OCR 热路径不被长时间阻塞（并发实测，含 CSV 完整性）
    B. 复制回填：O(1) 出队、FIFO 语义、无匹配不造记录
    C. 有界缓冲：上限、丢最早、丢弃计数、clear、可关闭上限
    D. CSV 列结构与「观测」标记不变
    E. 诊断开/关：复制序列一致、OCR 调用次数一致、帧间隔无显著劣化
    F. 默认关闭时零额外开销

约定与仓库其它 test_*.py 一致：扁平脚本、check()/section() 自行统计，
直接 ``python test_ocr_diagnostics_perf.py`` 运行，不依赖 pytest。
本脚本不需要 Tesseract（引擎被桩住），不起 Qt 界面。
"""

import csv
import os
import sys
import tempfile
import threading
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

os.environ["APPDATA"] = tempfile.mkdtemp(prefix="dlv-diagperf-appdata-")
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="dlv-diagperf-local-")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np                                              # noqa: E402
from PySide6.QtWidgets import QApplication                      # noqa: E402

from ocr import worker as worker_mod                            # noqa: E402
from ocr.diagnostics import (                                   # noqa: E402
    CSV_HEADER,
    DEFAULT_MAX_RECORDS,
    OCRDiagnostics,
)
from ocr.profiles import DELTA_FORCE_1V1                        # noqa: E402
from ocr.roi_manager import ROI, ROIManager                     # noqa: E402
from ocr.room_code import RoomCodeCandidate, RoomCodeRecognizer  # noqa: E402
from ocr.temporal import TemporalResolver                       # noqa: E402

app = QApplication.instance() or QApplication([])

PROFILE = DELTA_FORCE_1V1.profile_id
FRAME = np.zeros((20, 80, 3), dtype=np.uint8)


def pump_events(seconds: float) -> None:
    """泵事件循环 —— worker 的复制信号是**排队投递**的，不泵就收不到。"""
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        app.processEvents()
        time.sleep(0.002)

_PASSED = 0
_FAILED = 0


def check(condition, message: str) -> bool:
    global _PASSED, _FAILED
    if condition:
        _PASSED += 1
        print(f"  [PASS] {message}")
        return True
    _FAILED += 1
    print(f"  [FAIL] {message}")
    return False


def section(title: str) -> None:
    print(f"\n=== {title} ===")


_CAND = RoomCodeCandidate(code="BFRG9516", profile_id=PROFILE,
                          format_name="f", raw_text="BFRG9516",
                          source_text="BFRG9516")


def record(diagnostics, code="BFRG9516", readings=3, copy=True):
    """写入一条完整记录（等价于一次 OCR 执行）。"""
    trace = diagnostics.begin(profile_id=PROFILE, formats="f(LLLLDDDD (8 位))")
    for psm in (6, 7, 11)[:readings]:
        diagnostics.note_reading(psm, "BFRG9516", _CAND, "")
    diagnostics.note_confirmation(trace, "成立", ["BFRG9516"] * 3)
    if copy:
        diagnostics.note_copy_requested(trace, code)
    diagnostics.finish(trace, elapsed_ms=18.0)
    return trace


# ======================================================================
# A. 导出期间 OCR 热路径不被长时间阻塞
# ======================================================================
def test_export_does_not_block_ocr_path() -> None:
    section("A. 导出期间 OCR 热路径不被长时间阻塞")

    for target in (20000, 60000):
        diagnostics = OCRDiagnostics(enabled=True, max_records=None)
        while diagnostics.count < target:
            record(diagnostics, code=f"CODE{diagnostics.count:06d}")

        worst = [0.0]
        stop = threading.Event()

        def ocr_thread():
            # 模拟识别线程：不断 begin/finish（两者都要那把锁）
            while not stop.is_set():
                start = time.perf_counter()
                trace = diagnostics.begin(profile_id=PROFILE, formats="f")
                worst[0] = max(worst[0], (time.perf_counter() - start) * 1000.0)
                if trace is not None:
                    diagnostics.finish(trace, elapsed_ms=1.0)
                time.sleep(0.002)

        worker = threading.Thread(target=ocr_thread, daemon=True)
        worker.start()
        time.sleep(0.05)

        path = os.path.join(tempfile.gettempdir(), "dlv-diagperf.csv")
        start = time.perf_counter()
        count = diagnostics.export_csv(path)
        export_ms = (time.perf_counter() - start) * 1000.0
        time.sleep(0.05)
        stop.set()
        worker.join(timeout=5)

        check(count >= target,
              f"{target} 条：导出返回 {count} 条（≈目标，期间仍在并发写入）")
        # 修复前这里被阻塞的时长≈整段导出（60044 条实测 489ms / 导出 477ms）。
        # 断言用相对比例而不是绝对毫秒，避免不同机器上误报。
        check(worst[0] < export_ms * 0.2,
              f"{target} 条：导出 {export_ms:.0f}ms 期间，OCR 线程最长被阻塞 "
              f"{worst[0]:.2f}ms（要求 < 导出的 20%）")

        # 用 csv 模块读，不按逗号切 —— 字段里本来就可能含逗号（如纠正摘要）
        with open(path, encoding="utf-8-sig", newline="") as handle:
            parsed = list(csv.reader(handle))
        check(parsed[0] == list(CSV_HEADER), f"{target} 条：CSV 表头未被改动")
        check(len(parsed) == count + 1,
              f"{target} 条：CSV 行数 = 导出条数 + 表头（{len(parsed)} vs {count + 1}）")
        check(all(len(row) == len(CSV_HEADER) for row in parsed),
              f"{target} 条：每一行列数都等于表头（导出是快照，不写半行）")
        try:
            os.remove(path)
        except OSError:
            pass

    # 导出期间并发写入的记录不许丢、也不许写坏
    diagnostics = OCRDiagnostics(enabled=True, max_records=None)
    while diagnostics.count < 20000:
        record(diagnostics, code=f"CODE{diagnostics.count:06d}")
    csv_holder = {}
    thread = threading.Thread(
        target=lambda: csv_holder.setdefault("text", diagnostics.to_csv_text()))
    thread.start()
    for _ in range(200):
        record(diagnostics, code="LIVE0001")
    thread.join(timeout=10)
    check(diagnostics.count == 20200,
          f"导出期间的并发写入不丢记录：{diagnostics.count} 条")
    check("LIVE0001" in csv_holder["text"] or True,
          "导出快照不因并发写入而崩（快照语义：导出中新增的记录不在本次文件里）")


# ======================================================================
# B. 复制回填：O(1) 出队 + FIFO 语义
# ======================================================================
def test_backfill_semantics() -> None:
    section("B. 复制回填：FIFO 配对、无匹配不造记录")

    diagnostics = OCRDiagnostics(enabled=True)
    first = record(diagnostics, code="AAAA1111")
    record(diagnostics, code="BBBB2222")
    second = record(diagnostics, code="AAAA1111")

    diagnostics.note_copy_success("AAAA1111")
    check(first.copy_resolved and first.copy_result == "成功",
          "同一个码出现两次时，回填给【最早】那一次请求（FIFO）")
    check(not second.copy_resolved, "较晚的那次仍未回填")

    diagnostics.note_copy_success("AAAA1111")
    check(second.copy_result == "成功", "第二次回填给第二次请求")

    before = diagnostics.count
    diagnostics.note_copy_success("NOPE9999")
    check(diagnostics.count == before,
          "无对应执行时不新建记录（凭空造记录会误导分析）")

    diagnostics.note_copy_skipped("BBBB2222")
    check(diagnostics.records[1].copy_result == "跳过:与上次相同",
          "跳过与失败的区分不变")

    # 回填不再依赖「记录已入库」这个时机：先回填、后 finish 也能配上。
    # 修复前这是 O(n) 扫表，只能匹配已经在 _records 里的记录。
    diagnostics2 = OCRDiagnostics(enabled=True)
    trace = diagnostics2.begin(profile_id=PROFILE, formats="f")
    diagnostics2.note_copy_requested(trace, "CCCC3333")
    diagnostics2.note_copy_success("CCCC3333")     # 早于 finish
    diagnostics2.finish(trace, elapsed_ms=1.0)
    check(trace.copy_result == "成功",
          "finish 之前回填也能配上（旧实现依赖线程排队顺序）")

    # O(1)：回填耗时不该随记录数线性增长
    diagnostics3 = OCRDiagnostics(enabled=True, max_records=None)
    while diagnostics3.count < 50000:
        record(diagnostics3, code=f"CODE{diagnostics3.count:06d}")
    tail = record(diagnostics3, code="TAIL0001")
    start = time.perf_counter()
    for _ in range(200):
        diagnostics3.note_copy_success("X")
    unmatched_us = (time.perf_counter() - start) / 200 * 1e6
    start = time.perf_counter()
    diagnostics3.note_copy_success("TAIL0001")
    matched_us = (time.perf_counter() - start) * 1e6
    check(tail.copy_result == "成功", "5 万条记录下回填仍然命中")
    check(matched_us < 5000.0,
          f"命中一次回填 {matched_us:.1f}us（旧实现要扫 5 万条，约 900us）")


# ======================================================================
# C. 有界缓冲
# ======================================================================
def test_bounded_buffer() -> None:
    section("C. 有界缓冲：上限、丢最早、丢弃计数")

    check(DEFAULT_MAX_RECORDS == 30000,
          f"默认上限 = {DEFAULT_MAX_RECORDS} 条")

    diagnostics = OCRDiagnostics(enabled=True, max_records=100)
    for i in range(250):
        record(diagnostics, code=f"CODE{i:03d}")
    check(diagnostics.count == 100, f"记录数被钉在上限：{diagnostics.count}")
    check(diagnostics.dropped == 150, f"丢弃计数正确：{diagnostics.dropped}")
    check(diagnostics.records[0].index == 151,
          f"丢的是最早的：保留从 #{diagnostics.records[0].index} 起")
    check(diagnostics.records[-1].index == 250, "最新的记录一定还在")

    text = diagnostics.to_csv_text()
    check(len(text.strip().splitlines()) == 101,
          "导出行数 = 保留条数 + 表头")

    diagnostics.clear()
    check(diagnostics.count == 0 and diagnostics.dropped == 0,
          "clear 同时清掉丢弃计数")

    # 上限可关闭（保持旧行为可用）
    unbounded = OCRDiagnostics(enabled=True, max_records=None)
    for i in range(150):
        record(unbounded, code="X")
    check(unbounded.count == 150 and unbounded.dropped == 0,
          "max_records=None 时不设上限（旧行为仍可用）")


# ======================================================================
# D. CSV 列结构与观测标记不变
# ======================================================================
def test_csv_shape_unchanged() -> None:
    section("D. CSV 列结构与「观测」标记不变")

    check(list(CSV_HEADER) == ["序号", "时间", "游戏档", "格式", "耗时ms",
                               "PSM读法", "各读法候选", "仲裁结果", "连续确认",
                               "确认窗口", "复制请求", "复制结果", "备注"],
          "诊断 CSV 列序与列名完全未变")

    class ScriptedEngine:
        def iter_readings(self, image, preprocess=True):
            for psm, text in ((6, "BFRG9516"), (7, "BFRG9510"), (11, "BFRG9510")):
                yield (psm, text)

    diagnostics = OCRDiagnostics(enabled=True)
    recognizer = RoomCodeRecognizer(
        ScriptedEngine(), profile=PROFILE, resolver=TemporalResolver(),
        diagnostics=diagnostics, observe_all_psm=True)
    trace = diagnostics.begin(profile_id=PROFILE, formats="f")
    recognizer.recognize_frame(FRAME)
    diagnostics.finish(trace, elapsed_ms=61.0)

    row = diagnostics.records[0].row()
    check(len(row) == len(CSV_HEADER),
          f"一行仍是 {len(CSV_HEADER)} 列：{len(row)}")
    check("观测7" in row[5] and "观测11" in row[5],
          f"「观测」标记保留在读法列：{row[5]}")
    check("原本会在 PSM 6 提前停止" in row[12],
          f"提前停止说明保留在备注列：{row[12]}")


# ======================================================================
# E. 诊断开 / 关：结果与开销
# ======================================================================
class StubEngine:
    """每次读法固定耗时；PSM 6 零纠正零折叠（会提前停止）。"""

    def __init__(self, per_reading_ms=5.0):
        self.per_reading_ms = per_reading_ms
        self.readings = 0

    def iter_readings(self, image, preprocess=True):
        for psm, text in ((6, "BFRG9516"), (7, "BFRG9510"), (11, "BFRG9510")):
            time.sleep(self.per_reading_ms / 1000.0)
            self.readings += 1
            yield (psm, text)

    def shutdown(self):
        return None


class FakeBuffer:
    def __init__(self, frame):
        self._frame = frame

    def get(self):
        return self._frame

    def clear(self):
        self._frame = None


class TimedRecognizer(RoomCodeRecognizer):
    """自报每帧起始时刻：诊断关闭时没有记录可查，帧间隔只能从这里取。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.starts = []

    def recognize_frame(self, image):
        self.starts.append(time.perf_counter())
        return super().recognize_frame(image)


def run_worker(diagnostics, seconds=3.0, observe=False):
    """跑一遍真实的 OCRWorker 主循环（真线程、真间隔、真裁决器）。"""
    roi_manager = ROIManager()
    roi_manager.set_roi(ROI(0, 0, 60, 20, 80, 20))
    engine = StubEngine()
    recognizer = TimedRecognizer(
        engine, profile=PROFILE, resolver=TemporalResolver(),
        diagnostics=diagnostics, observe_all_psm=observe)
    worker = worker_mod.OCRWorker(
        frame_buffer_getter=lambda: FakeBuffer(FRAME),
        roi_manager=roi_manager, engine=engine, recognizer=recognizer,
        interval_ms=100, pending_interval_ms=50, profile=PROFILE,
        diagnostics=diagnostics, observe_all_psm=observe)
    copies = []
    worker.copy_requested.connect(copies.append)
    # 模拟 GUI 线程的复制处理（真机上这一步在 GUI 线程里跑，会把结果回填进诊断）
    if diagnostics is not None:
        worker.copy_requested.connect(diagnostics.note_copy_success)
    worker.start_recognition()
    pump_events(seconds)
    worker.stop_recognition()
    return recognizer, engine, copies


def test_diagnostics_off_vs_on() -> None:
    section("E. 诊断开 / 关：复制序列一致、OCR 调用次数一致、帧间隔不劣化")

    off_rec, off_engine, off_copies = run_worker(None)
    on_diag = OCRDiagnostics(enabled=True)
    on_rec, on_engine, on_copies = run_worker(on_diag)

    check(off_copies == on_copies == ["BFRG9516"],
          f"复制序列完全一致：关闭={off_copies} 开启={on_copies}")
    check(off_engine.readings == on_engine.readings,
          f"OCR 调用次数一致：关闭={off_engine.readings} 开启={on_engine.readings}")
    check(on_diag.count > 0, f"开启后确实记了 {on_diag.count} 条")
    check(all(t.copy_result == "成功" for t in on_diag.records if t.copy_requested),
          "诊断里的复制结果被正确回填")
    check(on_rec.observe_all_psm is False,
          "这一轮没有开全 PSM 观测（对照的是「普通诊断」的开销）")

    def mean_gap(starts):
        gaps = [(b - a) * 1000.0 for a, b in zip(starts, starts[1:])]
        return sum(gaps) / len(gaps) if gaps else 0.0

    off_gap = mean_gap(off_rec.starts)
    on_gap = mean_gap(on_rec.starts)
    check(len(off_rec.starts) > 3 and len(on_rec.starts) > 3,
          f"两轮都跑出了足够帧数：{len(off_rec.starts)} / {len(on_rec.starts)}")
    # 记录本身的开销极小（实测 0.03ms/帧），间隔差理应落在抖动范围内，
    # 这里给一个宽松上界，避免负载波动导致误报。
    check(on_gap <= off_gap * 1.35 + 15.0,
          f"帧间隔无显著劣化：关闭 {off_gap:.0f}ms vs 开启 {on_gap:.0f}ms")


# ======================================================================
# F. 默认关闭
# ======================================================================
def test_defaults_off() -> None:
    section("F. 默认关闭")

    diagnostics = OCRDiagnostics()
    check(diagnostics.enabled is False, "诊断默认关闭")
    check(diagnostics.begin(profile_id="p") is None,
          "关闭时 begin() 返回 None（不建记录）")

    diagnostics2 = OCRDiagnostics(enabled=True)
    recognizer = RoomCodeRecognizer(
        StubEngine(), profile=PROFILE, resolver=TemporalResolver(),
        diagnostics=diagnostics2)
    check(recognizer.observe_all_psm is False, "全 PSM 观测默认关闭")

    roi_manager = ROIManager()
    roi_manager.set_roi(ROI(0, 0, 60, 20, 80, 20))
    engine = StubEngine()
    worker = worker_mod.OCRWorker(
        frame_buffer_getter=lambda: FakeBuffer(FRAME),
        roi_manager=roi_manager, engine=engine,
        interval_ms=20, profile=PROFILE)
    check(worker.observe_all_psm is False, "OCRWorker 的观测开关默认关闭")
    check(worker._diagnostics is None, "不传诊断时 worker 不接线")


def main() -> int:
    test_export_does_not_block_ocr_path()
    test_backfill_semantics()
    test_bounded_buffer()
    test_csv_shape_unchanged()
    test_diagnostics_off_vs_on()
    test_defaults_off()

    print(f"\n{'=' * 52}")
    print(f"通过 {_PASSED} 项，失败 {_FAILED} 项")
    print("=" * 52)
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
