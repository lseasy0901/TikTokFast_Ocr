#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 8.5: 游戏选择 -> OCR 全链路 回归测试

正式接入要求用户【明确选择】游戏，且两款游戏的规则严格隔离。
本套件把这条链路整条跑通（除 GUI 控件本身，那部分由 8.1 覆盖）：

    界面选择项 (OCR_GAME_CHOICES)
        -> 对应 profile (DELTA_FORCE / VALORANT)
        -> 真实 OCRWorker（自带真实 RoomCodeRecognizer）
        -> 对应格式校验/逐位纠正
        -> 真实 TemporalResolver（2/3 时序一致性）
        -> copy_requested（GUI 侧据此写剪贴板）

覆盖：
    [1] 界面只提供两款具体游戏，各自落到不同的真实档位（无 auto）
    [2] 选“三角洲行动”：7 位码经时序确认后成立并触发一次复制
    [3] 选“无畏契约”：6 位码走自己的格式/纠正规则后成立
    [4] 规则隔离：另一款游戏的房间号在本次选择下【永不成立】
    [5] worker 层拒绝 None / "auto"：不会用识别内容去猜游戏
    [6] 切档清空时序观测，旧档的观测不会参与新档的判定
    [7] 识别不出东西时只更新显示，不触发复制

隔离说明：
    使用假引擎（脚本化读法），不加载 Tesseract、不读取任何用户截图；
    APPDATA 指向临时目录，不读取也不改写真实的 license.json；
    投递级断言只到 copy_requested 信号——本测试不碰系统剪贴板。
