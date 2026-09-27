#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
全 PSM 观测开关（阶段二）—— 验证脚本

背景：默认路径上，只要某个读法算出「零纠正零折叠」的候选就提前停止，
后面的 PSM 根本不跑。第二轮 100 码回归里 384 帧有 372 帧属于这种情况，
于是「PSM 7/11 会不会读成别的」这个问题在诊断 CSV 里完全无从回答——
那两档压根没被调用过。

本脚本验证的开关（RoomCodeRecognizer(observe_all_psm=True)）：
    开启后，同一个提前停止点不再 break，继续跑完其余 PSM；但补跑出来的
    读法【只写诊断记录】（标签带 "观测" 前缀），
        - 不进 _best_of 仲裁（否则 PSM 之间分歧会变成「读法歧义」→ 结果变 None）
        - 不进「选错游戏」判定的 seen（否则会凭空多出一次冲突提醒）
        - 不进时序裁决
    因此 recognize_frame 的返回值与关闭时逐字相同，TemporalResolver、
    复制请求与剪贴板结果都不变。开关默认关闭，关闭时性能路径与日志语义不变。

覆盖：
    A. 默认关闭：只记录到提前停止那个 PSM，备注措辞不变
    B. 开启：记录全部 PSM，补跑读法带「观测」标签，备注说明「原本会提前停止」
    C. 开/关的判定结果逐项一致（含 PSM 之间存在分歧的场景）
    D. 观测读法不参与「选错游戏」判定
    E. 没有提前停止点时两种模式行为相同
    F. 异常处理：主路径照旧上抛；观测期间的失败降级为备注、不改变本帧结论
    G. 耗时：elapsed_ms 照常写入；额外 OCR 开销的实测估计
    H. 开关本身：默认 False、只读

