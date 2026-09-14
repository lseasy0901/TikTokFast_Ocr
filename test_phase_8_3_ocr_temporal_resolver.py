#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OCR V2.3: TemporalResolver 回归测试

覆盖：
    - 单帧候选不成立；同一房间号连续 min_agreement 次才成立
    - 默认 2/3；None 不计入一致次数；X/Y 交替永不成立
    - 不模糊匹配：不同房间号只做精确比较
    - 已成立的同一房间号不再重复触发剪贴板/输出事件
    - 换成另一个房间号时，必须重新满足同样的规则
    - 窗口有界；min_agreement/window_size 可配置且非法配置被拒绝
    - 裁决器只处理【已通过严格校验】的候选，不做格式校验/纠正
    - 不牺牲低延迟：不等满窗口；OCRWorker 构造仍不构造 OCREngine

隔离说明：
    使用假引擎，不加载 Tesseract；APPDATA 指向临时目录，
    不读取也不改写真实的 license.json。
"""

import os
import sys
import tempfile
import threading
import time

import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

os.environ["APPDATA"] = tempfile.mkdtemp(prefix="dlv-ocr-v23-test-")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

import ocr.worker as worker_mod  # noqa: E402
from ocr.profiles import DELTA_FORCE, VALORANT, get_profile  # noqa: E402
from ocr.roi_manager import ROI, ROIManager  # noqa: E402
from ocr.room_code import (  # noqa: E402
    CandidateResolver,
    RoomCodeCandidate,
    RoomCodeRecognizer,
    SingleFrameResolver,
)
from ocr.temporal import (  # noqa: E402
    DEFAULT_MIN_AGREEMENT,
    DEFAULT_WINDOW_SIZE,
    TemporalResolver,
)

_PASSED = 0
_FAILED = 0
app = None

FRAME = np.zeros((20, 80, 3), dtype=np.uint8)


def check(condition, message: str) -> bool:
    global _PASSED, _FAILED
    if condition:
        _PASSED += 1
        print(f"    [ok]   {message}", flush=True)
    else:
        _FAILED += 1
        print(f"    [FAIL] {message}", flush=True)
    return bool(condition)


def section(title: str) -> None:
    print(f"\n{title}", flush=True)


# ----------------------------------------------------------------------
# 脚手架
# ----------------------------------------------------------------------
class _ScriptedEngine:
    """按脚本逐帧返回文本的假 OCREngine（脚本用完后重复最后一项）。"""

    def __init__(self, texts=()):
        self._texts = list(texts)
        self._index = 0
        self.images = []
        self.threads = []

    @property
    def text(self):
        return self._texts[-1] if self._texts else ""

    @text.setter
    def text(self, value):
        self._texts = [value]
        self._index = 0

    def recognize(self, image, preprocess: bool = True):
        self.images.append(image)
        self.threads.append(threading.current_thread())
        if not self._texts:
            return ""
        if self._index < len(self._texts) - 1:
            value = self._texts[self._index]
            self._index += 1
            return value
        return self._texts[-1]


class _RecordingResolver(CandidateResolver):
    """只记录裁决输入，原样返回（用来证明裁决器收到的一定是合法候选或 None）。"""

    def __init__(self):
        self.offered = []

    def offer(self, candidate):
        self.offered.append(candidate)
        return candidate


class _FakeBuffer:
    def __init__(self, frame=None):
        self._frame = frame

    def get(self):
        return self._frame

    def clear(self):
        self._frame = None


def temporal_recognizer(profile=DELTA_FORCE.profile_id, script=(), **resolver_kwargs):
    """构造 (识别器, 裁决器, 假引擎)。"""
    resolver = TemporalResolver(**resolver_kwargs)
    engine = _ScriptedEngine(script)
    recognizer = RoomCodeRecognizer(engine, profile=profile, resolver=resolver)
    return recognizer, resolver, engine


def observe(recognizer, frames: int):
    """连续观测 frames 帧，返回每帧对外成立的结果（RoomCodeCandidate 或 None）。"""
    return [recognizer.recognize_frame(FRAME) for _ in range(frames)]


def codes(results):
    return [r.code if r is not None else None for r in results]


def repeats(results):
    return [r.repeated if r is not None else None for r in results]


def pump_until(predicate, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return predicate()


def pump_for(seconds: float) -> None:
    """驱动事件循环固定时长（用于「稳定读数继续跑一段」）。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.01)


