#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
诊断模式性能对照（Phase OCR-6）

问题：开启 OCR 诊断后，识别明显卡顿。

本脚本在**相同帧源、相同引擎耗时、相同采样条件**下对照三种模式：

    A. 诊断关闭                       (diagnostics=None)
    B. 普通诊断开启、全 PSM 观测关闭
    C. 普通诊断开启、全 PSM 观测开启

每种模式驱动**真实的 OCRWorker 主循环**（真线程、真采样间隔、真裁决器），
只把 OCR 引擎换成受控实现。记录：

    每帧 OCR 耗时      取诊断记录的 elapsed_ms（worker 自己量的）
    诊断处理耗时       给 OCRDiagnostics 挂计时包装，逐方法累计
    CSV 写入耗时       to_csv_text() / export_csv() 的墙钟时间
    实际帧间隔         由记录里的 started_at（worker 逐帧打的）算出
    积压量             记录总数 + 仍未回填复制结果的条数
    复制结果序列       worker.copy_requested 信号

两种引擎：

    桩引擎（默认）     每次读法固定 sleep U，用来隔离「诊断本身的开销」
    真实 Tesseract     --real：真 OCREngine + 真实卡片图，用来回答真机问题

**桩引擎的数字只能说明诊断代码自身的开销，不能当作真机性能。**
真机结论只在 --real 下成立。

用法：
    venv/Scripts/python.exe bench_phase_ocr6_diagnostics_overhead.py
    venv/Scripts/python.exe bench_phase_ocr6_diagnostics_overhead.py --real
    venv/Scripts/python.exe bench_phase_ocr6_diagnostics_overhead.py --seconds 8
"""

import argparse
import os
import statistics
import sys
import tempfile
import threading
import time

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

os.environ["APPDATA"] = tempfile.mkdtemp(prefix="dlv-bench6-appdata-")
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="dlv-bench6-local-")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np                                              # noqa: E402
from PySide6.QtWidgets import QApplication                      # noqa: E402

from ocr import worker as worker_mod                            # noqa: E402
from ocr.diagnostics import OCRDiagnostics                      # noqa: E402
from ocr.profiles import DELTA_FORCE_1V1                        # noqa: E402
from ocr.roi_manager import ROI, ROIManager                     # noqa: E402
from ocr.room_code import RoomCodeRecognizer                    # noqa: E402
from ocr.temporal import TemporalResolver                       # noqa: E402

app = QApplication.instance() or QApplication([])
PROFILE = DELTA_FORCE_1V1.profile_id

#: 与真实 ROI 一致的取景：864x1080 帧里取 640x120 的一条
FRAME_W, FRAME_H = 864, 1080
ROI_X, ROI_Y, ROI_W, ROI_H = 100, 400, 640, 120

#: 桩引擎每次「读法」的模拟耗时（ms）。第二轮真机诊断 CSV 实测：
#: 单 PSM 帧 17~27ms、三 PSM 帧 44~67ms -> 每个 PSM 约 18~22ms。
STUB_PER_READING_MS = 20.0


class TimedDiagnostics(OCRDiagnostics):
    """给热路径方法挂计时，用来把「诊断自身占了多少」从总耗时里分出来。

    计时结果只 append 到列表（GIL 下安全），不参与任何判定。
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.spans = {}

    def _span(self, name, func, *args, **kwargs):
        start = time.perf_counter_ns()
        try:
            return func(*args, **kwargs)
        finally:
            self.spans.setdefault(name, []).append(time.perf_counter_ns() - start)

    def begin(self, *a, **k):
        return self._span("begin", super().begin, *a, **k)

    def note_reading(self, *a, **k):
        return self._span("note_reading", super().note_reading, *a, **k)

    def note_confirmation(self, *a, **k):
        return self._span("note_confirmation", super().note_confirmation, *a, **k)

    def note_copy_requested(self, *a, **k):
        return self._span("note_copy_requested", super().note_copy_requested, *a, **k)

    def note_copy_success(self, *a, **k):
        return self._span("backfill", super().note_copy_success, *a, **k)

    def note_copy_skipped(self, *a, **k):
        return self._span("backfill", super().note_copy_skipped, *a, **k)

    def finish(self, *a, **k):
        return self._span("finish", super().finish, *a, **k)

    def total_ms(self) -> float:
        return sum(sum(v) for v in self.spans.values()) / 1e6

    def per_call_us(self) -> dict:
        return {k: (sum(v) / len(v) / 1000.0) for k, v in self.spans.items() if v}