约定与仓库其它 test_*.py 一致：扁平脚本、check()/section() 自行统计，
直接 ``python test_ocr_observe_all_psm.py`` 运行，不依赖 pytest。
本脚本用桩引擎喂入模拟读法，验证的是【记录与判定是否一致】，
不是真实 OCR 的准确率。不联网、不起 OCR 线程、不需要 tesseract。
"""

import os
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np                                              # noqa: E402
from PySide6.QtWidgets import QApplication                      # noqa: E402

from ocr import worker as worker_mod                            # noqa: E402
from ocr.diagnostics import OCRDiagnostics                      # noqa: E402
from ocr.engine import OCREngineError                           # noqa: E402
from ocr.profiles import DELTA_FORCE_1V1                        # noqa: E402
from ocr.roi_manager import ROI, ROIManager                     # noqa: E402
from ocr.room_code import (                                     # noqa: E402
    _PHASE_OBSERVED,
    RoomCodeRecognizer,
    SingleFrameResolver,
)
from ocr.temporal import TemporalResolver                       # noqa: E402

app = QApplication.instance() or QApplication([])

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


PROFILE = DELTA_FORCE_1V1.profile_id
FRAME = np.zeros((20, 80, 3), dtype=np.uint8)

#: 帧脚本：PSM 6 读出「零纠正」的码，PSM 7/11 却读出另一个码。
#: 真实回归里 PSM 6 与 PSM 11 分歧过（'PQAZ174e' vs 'PQAZ1740'），
#: 这里把分歧造成「零纠正 vs 零纠正」的最坏情形，用来证明观测不进仲裁。
DISAGREEING = [((6, "BFRG9516"), (7, "BFRG9510"), (11, "BFRG9510"))]
#: 三档都读不出——没有任何提前停止点。
ALL_FAIL = [((6, "PQAZ174e"), (7, "PQAZ174e"), (11, "PQAZ174e"))]
#: PSM 6 给出滑窗候选（段过长，_has_exact_code 为假），PSM 11 带一个 7 位码。
#: 若观测读法被错放进 seen，就会凭空判定出「选错游戏」。
CONFLICT_BAIT = [((6, "ABCD1234xy"), (11, "ABC1234"))]


# ======================================================================
# 桩：按脚本逐帧给出多 PSM 读法
# ======================================================================
class ScriptedEngine:
    """脚本 = 每帧一份 [(psm, 文本), ...]；脚本用完后重复最后一项。

    error_at: 第 N 个读法（0-based）【之前】抛 OCREngineError，用来模拟
              「引擎读到一半失败」。None = 不失败。
    """

    def __init__(self, frames, error_at=None):
        self._frames = list(frames)
        self._index = 0
        self.error_at = error_at
        self.calls = 0
        self.readings_served = 0

    def iter_readings(self, image, preprocess: bool = True):
        self.calls += 1
        readings = self._frames[min(self._index, len(self._frames) - 1)]
        self._index += 1
        for i, (psm, text) in enumerate(readings):
            if self.error_at is not None and i >= self.error_at:
                raise OCREngineError("模拟引擎故障")
            self.readings_served += 1
            yield (psm, text)


class _FakeEngine:
    def recognize(self, image, preprocess: bool = True):
        return ""


def build(observe, frames, resolver=None, error_at=None):
    """构造 (识别器, 诊断记录器, 引擎)。默认时序裁决器，便于验证复制链路。"""
    diagnostic = OCRDiagnostics(enabled=True)
    engine = ScriptedEngine(frames, error_at=error_at)
    rec = RoomCodeRecognizer(
        engine,
        profile=PROFILE,
        resolver=TemporalResolver() if resolver is None else resolver,
        diagnostics=diagnostic,
        observe_all_psm=observe,
    )
    return rec, diagnostic, engine


def run_frames(rec, diagnostic, count, elapsed_ms=1.0):
    """按帧驱动 recognize_frame，并像 worker 那样用 begin/finish 包住每次执行。"""
    out = []
    for _ in range(count):
        trace = diagnostic.begin()
        candidate = rec.recognize_frame(FRAME)
        diagnostic.finish(trace, elapsed_ms=elapsed_ms)
        out.append(candidate)
    return out


def codes(candidates):
    return [c.code if c is not None else None for c in candidates]


class FakeBuffer:
    def __init__(self, frame=None):
        self._frame = frame

    def get(self):
        return self._frame


def build_worker(observe, frames):
    """构造一个接了桩引擎的【真实 OCRWorker】（不启动线程，逐帧同步驱动）。

    复制请求因此走的是 worker 自己的判定与信号，而不是在本测试里把规则重推
    一遍——开关到底有没有影响复制，由真实链路回答。
    """
    roi_manager = ROIManager()
    roi_manager.set_roi(ROI(0, 0, 60, 20, 80, 20))
    engine = ScriptedEngine(frames)
    diagnostic = OCRDiagnostics(enabled=True)
    recognizer = RoomCodeRecognizer(
        engine, profile=PROFILE, resolver=TemporalResolver(),
        diagnostics=diagnostic, observe_all_psm=observe,
    )
    worker = worker_mod.OCRWorker(
        frame_buffer_getter=lambda: FakeBuffer(FRAME),
        roi_manager=roi_manager,
        engine=engine,
        interval_ms=20,
        profile=PROFILE,
        recognizer=recognizer,
        resolver=TemporalResolver(),
        diagnostics=diagnostic,
    )
    copies = []
    worker.copy_requested.connect(copies.append)
    return worker, copies, diagnostic, engine


# ======================================================================
# A. 默认关闭
# ======================================================================
def test_default_off() -> None:
    section("A. 默认关闭：只记录到提前停止的 PSM")

    rec = RoomCodeRecognizer(_FakeEngine(), profile=PROFILE)
    check(rec.observe_all_psm is False, "observe_all_psm 默认 False")

    rec2, diagnostic, engine = build(False, DISAGREEING)
    run_frames(rec2, diagnostic, 1)
    trace = diagnostic.records[0]

    check([n.label for n in trace.readings] == ["6"],
          f"只记录到提前停止的那一档：{[n.label for n in trace.readings]}")
    check(_PHASE_OBSERVED not in "".join(n.label for n in trace.readings),
          "关闭时不会出现「观测」标签")
    check(trace.notes == ["提前停止于 PSM 6（该读法零纠正零折叠）"],
          f"备注措辞与从前完全一致：{trace.notes}")
    check(engine.readings_served == 1,
          f"引擎只被取了 1 个读法（后面的 PSM 没有跑）：{engine.readings_served}")


# ======================================================================
# B. 开启：记录全部 PSM
# ======================================================================
def test_enabled_records_all_psm() -> None:
    section("B. 开启：跑完全部 PSM，补跑的读法标「观测」")

    rec, diagnostic, engine = build(True, DISAGREEING)
    run_frames(rec, diagnostic, 1)
    trace = diagnostic.records[0]

    check([n.label for n in trace.readings] == ["6", "观测7", "观测11"],
          f"三档读法全部入账：{[n.label for n in trace.readings]}")
    check(engine.readings_served == 3,
          f"引擎确实被取了 3 个读法：{engine.readings_served}")

    # 原始读法列与候选列必须逐个对得上（同一套标签）
    readings = " ‖ ".join(n.render_reading() for n in trace.readings)
    cands = " ‖ ".join(n.render_candidate() for n in trace.readings)
    check(readings == "6:BFRG9516 ‖ 观测7:BFRG9510 ‖ 观测11:BFRG9510",
          f"原始读法列：{readings}")
    check(cands == "6:BFRG9516 ‖ 观测7:BFRG9510 ‖ 观测11:BFRG9510",
          f"候选列：{cands}")

    check(trace.notes == [
        "原本会在 PSM 6 提前停止（观测模式已继续跑完其余 PSM；"
        "标「观测」的读法仅供观测，不参与判定）"
    ], f"备注说明原本会在哪一档提前停止：{trace.notes}")
    check(trace.arbitration == "选定:BFRG9516",
          f"仲裁结论仍是 PSM 6 的读数：{trace.arbitration}")


# ======================================================================
# C. 开/关的判定结果逐项一致
# ======================================================================
def test_results_identical() -> None:
    section("C. 开关开/关：判定与复制结果逐项一致")

    # C1. 识别层。最坏情形：PSM 6 与 PSM 7/11 给出【两个零纠正的不同码】。
    #     若观测读法进了 _best_of，就会变成「读法歧义」-> None；
    #     正确实现下结果必须还是 PSM 6 给出的 BFRG9516。
    off_rec, off_diag, _ = build(False, DISAGREEING)
    on_rec, on_diag, _ = build(True, DISAGREEING)

    off_seq = codes(run_frames(off_rec, off_diag, 3))
    on_seq = codes(run_frames(on_rec, on_diag, 3))

    check(off_seq == [None, "BFRG9516", "BFRG9516"],
          f"关闭时：连续确认后成立（第 2 帧起）→ {off_seq}")
    check(on_seq == off_seq,
          f"开启时结果逐项相同（分歧读法没有把结果变成 None）→ {on_seq}")

    check(off_rec.resolver.observations == on_rec.resolver.observations,
          f"时序观测窗口一致：{on_rec.resolver.observations}")
    check(off_rec.resolver.last_emitted_code == on_rec.resolver.last_emitted_code,
          f"最近成立的房间号一致：{on_rec.resolver.last_emitted_code}")

    # C2. worker 层：复制请求走真实信号，不在这里重推规则。
    off_worker, off_copies, off_wdiag, _ = build_worker(False, DISAGREEING)
    on_worker, on_copies, on_wdiag, _ = build_worker(True, DISAGREEING)
    for _ in range(3):
        off_worker._perform_recognition()
        on_worker._perform_recognition()

    check(off_copies == on_copies == ["BFRG9516"],
          f"worker 的复制请求信号完全一致：关闭={off_copies} 开启={on_copies}")

    off_req = [t.copy_requested for t in off_wdiag.records if t.copy_requested]
    on_req = [t.copy_requested for t in on_wdiag.records if t.copy_requested]
    check(off_req == on_req == ["BFRG9516"],
          f"诊断里记录的复制请求一致：关闭={off_req} 开启={on_req}")

    off_arb = [t.arbitration for t in off_wdiag.records]
    on_arb = [t.arbitration for t in on_wdiag.records]
    check(off_arb == on_arb and all(a.startswith("选定:") for a in on_arb),
          f"每帧仲裁结论一致：{on_arb}")


# ======================================================================
# D. 观测读法不参与「选错游戏」判定
# ======================================================================
def test_observations_do_not_leak_into_conflict() -> None:
    section("D. 观测读法不参与「选错游戏」判定")

    off_rec, _, _ = build(False, CONFLICT_BAIT, resolver=SingleFrameResolver())
    on_rec, _, _ = build(True, CONFLICT_BAIT, resolver=SingleFrameResolver())

    off = off_rec.recognize_frame(FRAME)
    off_conflict = off_rec.last_conflict
    on = on_rec.recognize_frame(FRAME)
    on_conflict = on_rec.last_conflict

    check(off is not None and off.code == "ABCD1234",
          f"关闭时给出滑窗候选：{off.code if off else None}")
    check(on is not None and on.code == off.code,
          f"开启时同一个候选：{on.code if on else None}")
    check(off_conflict is None and on_conflict is None,
          "PSM 11 的 7 位码【没有】被算进「选错游戏」判定（两种模式都没冲突）")
    check(on_conflict is off_conflict,
          "判定证据一致：观测读法没有污染 seen")


# ======================================================================
# E. 没有提前停止点时两种模式行为相同
# ======================================================================
def test_no_early_stop() -> None:
    section("E. 没有提前停止点时，两种模式行为相同")

    off_rec, off_diag, off_engine = build(False, ALL_FAIL, resolver=SingleFrameResolver())
    on_rec, on_diag, on_engine = build(True, ALL_FAIL, resolver=SingleFrameResolver())

    off = run_frames(off_rec, off_diag, 1)[0]
    on = run_frames(on_rec, on_diag, 1)[0]
    check(off is None and on is None, "三档都读不出 -> 无候选")

    off_trace, on_trace = off_diag.records[-1], on_diag.records[-1]
    check([n.label for n in on_trace.readings] == ["6", "7", "11"],
          f"开启时也不加「观测」标签（本来就没有提前停止）："
          f"{[n.label for n in on_trace.readings]}")
    check([n.label for n in off_trace.readings] == ["6", "7", "11"],
          f"关闭时本来就跑满三档：{[n.label for n in off_trace.readings]}")
    check(off_trace.arbitration == on_trace.arbitration == "无候选",
          f"仲裁结论一致：{off_trace.arbitration}")
    check(off_trace.notes == on_trace.notes == [],
          f"没有提前停止就不加备注：{on_trace.notes}")
    check(off_engine.readings_served == on_engine.readings_served == 3,
          "两种模式取到的读法数相同")


# ======================================================================
# F. 异常处理
# ======================================================================
def test_exception_handling() -> None:
    section("F. 异常处理")

    # F1. 主路径（第一个读法）就失败：两种模式都必须照旧上抛
    for observe in (False, True):
        rec, diagnostic, _ = build(observe, DISAGREEING, error_at=0)
        raised = False
        try:
            rec.recognize_frame(FRAME)
        except OCREngineError:
            raised = True
        check(raised,
              f"observe_all_psm={observe}: 主路径引擎失败照旧抛 OCREngineError")

    # F2. 提前停止【之后】的观测读取失败：不能把已经得到的结论变成失败
    off_rec, off_diag, _ = build(False, DISAGREEING, error_at=1,
                                 resolver=SingleFrameResolver())
    on_rec, on_diag, _ = build(True, DISAGREEING, error_at=1,
                               resolver=SingleFrameResolver())

    off_trace = off_diag.begin()
    off = off_rec.recognize_frame(FRAME)
    off_diag.finish(off_trace, elapsed_ms=1.0)

    on_raised = False
    on = None
    on_trace = on_diag.begin()
    try:
        on = on_rec.recognize_frame(FRAME)
    except OCREngineError:
        on_raised = True
    on_diag.finish(on_trace, elapsed_ms=1.0)

    check(not on_raised, "观测期间引擎失败不向上抛（开关不能让本帧变成失败）")
    check(on is not None and off is not None and on.code == off.code,
          f"观测失败时本帧结论不变：关闭={off.code if off else None} "
          f"开启={on.code if on else None}")

    on_trace = on_diag.records[-1]
    check(any("观测读取失败" in note for note in on_trace.notes),
          f"失败被如实记进诊断备注，没有静默吞掉：{on_trace.notes}")
    check(on_trace.arbitration == "选定:BFRG9516",
          f"仲裁结论仍是提前停止点的读数：{on_trace.arbitration}")


# ======================================================================
# G. 耗时
# ======================================================================
def test_elapsed_and_overhead() -> None:
    section("G. 耗时字段与额外 OCR 开销")

    off_rec, off_diag, off_engine = build(False, DISAGREEING)
    on_rec, on_diag, on_engine = build(True, DISAGREEING)
    run_frames(off_rec, off_diag, 2, elapsed_ms=12.5)
    run_frames(on_rec, on_diag, 2, elapsed_ms=12.5)

    check(all(t.elapsed_ms == 12.5 for t in on_diag.records),
          f"elapsed_ms 照常写入（观测不干扰该字段）："
          f"{[t.elapsed_ms for t in on_diag.records]}")

    # 额外 OCR 调用次数是确定性的：开启后每个「提前停止」的帧多跑 2 次读法
    check(off_engine.readings_served == 2, f"关闭：2 帧共 2 次读法")
    check(on_engine.readings_served == 6, f"开启：2 帧共 6 次读法（每帧多 2 次）")

    # 墙钟估计：用一个「每次读法固定耗时」的桩，量开关带来的额外时间。
    per_reading_s = 0.002
    frames = 20

    class SleepingEngine(ScriptedEngine):
        def iter_readings(self, image, preprocess=True):
            for psm, text in super().iter_readings(image, preprocess):
                time.sleep(per_reading_s)
                yield (psm, text)

    def measure(observe):
        diagnostic = OCRDiagnostics(enabled=True)
        engine = SleepingEngine(DISAGREEING)
        rec = RoomCodeRecognizer(engine, profile=PROFILE,
                                 resolver=SingleFrameResolver(),
                                 diagnostics=diagnostic,
                                 observe_all_psm=observe)
        start = time.perf_counter()
        run_frames(rec, diagnostic, frames)
        return time.perf_counter() - start

    off_s = measure(False)
    on_s = measure(True)
    print(f"    [信息] {frames} 帧、每次读法 {per_reading_s * 1000:.0f}ms："
          f"关闭 {off_s * 1000:.0f}ms，开启 {on_s * 1000:.0f}ms，"
          f"额外 {(on_s - off_s) / frames * 1000:.1f}ms/帧")
    check(on_s > off_s,
          f"开启确有额外耗时（桩引擎下 {off_s * 1000:.0f}ms -> "
          f"{on_s * 1000:.0f}ms）")
    # 额外耗时应当约等于「每帧多跑 2 次读法」，而不是别的开销
    expected_extra = 2 * per_reading_s * frames
    check(abs((on_s - off_s) - expected_extra) < expected_extra,
          f"额外耗时与「每帧多 2 次读法」同量级：实测 "
          f"{(on_s - off_s) * 1000:.0f}ms vs 预期 {expected_extra * 1000:.0f}ms")


# ======================================================================
# H. 开关本身
# ======================================================================
def test_switch_surface() -> None:
    section("H. 开关的对外形状")

    rec = RoomCodeRecognizer(_FakeEngine(), profile=PROFILE)
    check(rec.observe_all_psm is False, "默认 False")
    try:
        rec.observe_all_psm = True
        writable = True
    except AttributeError:
        writable = False
    check(not writable, "observe_all_psm 是只读属性，不会被运行期改写")

    for observe in (False, True):
        rec2 = RoomCodeRecognizer(_FakeEngine(), profile=PROFILE,
                                  observe_all_psm=observe)
        check(rec2._resolve_text("ABCD1234") is not None,
              f"observe_all_psm={observe}: _resolve_text 纯文本路径不受影响")


def main() -> int:
    test_default_off()
    test_enabled_records_all_psm()
    test_results_identical()
    test_observations_do_not_leak_into_conflict()
    test_no_early_stop()
    test_exception_handling()
    test_elapsed_and_overhead()
    test_switch_surface()

    print(f"\n{'=' * 52}")
    print(f"通过 {_PASSED} 项，失败 {_FAILED} 项")
    print("=" * 52)
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