# ======================================================================
# 1. 基线：单帧不成立 / 连续两帧成立
# ======================================================================
def test_first_observation_alone_does_not_emit():
    section("[1] 单帧候选不成立")
    rec, resolver, _ = temporal_recognizer(script=["ABC1234"])

    result = rec.recognize_frame(FRAME)
    check(result is None, "第一帧即使校验通过也不成立")
    check(resolver.observations == ("ABC1234",), f"观测已记录: {resolver.observations}")
    check(resolver.last_emitted_code is None, "尚无成立过的房间号")

    second = rec.recognize_frame(FRAME)
    check(second is not None and second.code == "ABC1234",
          "同一房间号第二次出现才成立")


def test_two_consecutive_observations_emit():
    section("[2] 同一房间号连续 2 次（默认 2/3）成立")
    rec, resolver, _ = temporal_recognizer(script=["ABC1234"])

    results = observe(rec, 3)
    check(codes(results) == [None, "ABC1234", "ABC1234"],
          f"第二帧成立并持续给出当前房间号 (实际 {codes(results)})")
    check(repeats(results) == [None, False, True],
          f"首次成立 repeated=False，之后为重复 (实际 {repeats(results)})")
    check(resolver.last_emitted_code == "ABC1234", "已记录成立结果")

    section("[2b] 低延迟：不等待满窗口")
    rec2, _, _ = temporal_recognizer(script=["ABC1234"], min_agreement=2, window_size=5)
    results2 = observe(rec2, 2)
    check(codes(results2) == [None, "ABC1234"],
          "window_size=5 时仍在第 2 帧成立（不等满窗口）")


# ======================================================================
# 2. 2/3 与 1/3
# ======================================================================
def test_two_of_three_emits():
    section("[3] 三次观测中同一房间号占两次 -> 成立")
    rec, _, _ = temporal_recognizer(script=["ABC5678", "ABC1234", "ABC1234"])
    check(codes(observe(rec, 3)) == [None, None, "ABC1234"],
          "末尾连续两次一致即成立（前面是别的房间号）")

    rec2, _, _ = temporal_recognizer(script=["ABC1234", "ABC1234", "ABC5678"])
    check(codes(observe(rec2, 3)) == [None, "ABC1234", None],
          "先连续两次成立；随后出现不同房间号，不再成立新结果")

    rec3, _, _ = temporal_recognizer(script=["ABC1234", None, "ABC1234"])
    check(codes(observe(rec3, 3)) == [None, None, "ABC1234"],
          "中间夹一帧无结果时，两次一致仍然成立（None 不打断）")


def test_one_of_three_does_not_emit():
    section("[4] 1/3 -> 不成立")
    rec, resolver, _ = temporal_recognizer(script=["ABC1234", "ABC5678", "ABC9012"])
    check(codes(observe(rec, 3)) == [None, None, None], "三个互不相同的房间号都不成立")
    check(resolver.last_emitted_code is None, "没有结果成立")

    rec2, _, _ = temporal_recognizer(script=["ABC1234", "hello world", "ABC5678"])
    check(codes(observe(rec2, 3)) == [None, None, None],
          "合法结果被无结果帧隔开后不成立")


