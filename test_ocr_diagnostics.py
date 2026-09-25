#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
OCR 诊断日志（默认关闭）—— 验证脚本

覆盖：
    A. 各阶段记录准确：原始读法与纠错候选分列，绝不混淆
    B. 重复识别保留完整记录；连续确认与复制事件能关联到对应执行
    C. 无结果、读法歧义、异常三种情况分别记对
    D. 诊断 CSV：顺序、字段、毫秒时间、UTF-8 BOM、重复导出、界面导出流程
    E. 诊断关闭时原有 OCR 行为逐字不变（同一脚本跑开/关两遍比对）

**本测试用桩引擎喂入模拟 OCR 输入，验证的是记录链路是否正确，
不是真实 OCR 的准确率。**

约定与仓库其它 test_*.py 一致：扁平脚本、check()/section() 自行统计，
直接 ``python test_ocr_diagnostics.py`` 运行，不依赖 pytest。
"""

import csv
import os
import re
import sys
import tempfile
import threading
import time

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

os.environ["APPDATA"] = tempfile.mkdtemp(prefix="dlv-diag-appdata-")
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="dlv-diag-local-")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from ocr import worker as worker_mod  # noqa: E402
from ocr.clipboard import ClipboardManager  # noqa: E402
from ocr.diagnostics import CSV_HEADER, OCRDiagnostics  # noqa: E402
from ocr.engine import OCREngineError  # noqa: E402
from ocr.profiles import DELTA_FORCE_1V1  # noqa: E402
from ocr.roi_manager import ROI, ROIManager  # noqa: E402
from ocr.room_code import RoomCodeRecognizer  # noqa: E402
from ocr.temporal import TemporalResolver  # noqa: E402

app = QApplication.instance() or QApplication([])

FRAME = np.zeros((20, 80, 3), dtype=np.uint8)
PROFILE = DELTA_FORCE_1V1.profile_id          # 8 位：LLLLDDDD
TIME_RE = re.compile(r"^\d{2}:\d{2}:\d{2}\.\d{3}$")

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
    print(f"\n=== {title} ===", flush=True)


def pump(rounds: int = 30, delay: float = 0.005) -> None:
    for _ in range(rounds):
        app.processEvents()
        time.sleep(delay)


# ----------------------------------------------------------------------
# 桩：按脚本逐帧给出「多 PSM 读法」的假引擎
# ----------------------------------------------------------------------
class ScriptedEngine:
    """脚本 = 每帧一份 [(psm, 文本), ...]；脚本用完后重复最后一项。"""

    def __init__(self, frames):
        self._frames = list(frames)
        self._index = 0
        self.threads = []
        self.calls = 0

    def iter_readings(self, image, preprocess: bool = True):
        self.calls += 1
        self.threads.append(threading.current_thread())
        readings = self._frames[min(self._index, len(self._frames) - 1)]
        self._index += 1
        for psm, text in readings:
            yield (psm, text)


class ExplodingEngine:
    """每次读法都抛 OCREngineError，模拟「所有 PSM 都失败」。"""

    def iter_readings(self, image, preprocess: bool = True):
        raise OCREngineError("模拟引擎故障")
        yield  # pragma: no cover  —— 让它是个生成器


class FakeBuffer:
    def __init__(self, frame=None):
        self._frame = frame

    def get(self):
        return self._frame


def make_worker(frames, diagnostics=None, interval_ms=20):
    """构造一个接了脚本引擎、ROI 已就绪的真实 OCRWorker（不启动线程）。"""
    roi_manager = ROIManager()
    roi_manager.set_roi(ROI(0, 0, 60, 20, 80, 20))
    engine = ScriptedEngine(frames)
    worker = worker_mod.OCRWorker(
        frame_buffer_getter=lambda: FakeBuffer(FRAME),
        roi_manager=roi_manager,
        engine=engine,
        interval_ms=interval_ms,
        profile=PROFILE,
        diagnostics=diagnostics,
    )
    return worker, engine


def make_exploding_worker(diagnostics=None, interval_ms=20):
    roi_manager = ROIManager()
    roi_manager.set_roi(ROI(0, 0, 60, 20, 80, 20))
    worker = worker_mod.OCRWorker(
        frame_buffer_getter=lambda: FakeBuffer(FRAME),
        roi_manager=roi_manager,
        engine=ExplodingEngine(),
        interval_ms=interval_ms,
        profile=PROFILE,
        diagnostics=diagnostics,
    )
    return worker


def pump_until(predicate, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    app.processEvents()
    return predicate()


def run_worker(frames, diagnostics=None, timeout=6.0, extra_runs=0):
    """跑一小段真实工作线程，返回 (copies, texts, errors, worker)。"""
    worker, _engine = make_worker(frames, diagnostics=diagnostics)
    copies, texts, errors = [], [], []
    worker.copy_requested.connect(copies.append)
    worker.text_updated.connect(texts.append)
    worker.error_occurred.connect(errors.append)

    worker.start_recognition()
    try:
        pump_until(lambda: len(copies) > 0 or len(errors) > 0, timeout)
        deadline = time.time() + extra_runs
        while time.time() < deadline:
            app.processEvents()
            time.sleep(0.005)
    finally:
        worker.stop_recognition()
        pump(10)
    return copies, texts, errors, worker


def read_csv(path: str):
    with open(path, "rb") as handle:
        raw = handle.read()
    rows = list(csv.reader(raw.decode("utf-8-sig").splitlines()))
    return raw, rows


def cells(rows):
    """把 CSV 行转成 {列名: 值} 的列表，断言读起来更清楚。"""
    header = [c for c in rows[0]]
    return [dict(zip(header, row)) for row in rows[1:]]


# ======================================================================
# A. 各阶段记录准确：读法与候选分列
# ======================================================================
def test_stage_accuracy():
    section("A. 各阶段记录：原始读法 / 纠错候选 / 仲裁结论分列，不混淆")

    diag = OCRDiagnostics(enabled=True)
    # 一帧两个读法：PSM6 把数字位的 'O' 读成字母 O（需纠正），PSM7 读对。
    # 这正是本轮 100 组里 HJKT6402/HJKT6462 那类分歧的最小复现。
    frames = [[(6, "HJKT64O2"), (7, "HJKT6462")]]
    copies, texts, errors, _w = run_worker(frames, diagnostics=diag, extra_runs=0.2)

    check(len(diag) >= 1, f"诊断记录了执行：{len(diag)} 条")
    trace = diag.records[0]
    check(trace.index == 1, "序号从 1 开始")
    check(trace.profile_id == PROFILE, f"记下当前游戏档：{trace.profile_id}")
    check("delta_force_1v1_8" in trace.formats and "LLLLDDDD" in trace.formats,
          f"记下游戏配置的格式：{trace.formats}")
    check(trace.elapsed_ms is not None and trace.elapsed_ms >= 0,
          f"记下 OCR 耗时：{trace.elapsed_ms}")

    # 读法原文与候选分列，且一一对应
    check([n.psm for n in trace.readings] == [6, 7],
          f"两个读法按 PSM 顺序都记下了：{[n.psm for n in trace.readings]}")
    check([n.raw_text for n in trace.readings] == ["HJKT64O2", "HJKT6462"],
          f"原始读法原样记录：{[n.raw_text for n in trace.readings]}")
    check([n.candidate_code for n in trace.readings] == ["HJKT6402", "HJKT6462"],
          f"各读法的候选码都记下了：{[n.candidate_code for n in trace.readings]}")
    check("纠正1处" in trace.readings[0].detail and "O->0" in trace.readings[0].detail,
          f"候选旁的纠正说明来自候选自身：{trace.readings[0].detail!r}")
    check(trace.readings[1].detail == "", "零纠正的读法不写纠正说明")

    row = cells(list(csv.reader(diag.to_csv_text().splitlines())))[0]
    check(row["PSM读法"] == "6:HJKT64O2 ‖ 7:HJKT6462",
          f"PSM读法列 = 原始文本：{row['PSM读法']!r}")
    check(row["各读法候选"] == "6:HJKT6402(纠正1处[#6 O->0]) ‖ 7:HJKT6462",
          f"各读法候选列 = 纠错后的候选，标签与读法列一致：{row['各读法候选']!r}")
    check("HJKT6402" not in row["PSM读法"],
          "纠错后的码没有污染原始读法列（'HJKT64O2' 不会被改写成 'HJKT6402' 记进读法）")
    check("HJKT64O2" not in row["各读法候选"],
          "原始读法没有污染候选列（候选列里只有通过严格校验的码）")

    # 仲裁：纠正次数少的 PSM7 胜出；提前停在被采纳的那一读法上
    check(trace.arbitration == "选定:HJKT6462", f"仲裁结论：{trace.arbitration}")
    check(any("提前停止" in n and "PSM 7" in n for n in trace.notes),
          f"记下提前停止的位置：{trace.notes}")

    # 第一帧只是「有一个候选正在被确认」，还不能复制
    check(trace.confirmation == "确认中", f"第一帧的确认结论：{trace.confirmation}")
    check(trace.copy_requested == "" and trace.copy_result == "",
          "未成立时既没有复制请求、也没有复制结果")

    # 第二帧同一读法成立 -> 请求复制 -> 成功复制后回填给**这一条**记录
    second = diag.records[1]
    check(second.confirmation == "成立" and second.copy_requested == "HJKT6462",
          f"第二帧成立并请求复制：{second.confirmation} / {second.copy_requested}")
    check(copies and copies[0] == "HJKT6462", f"确实请求了复制：{copies[:1]}")
    check(second.copy_result == "", "回填之前复制结果为空（不预填成功）")
    diag.note_copy_success("HJKT6462")
    check(second.copy_result == "成功", f"复制成功后回填到对应执行：{second.copy_result}")
    check(diag.records[0].copy_result == "",
          "第一帧（没请求复制）不会被回填")


# ======================================================================
# B. 重复识别与「确认 → 复制」的关联
# ======================================================================
def test_repeat_and_copy_link():
    section("B. 重复识别保留完整记录；复制事件关联到对应执行")

    diag = OCRDiagnostics(enabled=True)
    # A,A → 成立并请求复制；B,B → 成立并请求复制；A,A → 再过几帧后再次成立并再次请求
    frames = [[(7, "MNBV1948")], [(7, "MNBV1948")],
              [(7, "QWER9012")], [(7, "QWER9012")],
              [(7, "MNBV1948")], [(7, "MNBV1948")]]
    copies, texts, errors, _w = run_worker(frames, diagnostics=diag, extra_runs=0.6)

    check(copies == ["MNBV1948", "QWER9012", "MNBV1948"],
          f"同一个房间码在不同时间再次复制会再次请求：{copies}")
    records = diag.records
    check(len(records) >= 6, f"每一次 OCR 执行都留下一条记录（未去重）：{len(records)} 条")
    check([r.index for r in records] == list(range(1, len(records) + 1)),
          "序号连续且不重复（重复识别没有覆盖前一条）")

    requested = [r for r in records if r.copy_requested]
    check([r.copy_requested for r in requested] == ["MNBV1948", "QWER9012", "MNBV1948"],
          f"三次复制请求分别落在三次执行上：{[r.copy_requested for r in requested]}")
    check([r.confirmation for r in requested] == ["成立"] * 3,
          f"请求复制的执行都是「成立」：{[r.confirmation for r in requested]}")

    # 复制结果的回填：按先进先出配到产生请求的那次执行
    diag.note_copy_success("MNBV1948")
    diag.note_copy_success("QWER9012")
    diag.note_copy_success("MNBV1948")
    check([r.copy_result for r in requested] == ["成功", "成功", "成功"],
          f"三次成功复制分别回填到三次执行：{[r.copy_result for r in requested]}")

    # 同码的两次请求必须一次一条，不能都记到同一条上
    check(all(r.copy_resolved for r in requested), "每次复制请求都被独立回填")

    # repeated 的执行（同一房间码连续成立）不会请求复制，也不该被回填
    repeats = [r for r in records if r.confirmation == "重复成立"]
    check(bool(repeats), f"重复成立被如实记录：{len(repeats)} 条")
    check(all(r.copy_requested == "" for r in repeats),
          "重复成立不请求复制（既有去重行为不变），也就没有复制结果")

    # 没有对应执行的复制事件不会凭空造记录
    before = len(diag)
    diag.note_copy_failure("ZZZZ0000", "剪贴板不可用")
    check(len(diag) == before, "找不到对应执行的复制结果不新建记录")


# ======================================================================
# C. 无结果 / 读法歧义 / 异常
# ======================================================================
def test_no_result_and_errors():
    section("C. 无结果、读法歧义与异常")

    # C1. 读法里没有合法候选
    diag = OCRDiagnostics(enabled=True)
    run_worker([[(7, "XX12")]], diagnostics=diag, extra_runs=0.1)
    trace = diag.records[0]
    check(trace.arbitration == "无候选", f"无合法候选：{trace.arbitration}")
    check(trace.readings[0].candidate_code == "", "该读法没有候选")
    row = cells(list(csv.reader(diag.to_csv_text().splitlines())))[0]
    check(row["各读法候选"] == "7:-", f"候选列用占位符表示没有候选：{row['各读法候选']!r}")
    check(row["PSM读法"] == "7:XX12", f"读法原件仍然保留：{row['PSM读法']!r}")

    # C2. 两个读法各自成立、可信度相同却给出不同房间号 -> 读法歧义（不赌）
    #     PSM6 写错一位（各纠正 1 处），PSM7 的段被空格切开需要拼回，代价恰好相同
    diag2 = OCRDiagnostics(enabled=True)
    run_worker([[(6, "HJKT64O2"), (7, "MN BV194O")]], diagnostics=diag2, extra_runs=0.1)
    trace2 = diag2.records[0]
    check(trace2.arbitration == "读法歧义", f"同等可信的冲突候选判为歧义：{trace2.arbitration}")
    check([n.candidate_code for n in trace2.readings] == ["HJKT6402", "MNBV1940"],
          "两个候选都记下来了（分析歧义时要能看见它们）")
    check(trace2.copy_requested == "", "歧义时不产生结果，也就没有复制请求")

    # C3. 引擎异常：记下异常与耗时，且不产生复制
    diag3 = OCRDiagnostics(enabled=True)
    worker = make_exploding_worker(diagnostics=diag3)
    errors = []
    worker.error_occurred.connect(errors.append)
    worker.start_recognition()
    try:
        pump_until(lambda: len(errors) > 0, 4.0)
        # 引擎初始化就失败：线程只报一次错
    finally:
        worker.stop_recognition()
        pump(10)
    check(bool(errors), f"异常照常上报：{errors[:1]}")
    check(bool(diag3.records) and all(
        any("异常" in n for n in r.notes) for r in diag3.records),
        "异常的那次执行如实标注异常（记录里没有编造的候选）")
    check(all(not r.readings for r in diag3.records),
          "读法层面就抛异常时，不伪造任何读法记录")
    check(all(r.copy_requested == "" for r in diag3.records), "异常不产生复制请求")
    class _Boom(RoomCodeRecognizer):
        def recognize_frame(self, image):
            raise OCREngineError("模拟识别崩溃")

    worker3, engine3 = make_worker([[(7, "HJKT6462")]], diagnostics=diag3)
    worker3._recognizer = _Boom(engine3, profile=PROFILE, diagnostics=diag3)
    errs3 = []
    worker3.error_occurred.connect(errs3.append)
    before = len(diag3)
    worker3._perform_recognition()                 # 直接在测试线程里跑一次
    check(len(diag3) == before + 1, "异常的那次执行仍然入库（不丢事件）")
    failed = diag3.records[-1]
    check(any("异常" in n for n in failed.notes), f"记下异常信息：{failed.notes}")
    check(failed.elapsed_ms is not None and failed.elapsed_ms >= 0,
          f"异常时也给出耗时：{failed.elapsed_ms}")
    check(failed.copy_requested == "", "异常的那次没有复制请求")
    check([r.index for r in diag3.records][-1] == len(diag3.records),
          "异常后的序号仍然连续（异常不会跳号或覆盖）")

    # C4. 选错游戏：这一帧被拦下（既有规则），诊断要如实标成「放弃」并留下证据
    diag4 = OCRDiagnostics(enabled=True)
    # 8 位码被空格切成 6+2：本档取不到长度恰好匹配的整段，另一款游戏却取得到
    run_worker([[(7, "BV1948 MN")]], diagnostics=diag4, extra_runs=0.1)
    trace4 = diag4.records[0]
    check(trace4.arbitration == "放弃:游戏冲突",
          f"冲突帧被拦下并如实记录：{trace4.arbitration}")
    check(any("冲突证据" in n for n in trace4.notes),
          f"留下判定证据：{trace4.notes}")
    check(trace4.copy_requested == "", "被拦下的帧不产生复制请求")


# ======================================================================
# D. CSV 导出
# ======================================================================
def test_csv_export():
    section("D. 诊断 CSV：顺序 / 字段 / 毫秒时间 / BOM / 重复导出")

    diag = OCRDiagnostics(enabled=True)
    run_worker([[(7, "HJKT6402")], [(7, "HJKT6402")]], diagnostics=diag, extra_runs=0.1)
    diag.note_copy_success("HJKT6402")

    target = os.path.join(tempfile.mkdtemp(prefix="dlv-diag-csv-"), "诊断.csv")
    count = diag.export_csv(target)
    check(count == len(diag), "导出条数 = 记录条数")
    raw, rows = read_csv(target)
    check(raw.startswith(b"\xef\xbb\xbf"), "UTF-8 BOM（Excel 中文兼容）")
    check(b"\r\n" in raw, "行尾为 CRLF")
    check(tuple(rows[0]) == tuple(CSV_HEADER), f"表头：{rows[0]}")
    check([r[0] for r in rows[1:]] == [str(i) for i in range(1, len(diag) + 1)],
          "序号列按执行顺序连续")
    check(all(TIME_RE.match(r[1]) for r in rows[1:]),
          f"每行时间都是 HH:MM:SS.mmm：{[r[1] for r in rows[1:]][:2]}")

    data = cells(rows)
    check(data[0]["游戏档"] == PROFILE and data[0]["格式"].startswith("delta_force_1v1_8"),
          "每行都带游戏配置")
    check(data[0]["连续确认"] == "确认中" and data[1]["连续确认"] == "成立",
          f"前两行分别是确认中/成立：{[d['连续确认'] for d in data[:2]]}")
    check(data[1]["复制请求"] == "HJKT6402" and data[1]["复制结果"] == "成功",
          f"复制请求与结果都落在第二行：{[data[1]['复制请求'], data[1]['复制结果']]}")
    check(data[1]["确认窗口"].startswith("["), f"确认窗口快照：{data[1]['确认窗口']!r}")

    # 重复导出：内容一致、记录不清空
    again = os.path.join(os.path.dirname(target), "诊断-2.csv")
    diag.export_csv(again)
    check(len(diag) == count, "导出不清空记录")
    check(read_csv(again)[0] == raw, "重复导出得到相同内容")

    # 关闭后不再新增，已有记录保留且仍可导出
    diag.set_enabled(False)
    before = len(diag)
    run_worker([[(7, "QWER9012")]], diagnostics=diag, extra_runs=0.1)
    check(len(diag) == before, "关闭后不再新增诊断记录")
    third = os.path.join(os.path.dirname(target), "诊断-3.csv")
    check(diag.export_csv(third) == before, "关闭后已有记录仍可导出")


# ======================================================================
# E. 关闭时行为不变 + 界面接线
# ======================================================================
def test_disabled_is_transparent():
    section("E. 关闭时原有 OCR 行为不变，且不产生任何记录")

    default = OCRDiagnostics()
    check(default.enabled is False, "诊断默认关闭")
    check(default.begin() is None, "关闭时 begin() 不建记录")
    check(len(default) == 0 and not default, "关闭时没有任何记录")
    check(default.to_csv_text().strip() == ",".join(CSV_HEADER),
          "关闭时导出的只有表头（空记录）")

    script = [[(6, "HJKT64O2"), (7, "HJKT6462")], [(7, "MNBV1948")],
              [(7, "MNBV1948")]]
    off_copies, off_texts, off_errors, _w1 = run_worker(list(script), diagnostics=None,
                                                       extra_runs=0.4)
    diag = OCRDiagnostics(enabled=True)
    on_copies, on_texts, on_errors, _w2 = run_worker(list(script), diagnostics=diag,
                                                    extra_runs=0.4)

    check(off_copies == on_copies and off_texts == on_texts and off_errors == on_errors,
          f"同一脚本开/关诊断结果逐字相同：copies={off_copies}")
    check(off_copies == ["MNBV1948"], f"成立的房间码照常请求复制：{off_copies}")
    check(bool(diag), f"开启时记录了 {len(diag)} 条执行")


class _StubActivation:
    def __init__(self, *a, **k):
        pass

    def exec(self):
        return 0

    def result_data(self):
        return None


class _RecordingFileDialog:
    result = ""
    calls = []

    @classmethod
    def getSaveFileName(cls, parent, title, directory, filt):
        cls.calls.append({"title": title, "directory": directory})
        return cls.result, filt


class _RecordingMessageBox:
    infos = []
    warnings = []

    @classmethod
    def information(cls, parent, title, text):
        cls.infos.append((title, text))
        return 0

    @classmethod
    def warning(cls, parent, title, text):
        cls.warnings.append((title, text))
        return 0


class _FailingClipboard:
    last_error = "clipboard unavailable"

    def copy(self, text):
        return False


def build_window():
    import gui.main_window as mw
    real = mw.ActivationDialog
    mw.ActivationDialog = _StubActivation
    try:
        window = mw.MainWindow()
        window.show()
    finally:
        mw.ActivationDialog = real
    pump(40)
    window._access_status.set_license_active(None)
    pump(20)
    return window


def test_gui_wiring(window):
    section("F. 界面：开关、复制结果回填、诊断 CSV 导出（与房间码 CSV 分开）")

    diag = window._diagnostics
    check(diag.enabled is False, "界面里的诊断默认关闭")
    check(window._diag_btn.text() == "开始诊断", "按钮初始文案反映关闭状态")
    check(window._ocr_panel.isAncestorOf(window._diag_btn)
          and window._ocr_panel.isAncestorOf(window._diag_export_btn),
          "两个诊断控件都在现有 OCR 面板内")
    check("导出" in window._diag_export_btn.text()
          and "诊断" in window._diag_export_btn.text(),
          f"导出按钮文案与房间码导出口区分开：{window._diag_export_btn.text()!r}")

    window._diag_btn.click()
    pump(10)
    check(diag.enabled is True and window._diag_btn.text() == "停止诊断",
          "点击可开启诊断并更新文案")

    # 复制结果回填：成功 / 被去重过滤 / 写入失败 三种都要如实记
    trace = diag.begin(profile_id=PROFILE, formats="delta_force_1v1_8(LLLLDDDD)")
    diag.note_copy_requested(trace, "HJKT6402")
    diag.finish(trace, elapsed_ms=12.5)
    window._on_copy_requested("HJKT6402")                     # 真实剪贴板 -> 成功
    pump(10)
    check(diag.records[-1].copy_result == "成功",
          f"复制成功后回填：{diag.records[-1].copy_result}")
    check(window._copy_log.count == 1,
          "房间码记录 CSV 的流水照常记一条（原有导出功能不受影响）")

    trace2 = diag.begin(profile_id=PROFILE, formats="")
    diag.note_copy_requested(trace2, "HJKT6402")
    diag.finish(trace2, elapsed_ms=9.0)
    window._on_copy_requested("HJKT6402")                     # 连续重复 -> 被过滤
    pump(10)
    check(diag.records[-1].copy_result.startswith("跳过"),
          f"被剪贴板去重过滤时不算成功：{diag.records[-1].copy_result}")
    check(window._copy_log.count == 1, "被过滤的复制不进入房间码流水")

    real_clipboard = window._clipboard_manager
    window._clipboard_manager = _FailingClipboard()
    try:
        trace3 = diag.begin(profile_id=PROFILE, formats="")
        diag.note_copy_requested(trace3, "QWER9012")
        diag.finish(trace3, elapsed_ms=8.0)
        window._on_copy_requested("QWER9012")
        pump(10)
    finally:
        window._clipboard_manager = real_clipboard
    check(diag.records[-1].copy_result.startswith("失败"),
          f"写入失败时如实记录原因：{diag.records[-1].copy_result}")

    # 导出流程（与房间码导出互不影响）
    import gui.main_window as mw
    real_dialog, real_box = mw.QFileDialog, mw.QMessageBox
    mw.QFileDialog, mw.QMessageBox = _RecordingFileDialog, _RecordingMessageBox
    _RecordingFileDialog.calls = []
    _RecordingMessageBox.infos = []
    try:
        target = os.path.join(tempfile.mkdtemp(prefix="dlv-diag-ui-"), "导出诊断.csv")
        _RecordingFileDialog.result = target
        before_copy_log = window._copy_log.count
        window._diag_export_btn.click()
        pump(10)
        check(os.path.isfile(target), "点「导出诊断 CSV」按用户选择的位置写出文件")
        check(_RecordingFileDialog.calls[0]["title"] == "导出 OCR 诊断日志",
              f"导出对话框标题区分于房间码导出：{_RecordingFileDialog.calls[0]['title']!r}")
        check("诊断" in os.path.basename(_RecordingFileDialog.calls[0]["directory"]),
              f"默认文件名带诊断前缀：{_RecordingFileDialog.calls[0]['directory']!r}")
        raw, rows = read_csv(target)
        check(raw.startswith(b"\xef\xbb\xbf") and tuple(rows[0]) == tuple(CSV_HEADER),
              "导出的诊断 CSV 表头与编码正确")
        check(len(rows) - 1 == len(diag), "导出的行数 = 记录条数")
        check(window._copy_log.count == before_copy_log,
              "导出诊断不影响房间码记录")

        # 无记录时只提示
        diag.clear()
        window._diag_export_btn.click()
        pump(10)
        check(len(_RecordingMessageBox.infos) == 1, "没有诊断记录时给出提示")
        check("开始诊断" in _RecordingMessageBox.infos[0][1],
              f"提示告诉用户诊断默认关闭：{_RecordingMessageBox.infos[0][1]!r}")
    finally:
        mw.QFileDialog, mw.QMessageBox = real_dialog, real_box


def main() -> int:
    print("OCR 诊断日志（默认关闭）—— 定向验证")
    print("（桩引擎喂入模拟 OCR 输入；验证的是记录链路，不是真实 OCR 准确率）")

    test_stage_accuracy()
    test_repeat_and_copy_link()
    test_no_result_and_errors()
    test_csv_export()
    test_disabled_is_transparent()

    window = build_window()
    try:
        test_gui_wiring(window)
    finally:
        window.close()
        pump(10)

    print(f"\n结果：{_PASSED} 通过 / {_FAILED} 失败")
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