"""

import os
import sys
import tempfile
import threading
import time

import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

os.environ["APPDATA"] = tempfile.mkdtemp(prefix="dlv-ocr-chain-test-")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

import gui.main_window as mw  # noqa: E402
import ocr.worker as worker_mod  # noqa: E402
from ocr.profiles import AUTO, DELTA_FORCE, PROFILES, VALORANT, get_profile  # noqa: E402
from ocr.roi_manager import ROI, ROIManager  # noqa: E402
from ocr.room_code import RoomCodeRecognizer, SingleFrameResolver  # noqa: E402
from ocr.temporal import TemporalResolver  # noqa: E402

_PASSED = 0
_FAILED = 0
app = None

#: 一帧 ROI 图像（内容无关紧要：引擎是桩）
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
# 桩
# ----------------------------------------------------------------------
class _ScriptedEngine:
    """按脚本逐帧给出读法的假 OCREngine（脚本用完后重复最后一项）。

    走 iter_readings()，即生产引擎真实的读法入口。
    """

    def __init__(self, texts=()):
        self._texts = list(texts)
        self._index = 0
        self.calls = 0
        self.threads = []

    def _next_text(self):
        self.calls += 1
        self.threads.append(threading.current_thread())
        if not self._texts:
            return ""
        if self._index < len(self._texts) - 1:
            value = self._texts[self._index]
            self._index += 1
            return value
        return self._texts[-1]

    def iter_readings(self, image, preprocess: bool = True):
        text = self._next_text()
        if text:
            yield (6, text)


class _FakeBuffer:
    def __init__(self, frame=None):
        self._frame = frame

    def get(self):
        return self._frame

    def clear(self):
        self._frame = None


def make_worker(profile, script, interval_ms=20, **kwargs):
    """构造一个接了脚本引擎、ROI 已就绪的真实 OCRWorker。"""
    roi_manager = ROIManager()
    roi_manager.set_roi(ROI(0, 0, 60, 20, 80, 20))
    engine = _ScriptedEngine(script)
    worker = worker_mod.OCRWorker(
        frame_buffer_getter=lambda: _FakeBuffer(FRAME),
        roi_manager=roi_manager,
        engine=engine,
        interval_ms=interval_ms,
        profile=profile,
        **kwargs,
    )
    return worker, engine


def pump_until(predicate, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    app.processEvents()
    return predicate()


def run_worker(script, profile, timeout=8.0, **kwargs):
    """跑一小段真实工作线程，返回 (copies, texts, worker)。

    copies: copy_requested 收到的房间号序列
    texts:  text_updated 收到的文本序列（含空串）
    """
    worker, _engine = make_worker(profile, script, **kwargs)

    copies = []
    texts = []
    errors = []
    worker.copy_requested.connect(copies.append)
    worker.text_updated.connect(texts.append)
    worker.error_occurred.connect(errors.append)

    worker.start_recognition()
    try:
        # 等到有结果，或者确认「确实一直没有结果」
        pump_until(lambda: len(copies) > 0, timeout)
        # 再多跑一小段，让可能出现的重复触发也暴露出来
        deadline = time.time() + 0.3
        while time.time() < deadline:
            app.processEvents()
            time.sleep(0.005)
    finally:
        worker.stop_recognition()  # 会 join 线程，之后状态是静止的

    check(not errors, f"工作线程没有报错: {errors}")
    return copies, texts, worker


# ======================================================================
# 1. 界面选择项
# ======================================================================
def test_ui_choices_are_concrete_games():
    section("[1] 界面只提供两款具体游戏，各自落到不同的真实档位")

    choices = mw.OCR_GAME_CHOICES
    ids = [pid for pid, _label in choices]
    check(len(choices) == 2, f"只有两个可选项: {ids}")
    check(set(ids) == {DELTA_FORCE.profile_id, VALORANT.profile_id},
          f"两项就是两款具体游戏: {ids}")
    check(AUTO.profile_id not in ids, "auto 不出现在界面上")
    check(all(get_profile(pid).profile_id == pid for pid in ids),
          "每一项都能解析到它自己那个档位（不会被换成别的档）")

    delta = get_profile(DELTA_FORCE.profile_id)
    valorant = get_profile(VALORANT.profile_id)
    check(delta.primary_format.length == 7,
          f"三角洲档只认 7 位: {delta.primary_format.describe()}")
    check(valorant.primary_format.length == 6,
          f"无畏契约档只认 6 位: {valorant.primary_format.describe()}")
    check(len(delta.formats) == 1 and len(valorant.formats) == 1,
          "两款游戏各只有一种格式（选择即确定格式，无需猜测）")

    check(mw.DEFAULT_OCR_GAME_PROFILE_ID == DELTA_FORCE.profile_id,
          f"默认选项是具体游戏而不是 auto: {mw.DEFAULT_OCR_GAME_PROFILE_ID}")
    check(mw.DEFAULT_OCR_GAME_PROFILE_ID in ids, "默认选项在可选项之内")


# ======================================================================
# 2. 三角洲行动：全链路
# ======================================================================
def test_delta_force_chain():
    section("[2] 选“三角洲行动”：7 位码经时序确认后成立并复制一次")

    copies, texts, worker = run_worker(["ABC1234"], DELTA_FORCE.profile_id)

    check(isinstance(worker._resolver, TemporalResolver),
          f"worker 默认用时序裁决器: {type(worker._resolver).__name__}")
    check((worker._resolver.min_agreement, worker._resolver.window_size) == (2, 3),
          "默认 2/3")
    check(worker._recognizer is not None
          and worker._recognizer.profile.profile_id == DELTA_FORCE.profile_id,
          "worker 自己构造的识别器用的是所选游戏档")
    check(copies == ["ABC1234"],
          f"房间号成立并恰好复制一次: {copies}")
    check(texts and texts[-1] == "ABC1234", f"显示同步更新: {texts}")
    check(worker._recognizer.engine is worker._engine,
          "识别器包裹的是 worker 自己那个引擎")
    check(worker._engine is not None, "引擎在工作线程内被创建（启动路径不变）")
    check(worker._engine.threads
          and all(t is not threading.main_thread() for t in worker._engine.threads),
          "识别发生在工作线程，不在 GUI 线程")

    section("[2b] 用时序观测证明「确实等了第二帧」")
    # 同一档位、同一帧图像，走同一个时空约束：单帧不成立，第二帧才成立。
    recognizer = RoomCodeRecognizer(_ScriptedEngine(["ABC1234"]),
                                    profile=DELTA_FORCE.profile_id,
                                    resolver=TemporalResolver())
    check(recognizer.recognize_frame(FRAME) is None, "第一帧不成立")
    second = recognizer.recognize_frame(FRAME)
    check(second is not None and second.code == "ABC1234", "第二帧成立")


# ======================================================================
# 3. 无畏契约：全链路（含纠正规则）
# ======================================================================
def test_valorant_chain():
    section("[3] 选“无畏契约”：6 位码按本档格式与纠正规则成立")

    # 'ABC12O' 的末位 'O' 在数字位要纠正成 '0'——这是无畏契约档才能接受的长度
    copies, texts, worker = run_worker(["Room Code: ABC12O"], VALORANT.profile_id)

    check(worker._recognizer.profile.profile_id == VALORANT.profile_id,
          "识别器用的是无畏契约档")
    check(copies == ["ABC120"], f"纠正规则生效且复制一次: {copies}")
    check(texts and texts[-1] == "ABC120", f"显示同步更新: {texts}")


# ======================================================================
# 4. 隔离：另一款游戏的房间号永不成立
# ======================================================================
def test_cross_game_isolation():
    section("[4] 规则隔离：长度对不上的房间号在本档下不成立")

    # 6 位码在三角洲档（7 位）下：比格式短，取不出候选
    copies, texts, _ = run_worker(["ABC123"], DELTA_FORCE.profile_id,
                                  timeout=2.0)
    check(copies == [], f"无畏契约式的 6 位码在三角洲档下不成立: {copies}")
    check(texts == [""], f"只更新了显示，没有复制: {texts}")

    section("[4b] 同一段文本在两档下结论不同（证明用的是各自档位的规则）")
    text = "ABC12O"
    delta_rec = RoomCodeRecognizer(_ScriptedEngine([text]),
                                  profile=DELTA_FORCE.profile_id,
                                  resolver=SingleFrameResolver())
    valorant_rec = RoomCodeRecognizer(_ScriptedEngine([text]),
                                      profile=VALORANT.profile_id,
                                      resolver=SingleFrameResolver())
    check(delta_rec.recognize_frame(FRAME) is None,
          "三角洲档拒绝 'ABC12O'（长度不符）")
    accepted = valorant_rec.recognize_frame(FRAME)
    check(accepted is not None and accepted.code == "ABC120",
          f"无畏契约档接受并纠正为 'ABC120': "
          f"{accepted.code if accepted else None}")
    check(accepted is not None and accepted.format_name == VALORANT.primary_format.name,
          f"命中的是所选档自己的格式: "
          f"{accepted.format_name if accepted else None}")

    # 7 位文本：三角洲档接受，无畏契约档【不会】给出结果
    text7 = "ABC1234"
    seven = RoomCodeRecognizer(_ScriptedEngine([text7]),
                               profile=DELTA_FORCE.profile_id,
                               resolver=SingleFrameResolver()).recognize_frame(FRAME)
    check(seven is not None and seven.code == "ABC1234",
          "三角洲档接受 'ABC1234'")

    section("[4c] 选错游戏时不会被滑窗蒙混过关（Phase 8.6 起）")
    # _match_format 会对【过长的连续段】滑窗取本档长度的子串（_QUALITY_WINDOW），
    # 这是为了让 ROI 多框进几个字符时仍能取到房间号，属既有能力。
    # 代价是选错游戏时 7 位码会被截出一个 6 位子串；Phase 8.6 的「选错游戏」
    # 判定会先把这种帧拦下（见 test_phase_8_6_ocr_game_mismatch_notice.py）。
    cross_rec = RoomCodeRecognizer(_ScriptedEngine([text7]),
                                  profile=VALORANT.profile_id,
                                  resolver=SingleFrameResolver())
    check(cross_rec.recognize_frame(FRAME) is None,
          "选错游戏时 7 位码不会被截出一个 6 位结果")
    check(cross_rec.last_conflict is not None, "并且被判定为游戏选择冲突")

    # 滑窗候选仍然永远不会盖过长度精确匹配的候选（质量 EXACT < WINDOW）
    mixed = "XYZ789 ABC1234"
    best = RoomCodeRecognizer(_ScriptedEngine([mixed]),
                              profile=VALORANT.profile_id,
                              resolver=SingleFrameResolver()).recognize_frame(FRAME)
    check(best is not None and best.code == "XYZ789",
          f"同一帧里长度精确的候选优先于滑窗候选: "
          f"{best.code if best else None}")


def test_oversized_run_is_safe():
    section("[4d] 过长的段滑窗取不出合法候选时安全返回 None")
    # 单段过长且无解时不会崩：'ABCDEFG' 的 6 位窗口含字母在数字位，全部无解
    none_result = RoomCodeRecognizer(_ScriptedEngine(["ABCDEFGx"]),
                                    profile=VALORANT.profile_id,
                                    resolver=SingleFrameResolver()).recognize_frame(FRAME)
    check(none_result is None,
          f"滑窗取不出合法候选时返回 None: "
          f"{none_result.code if none_result else None}")


# ======================================================================
# 5. 不允许猜测：worker 层拒绝 None / auto
# ======================================================================
def test_worker_refuses_guess():
    section("[5] worker 层拒绝 None / \"auto\"：不会用识别内容猜游戏")

    roi_manager = ROIManager()
    roi_manager.set_roi(ROI(0, 0, 60, 20, 80, 20))

    def build(profile):
        return worker_mod.OCRWorker(
            frame_buffer_getter=lambda: None,
            roi_manager=roi_manager,
            interval_ms=300,
            profile=profile,
        )

    for bad in (None, AUTO.profile_id, ""):
        try:
            build(bad)
            check(False, f"构造 worker 时 {bad!r} 应当抛 ValueError")
        except ValueError as exc:
            check("游戏" in str(exc),
                  f"{bad!r} 被拒绝且提示里指明要选游戏: {str(exc)[:40]}...")
        except Exception as exc:  # noqa: BLE001
            check(False, f"{bad!r} 抛的是 {type(exc).__name__} 而不是 ValueError")

    check(build(DELTA_FORCE.profile_id)._profile == DELTA_FORCE.profile_id,
          "明确指定具体游戏时正常构造")
    check(build(VALORANT.profile_id)._profile == VALORANT.profile_id,
          "另一款具体游戏也正常构造")

    section("[5b] 切档同样拒绝猜测，且失败时不动任何状态")
    worker, scripted = make_worker(DELTA_FORCE.profile_id, ["ABC1234"])
    worker._recognizer = RoomCodeRecognizer(scripted,
                                            profile=DELTA_FORCE.profile_id,
                                            resolver=worker._resolver)
    # 先留下一条观测，用来证明「切档失败」没有顺手清空它
    worker._recognizer.recognize_frame(FRAME)
    before = worker._resolver.observations
    check(len(before) == 1, f"已有一条观测: {before}")

    for bad in (None, AUTO.profile_id):
        try:
            worker.set_profile(bad)
            check(False, f"set_profile({bad!r}) 应当抛 ValueError")
        except ValueError:
            check(True, f"set_profile({bad!r}) 被拒绝")
    check(worker._profile == DELTA_FORCE.profile_id,
          f"档位没有被改坏: {worker._profile!r}")
    check(worker._resolver.observations == before,
          "失败时没有清空时序观测（先校验再改状态）")
    check(worker._recognizer.profile.profile_id == DELTA_FORCE.profile_id,
          "失败时没有换掉识别器")

    worker.set_profile(VALORANT.profile_id)
    check(worker._profile == VALORANT.profile_id, "具体游戏之间可以正常切换")

    section("[5c] 底层仍然保留内部默认档（8.0/8.1/8.3 的断言不变）")
    check(get_profile(None) is PROFILES[AUTO.profile_id],
          "底层 get_profile(None) 仍是 auto")
    check(len(PROFILES[AUTO.profile_id].formats) == 2,
          "auto 档仍然登记着两款游戏的格式（内部能力保留，只是 worker 不再走它）")
    auto_rec = RoomCodeRecognizer(_ScriptedEngine(["ABC1234"]))
    check(auto_rec.profile.profile_id == AUTO.profile_id,
          "底层识别器仍可在显式不指定档位时使用 auto")


# ======================================================================
# 6. 切档：时序观测被清空
# ======================================================================
def test_profile_switch_resets_temporal_state():
    section("[6] 切档清空时序观测：旧档的观测不参与新档判定")

    engine = _ScriptedEngine(["ABC1234"])
    recognizer = RoomCodeRecognizer(engine, profile=DELTA_FORCE.profile_id,
                                    resolver=TemporalResolver())
    check(recognizer.recognize_frame(FRAME) is None, "三角洲档第一帧不成立")
    established = recognizer.recognize_frame(FRAME)
    check(established is not None and established.code == "ABC1234",
          "三角洲档连续两帧成立")
    check(len(recognizer.resolver.observations) > 0, "裁决器里已有观测")

    section("[6b] OCRWorker.set_profile 之后观测清空、识别器换档")
    worker, scripted = make_worker(DELTA_FORCE.profile_id, ["ABC1234"])
    worker._recognizer = RoomCodeRecognizer(scripted,
                                            profile=DELTA_FORCE.profile_id,
                                            resolver=worker._resolver)
    check(worker._recognizer.recognize_frame(FRAME) is None,
          "旧档下第一帧不成立（时序裁决器在 worker 里）")
    one = worker._recognizer.recognize_frame(FRAME)
    check(one is not None and one.code == "ABC1234", "旧档下第二帧成立")
    check(len(worker._resolver.observations) > 0,
          f"worker 的裁决器里已有旧档观测: {worker._resolver.observations}")

    worker.set_profile(VALORANT.profile_id)
    check(worker._resolver.observations == (),
          f"切档清空了观测: {worker._resolver.observations}")
    check(worker._recognizer.profile.profile_id == VALORANT.profile_id,
          "识别器已换成新档")
    check(worker._recognizer.resolver is worker._resolver,
          "识别器仍绑定 worker 的同一个裁决器")

    section("[6c] 切档后要重新累计，不会借旧档的观测“一帧就成立”")
    # 脚本：第 1 次读 7 位（旧档），之后都读 6 位（新档）
    worker2, scripted2 = make_worker(DELTA_FORCE.profile_id,
                                     ["ABC1234", "ABC123"])
    worker2._recognizer = RoomCodeRecognizer(scripted2,
                                             profile=DELTA_FORCE.profile_id,
                                             resolver=worker2._resolver)
    worker2._recognizer.recognize_frame(FRAME)   # 旧档先攒下一条观测
    check(len(worker2._resolver.observations) == 1, "旧档已有观测")
    worker2.set_profile(VALORANT.profile_id)
    first = worker2._recognizer.recognize_frame(FRAME)
    check(first is None, "切档后的第一帧不成立（观测从零开始）")
    second = worker2._recognizer.recognize_frame(FRAME)
    check(second is not None and second.code == "ABC123",
          f"重新攒够两帧才成立: {second.code if second else None}")


# ======================================================================
# 7. 空结果不触发剪贴板
# ======================================================================
def test_empty_result_does_not_copy():
    section("[7] 识别不出东西时只更新显示，不触发复制")

    copies, texts, _ = run_worker([""], DELTA_FORCE.profile_id, timeout=1.5)
    check(copies == [], f"没有房间号时不复制: {copies}")
    check(texts == [""], f"显示被清空一次: {texts}")

    copies2, texts2, _ = run_worker(["nothing here !!!"], DELTA_FORCE.profile_id,
                                    timeout=1.5)
    check(copies2 == [], f"噪声文本同样不复制: {copies2}")
    check(texts2 == [""], f"噪声文本只清空显示: {texts2}")


# ======================================================================
# main
# ======================================================================
def main() -> int:
    global app
    app = QApplication.instance() or QApplication([])

    print("=" * 70, flush=True)
    print("Phase 8.5: OCR game-selection chain suite", flush=True)
    print(f"isolated APPDATA: {os.environ['APPDATA']}", flush=True)
    print(f"choices: {[pid for pid, _ in mw.OCR_GAME_CHOICES]}", flush=True)
    print("=" * 70, flush=True)

    test_ui_choices_are_concrete_games()
    test_delta_force_chain()
    test_valorant_chain()
    test_cross_game_isolation()
    test_oversized_run_is_safe()
    test_worker_refuses_guess()
    test_profile_switch_resets_temporal_state()
    test_empty_result_does_not_copy()

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