# ======================================================================
# 3. None 不计数 / 交替不成立
# ======================================================================
def test_none_does_not_count():
    section("[5] None 不计入一致次数")
    rec, resolver, _ = temporal_recognizer(script=[None, None, None])
    check(codes(observe(rec, 3)) == [None, None, None], "全是无结果：始终不成立")
    check(resolver.observations == (None, None, None),
          f"None 也被记录进窗口（便于观察）: {resolver.observations}")

    rec2, _, _ = temporal_recognizer(script=[None, "ABC1234"])
    check(codes(observe(rec2, 2)) == [None, None],
          "一次有效观测 + 一次 None 不足以成立")

    rec3, _, _ = temporal_recognizer(script=["ABC1234", None])
    check(codes(observe(rec3, 3)) == [None, None, None],
          "有效观测之后只有无结果帧 -> 不成立")


def test_alternating_valid_codes_never_emit():
    section("[6] 交替出现的合法房间号不成立")
    rec, resolver, _ = temporal_recognizer(
        script=["ABC1234", "ABC5678"] * 4
    )
    check(codes(observe(rec, 8)) == [None] * 8, "交替 8 帧始终无结果")
    check(resolver.last_emitted_code is None, "没有任何结果成立")

    rec2, _, _ = temporal_recognizer(script=["ABC1234", "ABC5678", "ABC1234"])
    check(codes(observe(rec2, 3)) == [None, None, None],
          "非连续的两票（X,Y,X）不算一致：宁可不出结果")


# ======================================================================
# 4. 去重：同一号码不重复触发输出
# ======================================================================
def test_repeat_does_not_emit_again():
    section("[7] 已成立的同一房间号不再重复触发输出")
    rec, resolver, _ = temporal_recognizer(script=["ABC1234"])

    results = observe(rec, 6)
    check(codes(results) == [None] + ["ABC1234"] * 5,
          "结果持续可用（显示不闪断）")
    check(repeats(results)[1] is False and all(repeats(results)[2:]),
          f"只有首次成立 repeated=False (实际 {repeats(results)})")
    check(resolver.last_emitted_code == "ABC1234", "成立记录未变")


def test_different_code_later_can_establish():
    section("[8] 之后出现的不同房间号可以重新成立")
    rec, resolver, _ = temporal_recognizer(
        script=["ABC1234", "ABC1234", "ABC5678", "ABC5678"]
    )

    results = observe(rec, 4)
    check(codes(results) == [None, "ABC1234", None, "ABC5678"],
          f"先成立 X，再成立 Y (实际 {codes(results)})")
    check(repeats(results)[1] is False and repeats(results)[3] is False,
          "新房间号成立时 repeated=False（允许再次复制）")
    check(resolver.last_emitted_code == "ABC5678", "成立记录已更新为 Y")

    rec2, _, _ = temporal_recognizer(
        script=["ABC1234", "ABC1234", "ABC5678", "ABC5678", "ABC1234", "ABC1234"]
    )
    check(codes(observe(rec2, 6)) == [None, "ABC1234", None, "ABC5678", None, "ABC1234"],
          "换回原房间号同样需要重新满足规则")


# ======================================================================
# 5. 窗口有界 / 配置
# ======================================================================
def test_window_is_bounded():
    section("[9] 窗口有界")
    rec, resolver, _ = temporal_recognizer(script=["ABC1234"], window_size=3)
    observe(rec, 20)
    check(len(resolver.observations) == 3,
          f"观测窗口上限为 window_size (实际 {len(resolver.observations)})")
    check(resolver.window_size == 3 and resolver.min_agreement == 2, "配置被保留")

    rec2, resolver2, _ = temporal_recognizer(
        script=["ABC1234"], min_agreement=1, window_size=1
    )
    observe(rec2, 5)
    check(len(resolver2.observations) == 1, "window_size=1 时只保留 1 条观测")