class StubEngine:
    """每次读法固定 sleep，模拟一个 PSM 的识别成本；PSM 6 零纠正零折叠。"""

    def __init__(self, per_reading_ms=STUB_PER_READING_MS):
        self.per_reading_ms = per_reading_ms
        self.readings = 0
        self.calls = 0

    def iter_readings(self, image, preprocess=True):
        self.calls += 1
        for psm, text in ((6, "BFRG9516"), (7, "BFRG9510"), (11, "BFRG9510")):
            time.sleep(self.per_reading_ms / 1000.0)
            self.readings += 1
            yield (psm, text)

    def shutdown(self):
        return None


class RealEngine:
    """真 OCREngine 的薄包装：只为统计读法次数与转发 shutdown。"""

    def __init__(self):
        from ocr.engine import OCREngine
        self._engine = OCREngine()
        self.readings = 0
        self.calls = 0

    def iter_readings(self, image, preprocess=True):
        self.calls += 1
        for psm, text in self._engine.iter_readings(image, preprocess=preprocess):
            self.readings += 1
            yield (psm, text)

    def shutdown(self):
        shutdown = getattr(self._engine, "shutdown", None)
        if shutdown is not None:
            shutdown()


class TimedRecognizer(RoomCodeRecognizer):
    """量每帧的 recognize_frame 耗时与起始时刻。

    诊断关闭时没有记录可查，所以「帧间隔」与「OCR 耗时」必须由这里独立提供，
    三种模式才可比。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.starts = []
        self.durations_ms = []

    def recognize_frame(self, image):
        start = time.perf_counter()
        self.starts.append(start)
        try:
            return super().recognize_frame(image)
        finally:
            self.durations_ms.append((time.perf_counter() - start) * 1000.0)


class FakeBuffer:
    def __init__(self, frame):
        self._frame = frame

    def get(self):
        return self._frame

    def clear(self):
        self._frame = None


def build_frame(real: bool) -> np.ndarray:
    frame = np.full((FRAME_H, FRAME_W, 3), 40, dtype=np.uint8)
    if real:
        from bench_phase_ocr1_latency import make_card
        card = make_card("BFRG9516", False)
        frame[ROI_Y:ROI_Y + card.shape[0], ROI_X:ROI_X + card.shape[1]] = card
    return frame


def run_mode(mode: str, seconds: float, real: bool, preseed: int = 0):
    """跑一种模式，返回测量结果。

    preseed: 先塞进这么多条历史记录，模拟「诊断开了一两个小时之后」的状态。
             用来量那些随记录数增长的开销（回填是 O(n) 的）。
    """
    diag_enabled = mode != "A"
    observe = mode == "C"

    diagnostics = TimedDiagnostics(enabled=diag_enabled) if diag_enabled else None
    if diagnostics is not None and preseed:
        _preseed(diagnostics, preseed)
    engine = RealEngine() if real else StubEngine()

    roi_manager = ROIManager()
    roi_manager.set_roi(ROI(ROI_X, ROI_Y, ROI_W, ROI_H, FRAME_W, FRAME_H))
    frame = build_frame(real)

    recognizer = TimedRecognizer(
        engine,
        profile=PROFILE,
        resolver=TemporalResolver(),
        diagnostics=diagnostics,
        observe_all_psm=observe,
    )
    worker = worker_mod.OCRWorker(
        frame_buffer_getter=lambda: FakeBuffer(frame),
        roi_manager=roi_manager,
        engine=engine,
        recognizer=recognizer,
        interval_ms=300,
        pending_interval_ms=100,
        profile=PROFILE,
        diagnostics=diagnostics,
        observe_all_psm=observe,
    )

    copies = []
    worker.copy_requested.connect(copies.append)
    # 模拟 GUI 线程的复制处理：真机上这一步在 GUI 线程里跑，会把结果回填进诊断。
    # 注意连接是排队投递的 —— 主线程必须真的泵事件，槽才会跑（这本身就是要量的）。
    if diagnostics is not None:
        worker.copy_requested.connect(diagnostics.note_copy_success)

    worker.start_recognition()
    pump_ms = _pump_gui(seconds)
    worker.stop_recognition()

    starts = recognizer.starts
    intervals = [
        (b - a) * 1000.0 for a, b in zip(starts, starts[1:])
    ]
    ocr_ms = list(recognizer.durations_ms)

    result = {
        "mode": mode,
        "engine_readings": engine.readings,
        "engine_calls": engine.calls,
        "copies": list(copies),
        "records": diagnostics.count if diagnostics else 0,
        "frames": len(starts),
        "interval_mean": statistics.mean(intervals) if intervals else 0.0,
        "interval_median": statistics.median(intervals) if intervals else 0.0,
        "interval_p95": (sorted(intervals)[int(len(intervals) * 0.95)]
                         if intervals else 0.0),
        "interval_max": max(intervals) if intervals else 0.0,
        "ocr_mean": statistics.mean(ocr_ms) if ocr_ms else 0.0,
        "ocr_max": max(ocr_ms) if ocr_ms else 0.0,
        "gui_pump_mean_ms": statistics.mean(pump_ms) if pump_ms else 0.0,
        "gui_pump_max_ms": max(pump_ms) if pump_ms else 0.0,
        "diag_total_ms": diagnostics.total_ms() if diagnostics else 0.0,
        "diag_per_frame_ms": (
            diagnostics.total_ms() / len(starts) if diagnostics and starts else 0.0),
        "diag_per_call_us": diagnostics.per_call_us() if diagnostics else {},
        "unresolved": (sum(1 for r in diagnostics.records
                           if r.copy_requested and not r.copy_resolved)
                       if diagnostics else 0),
        # CSV 写入：导出在 GUI 线程上做，这里量的是那条路径的墙钟时间
        "csv_render_ms": _time_csv(diagnostics, "render") if diagnostics else 0.0,
        "csv_write_ms": _time_csv(diagnostics, "write") if diagnostics else 0.0,
    }
    return result, worker, diagnostics


def _preseed(diagnostics, count: int) -> None:
    """预置历史记录（模拟长会话）。

    刻意把其中一部分做成「请求了复制但从未回填」——那正是 _resolve_copy
    每次都要从表头扫过去的形态。
    """
    from ocr.room_code import RoomCodeCandidate
    cand = RoomCodeCandidate(code="AAAA1111", profile_id=PROFILE,
                             format_name="f", raw_text="AAAA1111",
                             source_text="AAAA1111")
    for i in range(count):
        trace = diagnostics.begin(profile_id=PROFILE, formats="f(LLLLDDDD (8 位))")
        diagnostics.note_reading(6, "AAAA1111", cand, "")
        diagnostics.note_confirmation(trace, "重复成立", ["AAAA1111"] * 3)
        diagnostics.note_copy_requested(trace, f"CODE{i:06d}")
        diagnostics.finish(trace, elapsed_ms=18.0)
    diagnostics.spans.clear()


def _pump_gui(seconds: float):
    """模拟 GUI 事件循环：泵事件并记录每次 processEvents 的耗时。

    真机上 GUI 线程一直在跑这个循环，排队投递的诊断信号（复制回填、
    text_updated）都在这里被执行 —— 它的单次耗时就是主线程被占用的时间。
    """
    durations = []
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        start = time.perf_counter()
        app.processEvents()
        durations.append((time.perf_counter() - start) * 1000.0)
        time.sleep(0.001)
    return durations


def _time_csv(diagnostics, kind: str) -> float:
    """CSV 渲染 / 落盘耗时（ms）。空记录时返回 0。"""
    if not diagnostics or not diagnostics.count:
        return 0.0
    if kind == "render":
        start = time.perf_counter()
        diagnostics.to_csv_text()
        return (time.perf_counter() - start) * 1000.0
    path = os.path.join(tempfile.gettempdir(), "dlv-bench6-diag.csv")
    start = time.perf_counter()
    diagnostics.export_csv(path)
    elapsed = (time.perf_counter() - start) * 1000.0
    try:
        os.remove(path)
    except OSError:
        pass
    return elapsed


LABELS = {
    "A": "A. 诊断关闭            ",
    "B": "B. 诊断开 / 观测关      ",
    "C": "C. 诊断开 / 观测开      ",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--real", action="store_true",
                        help="用真实 Tesseract 引擎（真机结论只在这里成立）")
    parser.add_argument("--seconds", type=float, default=6.0)
    parser.add_argument("--preseed", type=int, default=0,
                        help="预置 N 条历史记录，模拟长会话")
    parser.add_argument("--modes", default="ABC", help="要跑的档，例如 B 或 AB")
    args = parser.parse_args()

    print("=" * 78)
    print("引擎：%s" % ("真实 Tesseract" if args.real else
                      "桩引擎（每次读法 %.0fms）—— 只反映诊断自身开销，"
                      "不代表真机" % STUB_PER_READING_MS))
    print("每档运行 %.0f 秒；稳态间隔 300ms，确认窗口间隔 100ms" % args.seconds)
    print("=" * 78)

    if args.preseed:
        print("预置历史记录：%d 条" % args.preseed)

    results = {}
    for mode in args.modes:
        result, worker, _ = run_mode(mode, args.seconds, args.real, args.preseed)
        results[mode] = result
        print("  %s 跑完：%d 帧，引擎读法 %d 次，记录共 %d 条"
              % (LABELS[mode], result["frames"], result["engine_readings"],
                 result["records"]))

    header = ("模式", "帧数", "间隔均", "间隔中位", "间隔p95", "间隔最大", "OCR均",
              "OCR最大", "诊断/帧", "读法数", "记录", "未回填", "GUI泵均", "GUI泵最大")
    print("\n" + " | ".join(f"{h:>8}" for h in header))
    print("-" * 140)
    for mode in results:
        r = results[mode]
        row = (LABELS[mode].strip(), r["frames"],
               f"{r['interval_mean']:.0f}", f"{r['interval_median']:.0f}",
               f"{r['interval_p95']:.0f}", f"{r['interval_max']:.0f}",
               f"{r['ocr_mean']:.1f}",
               f"{r['ocr_max']:.1f}", f"{r['diag_per_frame_ms']:.3f}",
               r["engine_readings"], r["records"], r["unresolved"],
               f"{r['gui_pump_mean_ms']:.2f}", f"{r['gui_pump_max_ms']:.2f}")
        print(" | ".join(f"{str(c):>8}" for c in row))

    print("\nCSV 导出（GUI 线程上会跑的那一段）：")
    for mode in results:
        r = results[mode]
        print("  %s 渲染 %8.1f ms | 落盘 %8.1f ms"
              % (LABELS[mode], r["csv_render_ms"], r["csv_write_ms"]))

    print("\n诊断各方法的单次耗时（微秒）：")
    for mode in results:
        per = results[mode]["diag_per_call_us"]
        if per:
            print("  %s %s" % (LABELS[mode],
                               "  ".join(f"{k}={v:.2f}us" for k, v in sorted(per.items()))))

    print("\n复制序列：")
    for mode in results:
        c = results[mode]["copies"]
        print("  %s 共 %d 次，前 5 个 %s"
              % (LABELS[mode], len(c), c[:5]))

    if not args.real:
        print("\n[注意] 以上是桩引擎结果：只能说明诊断代码自身的开销。")
        print("       真机结论请用 --real 重跑。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