def test_configuration():
    section("[10] min_agreement / window_size 可配置")
    check((DEFAULT_MIN_AGREEMENT, DEFAULT_WINDOW_SIZE) == (2, 3),
          "默认配置为 2/3")
    default_resolver = TemporalResolver()
    check((default_resolver.min_agreement, default_resolver.window_size) == (2, 3),
          "TemporalResolver() 默认 2/3")

    rec, _, _ = temporal_recognizer(script=["ABC1234"], min_agreement=3, window_size=3)
    results = observe(rec, 3)
    check(codes(results) == [None, None, "ABC1234"],
          f"min_agreement=3 时需要连续 3 次 (实际 {codes(results)})")

    rec2, _, _ = temporal_recognizer(script=["ABC1234"], min_agreement=1, window_size=1)
    check(codes(observe(rec2, 1)) == ["ABC1234"], "min_agreement=1 等价于单帧直通")

    for bad in ((4, 3), (0, 3), (2, 0), (2, -1)):
        try:
            TemporalResolver(min_agreement=bad[0], window_size=bad[1])
            check(False, f"非法配置 {bad} 应当抛错")
        except ValueError:
            check(True, f"非法配置 {bad} 抛 ValueError")


# ======================================================================
# 6. 语义边界：只裁决、不再校验
# ======================================================================
def test_only_validated_candidates_reach_resolver():
    section("[11] 裁决器只收到【已通过严格校验】的候选或 None")
    recorder = _RecordingResolver()
    engine = _ScriptedEngine(
        ["hello world", "ABC12D", "AB1234", "ABC123", "ABC1234"]
    )
    rec = RoomCodeRecognizer(
        engine, profile=DELTA_FORCE.profile_id, resolver=recorder
    )
    observe(rec, 5)

    check(len(recorder.offered) == 5, "每一帧都经过裁决器")
    check(all(o is None or DELTA_FORCE.primary_format.accepts(o.code)
              for o in recorder.offered),
          "裁决器看到的非空结果一定通过严格校验")
    check([o is None for o in recorder.offered] == [True, True, True, True, False],
          f"非法/长度不符的输入一律以 None 进入裁决 (实际 "
          f"{[None if o is None else o.code for o in recorder.offered]})")

    section("[11b] 时序层不改变候选本身（不做校验/纠正/模糊匹配）")
    # 用刻意带 OCR 噪声的输入（尾部 'O' 需要逐位纠正为 '0'），
    # 证明「纠正后的候选」在两种裁决下完全一致 —— 时序层不碰校验与纠正。
    single = RoomCodeRecognizer(
        _ScriptedEngine(["Room Code: ABC12O"]),
        profile=VALORANT.profile_id,
        resolver=SingleFrameResolver(),
    )
    one_shot = single.recognize_frame(FRAME)

    temporal, _, _ = temporal_recognizer(
        profile=VALORANT.profile_id,
        script=["Room Code: ABC12O", "Room Code: ABC12O"],
    )
    confirmed = observe(temporal, 2)[1]

    check(one_shot is not None and confirmed is not None, "两种裁决都给出结果")
    check((one_shot.code, one_shot.format_name, one_shot.source_text,
           one_shot.corrections) ==
          (confirmed.code, confirmed.format_name, confirmed.source_text,
           confirmed.corrections),
          "房间号/格式/来源/纠正完全相同：时序层只做一致性判断")
    check(one_shot.repeated is False and confirmed.repeated is False,
          "单帧直通与首次成立都不是重复结果")

    check(all(o is None or isinstance(o, RoomCodeCandidate) for o in recorder.offered),
          "裁决输入类型稳定（RoomCodeCandidate 或 None）")


# ======================================================================
# 7. OCRWorker 接线
# ======================================================================
def test_worker_default_uses_temporal_resolver():
    section("[12] OCRWorker 默认使用时序裁决器（2/3），且不构造引擎")
    roi_manager = ROIManager()
    roi_manager.set_roi(ROI(0, 0, 100, 50, 100, 50))

    worker = worker_mod.OCRWorker(
        frame_buffer_getter=lambda: None,
        roi_manager=roi_manager,
        interval_ms=300,
        profile=DELTA_FORCE.profile_id,
    )
    check(isinstance(worker._resolver, TemporalResolver),
          f"默认裁决器是 TemporalResolver (实际 {type(worker._resolver).__name__})")
    check((worker._resolver.min_agreement, worker._resolver.window_size) == (2, 3),
          "默认 2/3")
    check(worker._engine is None and worker._recognizer is None,
          "构造 worker 仍然不构造引擎/识别器（启动修复保持）")

    section("[12b] 注入的识别器/裁决器被原样使用")
    resolver = TemporalResolver(min_agreement=2, window_size=2)
    worker2 = worker_mod.OCRWorker(
        frame_buffer_getter=lambda: None,
        roi_manager=roi_manager,
        interval_ms=300,
        profile=DELTA_FORCE.profile_id,
        resolver=resolver,
    )
    check(worker2._resolver is resolver, "自定义裁决器被保存")

    worker3 = worker_mod.OCRWorker(
        frame_buffer_getter=lambda: None,
        roi_manager=roi_manager,
        interval_ms=300,
        profile=DELTA_FORCE.profile_id,
        resolver=SingleFrameResolver(),
    )
    check(isinstance(worker3._resolver, SingleFrameResolver),
          "可显式关闭时序裁决（单帧直通）")

    section("[12c] RoomCodeRecognizer 的默认行为不变（V2.1 API）")
    rec = RoomCodeRecognizer(_ScriptedEngine(["ABC1234"]),
                             profile=DELTA_FORCE.profile_id)
    check(isinstance(rec.resolver, SingleFrameResolver),
          "直接构造识别器默认仍是单帧直通")
    check(rec.recognize_text("ABC1234").code == "ABC1234",
          "单帧直通一次即出结果（V2.1 行为保留）")


def test_worker_emits_confirmed_code_once():
    section("[13] 工作线程集成：确认一次，剪贴板只触发一次")
    roi_manager = ROIManager()
    roi_manager.set_roi(ROI(0, 0, 100, 50, 100, 50))
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    engine = _ScriptedEngine(["Room Code: ABC12O"])

    worker = worker_mod.OCRWorker(
        frame_buffer_getter=lambda: _FakeBuffer(frame),
        roi_manager=roi_manager,
        engine=engine,
        interval_ms=50,
        profile=VALORANT.profile_id,
    )

    texts = []
    copies = []
    worker.text_updated.connect(lambda t: texts.append(t))
    worker.copy_requested.connect(lambda t: copies.append(t))

    worker.start_recognition()
    ok = pump_until(lambda: "ABC120" in texts, timeout=10.0)
    check(ok, f"确认后的房间号被传出 (收到 {texts})")

    # 稳定读数继续跑一段（约 20 帧）：不应重复触发输出
    pump_for(1.0)
    check(texts.count("ABC120") == 1,
          f"同一房间号只产生一次 text_updated (实际 {texts.count('ABC120')})")
    check(copies == ["ABC120"], f"剪贴板只被请求一次 (收到 {copies})")
    check(worker._recognizer.resolver is worker._resolver,
          "工作线程内构造的识别器使用了 worker 的裁决器")

    # 换成另一个合法房间号：需要重新成立，然后可以再次复制
    engine.text = "Room Code: XYZ789"
    check(pump_until(lambda: "XYZ789" in copies, timeout=10.0),
          f"新的合法房间号在重新成立后被复制 (收到 {copies})")
    check(copies == ["ABC120", "XYZ789"], f"复制事件序列正确 (实际 {copies})")

    check(worker.error_count == 0, "整个过程无错误")
    check(all(t is not threading.main_thread() for t in engine.threads),
          "裁决与识别都发生在工作线程")

    worker.stop_recognition()
    worker.wait(2000)
    check(worker.is_recognizing() is False, "工作线程已停止")

    section("[13b] 三角洲档（7 位）同样走时序确认")
    delta_engine = _ScriptedEngine(["ABC1234"])
    delta_worker = worker_mod.OCRWorker(
        frame_buffer_getter=lambda: _FakeBuffer(frame),
        roi_manager=roi_manager,
        engine=delta_engine,
        interval_ms=50,
        profile=DELTA_FORCE.profile_id,
    )
    delta_copies = []
    delta_worker.copy_requested.connect(lambda t: delta_copies.append(t))
    delta_worker.start_recognition()
    ok = pump_until(lambda: delta_copies == ["ABC1234"], timeout=10.0)
    check(ok, f"7 位房间号连续两帧后被复制一次 (收到 {delta_copies})")
    delta_worker.stop_recognition()
    delta_worker.wait(2000)


def test_worker_profile_switch_resets_temporal_state():
    section("[14] 换游戏档：时序状态清空，裁决器保持同一个实例")
    roi_manager = ROIManager()
    roi_manager.set_roi(ROI(0, 0, 100, 50, 100, 50))
    engine = _ScriptedEngine(["ABC1234"])
    worker = worker_mod.OCRWorker(
        frame_buffer_getter=lambda: None,
        roi_manager=roi_manager,
        engine=engine,
        interval_ms=300,
        profile=DELTA_FORCE.profile_id,
    )

    resolver = worker._resolver
    resolver.offer(_valid_candidate("ABC1234"))
    check(resolver.observations == ("ABC1234",), "换档前已有观测")

    worker.set_profile(VALORANT.profile_id)
    check(worker._resolver is resolver, "裁决器实例未被替换（时序状态不丢失来源）")
    check(resolver.observations == () and resolver.last_emitted_code is None,
          "换档后时序状态已清空（旧格式的观测不再有意义）")
    check(worker._engine is engine, "引擎未被重建")
    check(worker._recognizer is None or
          worker._recognizer.profile.profile_id == VALORANT.profile_id,
          "识别器（若已存在）已切到新档")


def _valid_candidate(code: str):
    """借识别器造一个真实通过校验的候选，避免手工拼装。"""
    rec = RoomCodeRecognizer(_ScriptedEngine([code]),
                             profile=DELTA_FORCE.profile_id)
    candidate = rec.recognize_frame(FRAME)
    assert candidate is not None, code
    return candidate


# ======================================================================
# main
# ======================================================================
def main() -> int:
    global app
    app = QApplication.instance() or QApplication([])

    print("=" * 70, flush=True)
    print("OCR V2.3: TemporalResolver suite", flush=True)
    print(f"isolated APPDATA: {os.environ['APPDATA']}", flush=True)
    print(f"profiles: {DELTA_FORCE.profile_id}, {VALORANT.profile_id}, "
          f"{get_profile(None).profile_id}", flush=True)
    print("=" * 70, flush=True)

    test_first_observation_alone_does_not_emit()
    test_two_consecutive_observations_emit()
    test_two_of_three_emits()
    test_one_of_three_does_not_emit()
    test_none_does_not_count()
    test_alternating_valid_codes_never_emit()
    test_repeat_does_not_emit_again()
    test_different_code_later_can_establish()
    test_window_is_bounded()
    test_configuration()
    test_only_validated_candidates_reach_resolver()
    test_worker_default_uses_temporal_resolver()
    test_worker_emits_confirmed_code_once()
    test_worker_profile_switch_resets_temporal_state()

    print("\n" + "=" * 70, flush=True)
    total = _PASSED + _FAILED
    if _FAILED == 0:
        print(f"RESULT: {_PASSED}/{total} checks passed, 0 failed", flush=True)
    else:
        print(f"RESULT: {_PASSED}/{total} checks passed, {_FAILED} FAILED", flush=True)
    print("=" * 70, flush=True)
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
