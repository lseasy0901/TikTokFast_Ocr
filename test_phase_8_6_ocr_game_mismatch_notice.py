#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 8.6: 「选错游戏」提醒 回归测试

产品规则（本套件逐条对应）：
    1. 当前档与识别内容【明显冲突】时：拦下这一帧（不进时序裁决），
       不触发 copy_requested、不写剪贴板，并提醒用户重新选择。
    2. 文案固定且不含任何技术细节（不出现位数、格式名、档位名，
       也不暗示程序认为应该是哪款游戏）。
    3. 程序绝不自动切换/猜测/选择游戏，也不重启 OCR、不提供一键切换。
    4. 用户自行重新选择后，下一轮 OCR 按新的具体档执行。

覆盖：
    [1] 固定文案：内容正确，且不含字母数字/档位名/游戏名
    [2] 正常选择正确游戏 -> 正常识别 -> 正常 copy_requested（回归）
    [3] 选错游戏 -> 不 copy + 只提醒一次（两个方向都试）
    [4] 错误结果被完全阻断：不进时序裁决、不会成立、恢复后仍正常
    [5] 用户重新选择后 -> 同一帧内容按新档执行
    [6] 提醒不带任何游戏信息；GUI 处理函数只提醒、不切换
    [7] 正式路径仍然禁止 profile=None / auto（回归保留）

隔离说明：
    使用脚本化假引擎，不启动 Tesseract、不读取用户截图、不接直播流；
    APPDATA 指向临时目录，不读取也不改写真实的 license.json；
    不构造 MainWindow，也不触碰系统剪贴板（断言到 copy_requested 为止）。
"""

import ast
import inspect
import os
import re
import sys
import tempfile
import textwrap
import threading
import time
from unittest import mock

import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

os.environ["APPDATA"] = tempfile.mkdtemp(prefix="dlv-ocr-mismatch-test-")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

import gui.main_window as mw  # noqa: E402
import ocr.worker as worker_mod  # noqa: E402
from ocr.profiles import (  # noqa: E402
    AUTO,
    DELTA_FORCE,
    GAME_PROFILE_IDS,
    VALORANT,
    game_profiles,
    get_profile,
)
from ocr.roi_manager import ROI, ROIManager  # noqa: E402
from ocr.room_code import RoomCodeRecognizer, SingleFrameResolver  # noqa: E402
from ocr.temporal import TemporalResolver  # noqa: E402

_PASSED = 0
_FAILED = 0
app = None

FRAME = np.zeros((20, 80, 3), dtype=np.uint8)

#: 7 位（三角洲）与 6 位（无畏契约）各一个形状明确的码
CODE_7 = "ABC1234"
CODE_6 = "ABC123"


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
    """按脚本逐帧给出读法的假 OCREngine（脚本用完后重复最后一项）。"""

    def __init__(self, texts=()):
        self._texts = list(texts)
        self._index = 0
        self.calls = 0

    def _next_text(self):
        self.calls += 1
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


def recognizer(profile, script, resolver=None):
    return RoomCodeRecognizer(_ScriptedEngine(script), profile=profile,
                              resolver=resolver or SingleFrameResolver())


def make_worker(profile, script, interval_ms=20):
    roi_manager = ROIManager()
    roi_manager.set_roi(ROI(0, 0, 60, 20, 80, 20))
    worker = worker_mod.OCRWorker(
        frame_buffer_getter=lambda: _FakeBuffer(FRAME),
        roi_manager=roi_manager,
        engine=_ScriptedEngine(script),
        interval_ms=interval_ms,
        profile=profile,
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


def run_worker(profile, script, timeout=3.0, settle=0.3, stop=True):
    """跑一小段真实工作线程，返回 (worker, copies, texts, notices)。

    notices 记录每次提醒的信号参数（本功能应当恒为空元组）。
    """
    worker = make_worker(profile, script)
    copies, texts, notices, errors = [], [], [], []
    # 用 *args 接收，才能证明信号确实不带任何游戏信息
    worker.copy_requested.connect(lambda *a: copies.append(a[0] if a else None))
    worker.text_updated.connect(lambda *a: texts.append(a[0] if a else None))
    worker.game_mismatch.connect(lambda *a: notices.append(a))
    worker.error_occurred.connect(errors.append)

    worker.start_recognition()
    try:
        pump_until(lambda: bool(copies) or bool(notices), timeout)
        deadline = time.time() + settle
        while time.time() < deadline:
            app.processEvents()
            time.sleep(0.005)
    finally:
        if stop:
            worker.stop_recognition()

    check(not errors, f"工作线程没有报错: {errors}")
    return worker, copies, texts, notices


# ======================================================================
# 1. 固定文案
# ======================================================================
def test_message_is_fixed_and_free_of_details():
    section("[1] 提醒文案固定，且不含任何技术细节")

    message = mw.OCR_GAME_MISMATCH_MESSAGE
    check(message == "检测到选择错误，请重新选择游戏类型后再进行识别。",
          f"文案与产品要求逐字一致: {message!r}")
    check(message.endswith("。"), "文案以句号结尾，是完整的一句话")

    # 最强的机械保证：一个字母数字都没有 -> 不可能泄漏位数/格式名/档位名
    check(re.search(r"[A-Za-z0-9]", message) is None,
          "文案里没有任何字母或数字（不可能出现 6/7、格式名、profile_id）")
    for token in ("位", "格式", "长度", "档", "profile", "auto"):
        check(token not in message, f"文案不含技术词汇 {token!r}")
    for profile in game_profiles():
        check(profile.profile_id not in message,
              f"文案不含档位名 {profile.profile_id!r}")
        check(profile.display_name not in message,
              f"文案不含游戏名 {profile.display_name!r}")
    for name in (DELTA_FORCE.primary_format.name, VALORANT.primary_format.name):
        check(name not in message, f"文案不含格式名 {name!r}")
    check("三角洲" not in message and "无畏契约" not in message,
          "文案没有暗示或指出程序认为应该是哪款游戏")
    check("选择" in message and "重新" in message,
          "文案明确要求用户重新选择（而不是让用户猜该做什么）")


# ======================================================================
# 2. 正常选择 -> 正常识别 -> 正常复制（回归）
# ======================================================================
def test_correct_selection_still_copies():
    section("[2] 选择正确游戏：正常识别并复制，不产生提醒")

    worker, copies, texts, notices = run_worker(DELTA_FORCE.profile_id, [CODE_7])
    check(copies == [CODE_7], f"三角洲：正常复制一次: {copies}")
    check(notices == [], f"没有误报提醒: {notices}")
    check(texts and texts[-1] == CODE_7, f"显示同步更新: {texts}")
    check(worker._recognizer.last_conflict is None, "识别器也没有记录冲突")

    worker2, copies2, _texts2, notices2 = run_worker(VALORANT.profile_id, ["Room Code: ABC12O"])
    check(copies2 == ["ABC120"], f"无畏契约：正常复制一次: {copies2}")
    check(notices2 == [], f"没有误报提醒: {notices2}")


# ======================================================================
# 3. 选错游戏 -> 不复制 + 只提醒一次
# ======================================================================
def test_wrong_selection_blocks_copy_and_notifies_once():
    section("[3] 选错游戏：绝不复制，且只提醒一次")

    # 方向一：选了无畏契约，画面里是 7 位的三角洲码
    worker, copies, texts, notices = run_worker(VALORANT.profile_id, [CODE_7],
                                                timeout=2.0, settle=0.5)
    check(copies == [], f"选错游戏时从不触发 copy_requested: {copies}")
    check(len(notices) == 1, f"提醒恰好一次（不是每帧一次）: {len(notices)} 次")
    check(notices == [()], f"提醒信号不携带任何参数: {notices}")
    check(texts == [""], f"只把识别结果清空，没有任何房间号被显示: {texts}")
    check(worker._recognizer.last_conflict is not None,
          "识别器记录了冲突证据（供日志用，不面向用户）")

    # 方向二：选了三角洲，画面里是 6 位的无畏契约码
    worker2, copies2, _texts2, notices2 = run_worker(DELTA_FORCE.profile_id, [CODE_6],
                                                     timeout=2.0, settle=0.5)
    check(copies2 == [], f"反向同样不复制: {copies2}")
    check(len(notices2) == 1, f"反向同样只提醒一次: {len(notices2)} 次")

    section("[3b] 冲突期间剪贴板始终不被触碰")
    check(worker.recognition_count > 10,
          f"已经识别了很多帧（{worker.recognition_count} 次）")
    check(copies == [], "多帧冲突之后依然没有一次复制请求")


# ======================================================================
# 4. 错误结果被完全阻断
# ======================================================================
def test_wrong_result_is_fully_blocked():
    section("[4] 冲突帧不进时序裁决，也不会成立")

    resolver = TemporalResolver()
    rec = recognizer(VALORANT.profile_id, [CODE_7], resolver=resolver)
    results = [rec.recognize_frame(FRAME) for _ in range(4)]
    check(results == [None, None, None, None],
          f"连续多帧都不返回结果: {[str(r) if r else None for r in results]}")
    # 裁决器照常按帧推进（一帧只 offer 一次），但进来的只有 None：
    # 错误游戏的房间号从未进入时序窗口。
    check(all(o is None for o in resolver.observations),
          f"时序窗口里只有 None，没有任何房间号: {resolver.observations}")
    check(len(resolver.observations) == resolver.window_size,
          "每帧仍然对应一次 offer（时序窗口按帧推进，契约不变）")
    check(resolver.last_emitted_code is None, "也从未成立过任何房间号")
    check(rec.last_conflict is not None, "最后一帧同样判定为冲突")

    section("[4b] 冲突解除后管线照常工作")
    resolver2 = TemporalResolver()
    # 第 1 帧是错误游戏的形状，第 2 帧起是本档的码
    rec2 = recognizer(VALORANT.profile_id, [CODE_7, CODE_6], resolver=resolver2)
    first = rec2.recognize_frame(FRAME)
    first_conflict = rec2.last_conflict      # last_conflict 是「最近一帧」的状态
    second = rec2.recognize_frame(FRAME)
    second_conflict = rec2.last_conflict
    third = rec2.recognize_frame(FRAME)
    check(first is None and first_conflict is not None, "第一帧被拦下")
    check(second is None and second_conflict is None,
          "第二帧不再冲突，但还需要一次一致")
    check(third is not None and third.code == CODE_6,
          f"第三帧正常成立: {third.code if third else None}")

    section("[4c] 拦下的帧不会把「错误形状」也算成一次一致")
    # 若 7 位码真的进了裁决器，'ABC123' 与 'ABC1234' 交替时会互相打断；
    # 这里连续两次错误形状 + 一次正确形状就必须成立，证明前者完全没进去
    resolver3 = TemporalResolver()
    rec3 = recognizer(VALORANT.profile_id, [CODE_7, CODE_7, CODE_6], resolver=resolver3)
    a = rec3.recognize_frame(FRAME)
    b = rec3.recognize_frame(FRAME)
    c = rec3.recognize_frame(FRAME)
    d = rec3.recognize_frame(FRAME)
    check(a is None and b is None, "两次错误形状都不成立")
    check(c is None or c.code == CODE_6, "正确形状开始累计")
    check(d is not None and d.code == CODE_6,
          f"连续两次正确形状即成立: {d.code if d else None}")
    check({o for o in resolver3.observations if o is not None} == {CODE_6},
          f"窗口里出现过的房间号只有本档的码: {resolver3.observations}")


# ======================================================================
# 5. 用户重新选择后按新档执行
# ======================================================================
def test_reselection_uses_new_profile():
    section("[5] 用户重新选择后，同一帧内容按新档执行")

    worker, copies, _texts, notices = run_worker(VALORANT.profile_id, [CODE_7],
                                                 timeout=2.0, settle=0.3, stop=False)
    check(copies == [] and len(notices) == 1, "先确认处于「选错」状态")
    thread_before = worker._recognizer.engine  # 引擎对象（证明没重建引擎）

    # 用户自己重新选择（等价于界面里改选游戏）
    worker.set_profile(DELTA_FORCE.profile_id)
    pump_until(lambda: bool(copies), 3.0)
    check(worker._recognizer.profile.profile_id == DELTA_FORCE.profile_id,
          "识别器已按新的具体档工作")
    check(copies == [CODE_7], f"同一帧内容现在正常复制: {copies}")
    check(len(notices) == 1, "重新选择没有额外提醒")

    check(worker.isRunning(), "OCR 线程没有被重启（仍在同一个线程里跑）")
    check(worker._recognizer.engine is thread_before, "引擎没有被重建")
    check(worker._profile == DELTA_FORCE.profile_id, "worker 的档位就是新选的档")
    worker.stop_recognition()

    section("[5b] 切档后允许对新的选择再次提醒")
    worker2, copies2, _t2, notices2 = run_worker(DELTA_FORCE.profile_id, [CODE_6],
                                                 timeout=2.0, settle=0.3, stop=False)
    check(len(notices2) == 1, "第一次选错提醒了一次")
    # 用户改选成无畏契约（仍不相符，因为画面里是 7 位码）
    worker2.set_profile(VALORANT.profile_id)
    worker2._engine._texts = [CODE_7]   # 换成 7 位码，对新档同样是选错
    worker2._engine._index = 0
    pump_until(lambda: len(notices2) > 1, 3.0)
    check(len(notices2) == 2, f"新的一轮选择可以再提醒一次: {len(notices2)} 次")
    worker2.stop_recognition()


# ======================================================================
# 6. 不自动切换 / 不泄漏内部判断
# ======================================================================
def test_no_auto_switch_and_no_leak():
    section("[6] 提醒不带游戏信息，程序不做任何自动选择")

    worker = make_worker(VALORANT.profile_id, [CODE_7])
    notices = []
    worker.game_mismatch.connect(lambda *a: notices.append(a))
    worker.start_recognition()
    pump_until(lambda: bool(notices), 2.0)
    worker.stop_recognition()

    check(notices and all(a == () for a in notices),
          f"提醒信号没有携带任何参数（无法据此得知另一款游戏）: {notices}")
    check(worker._profile == VALORANT.profile_id, "冲突后 worker 档位没有被改动")
    check(worker._recognizer.profile.profile_id == VALORANT.profile_id,
          "冲突后识别器档位没有被改动")
    check(worker._recognizer.last_conflict is not None
          and worker._recognizer.last_conflict.other_profile_id in GAME_PROFILE_IDS,
          "冲突证据只存在于后端内部（用于日志），不经过信号外传")

    section("[6b] GUI 处理函数：只提醒，不切换")
    box_calls = []

    class _MsgBox:
        @staticmethod
        def warning(parent, title, text):
            box_calls.append((parent, title, text))

    class _StatusBar:
        def __init__(self):
            self.messages = []

        def showMessage(self, message):
            self.messages.append(message)

    class _FakeWindow:
        """只提供 _on_ocr_game_mismatch 需要的东西（不构造真实 MainWindow）。"""

        def __init__(self):
            self._status = _StatusBar()
            self._ocr_profile_id = DELTA_FORCE.profile_id
            self.profile_switches = []

        def statusBar(self):
            return self._status

    fake = _FakeWindow()
    with mock.patch.object(mw, "QMessageBox", _MsgBox):
        mw.MainWindow._on_ocr_game_mismatch(fake)

    check(len(box_calls) == 1, f"恰好弹一次提示: {len(box_calls)} 次")
    _parent, title, text = box_calls[0]
    check(text == mw.OCR_GAME_MISMATCH_MESSAGE, f"提示内容就是固定文案: {text!r}")
    check(title == "提示", f"标题中立，不含游戏信息: {title!r}")
    check(fake._status.messages == [mw.OCR_GAME_MISMATCH_MESSAGE],
          f"状态栏也只显示该文案: {fake._status.messages}")
    check(fake._ocr_profile_id == DELTA_FORCE.profile_id,
          "处理函数没有改动当前选择的游戏")
    check(fake.profile_switches == [], "处理函数没有发起任何切档调用")

    section("[6c] 处理函数里不存在自动切换/一键切换的代码路径")
    handler = mw.MainWindow._on_ocr_game_mismatch
    # 去掉 docstring 与注释，只看真正会执行的代码
    # （getsource 带类内缩进，先 dedent 再用 AST 解析）
    tree = ast.parse(textwrap.dedent(inspect.getsource(handler)))
    function = tree.body[0]
    function.body = [
        node for node in function.body
        if not (isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str))
    ]
    body = ast.unparse(function)
    for forbidden in ("set_profile", "_ocr_profile_id", "auto", "自动", "profile"):
        check(forbidden not in body, f"函数体里没有 {forbidden!r}")

    # 没有任何「一键切换」控件：提示框只用默认按钮（不额外传按钮参数）
    check("QMessageBox.warning" in body and "Button" not in body,
          "提示框只用默认按钮，不提供「切换为 XX」之类的操作")


# ======================================================================
# 7. 正式路径仍然禁止猜测（回归保留）
# ======================================================================
def test_worker_still_requires_explicit_game():
    section("[7] 正式路径仍然禁止 profile=None / auto")

    roi_manager = ROIManager()
    roi_manager.set_roi(ROI(0, 0, 60, 20, 80, 20))

    def build(profile):
        return worker_mod.OCRWorker(
            frame_buffer_getter=lambda: None,
            roi_manager=roi_manager,
            interval_ms=300,
            profile=profile,
        )

    for bad in (None, AUTO.profile_id):
        try:
            build(bad)
            check(False, f"{bad!r} 应当被拒绝")
        except ValueError:
            check(True, f"{bad!r} 仍然被拒绝（不会用内容猜游戏）")
    check(get_profile(None).profile_id == AUTO.profile_id,
          "底层默认档 auto 仍然保留（只是正式路径不再走它）")


# ======================================================================
# main
# ======================================================================
def main() -> int:
    global app
    app = QApplication.instance() or QApplication([])

    print("=" * 70, flush=True)
    print("Phase 8.6: OCR wrong-game-selection notice suite", flush=True)
    print(f"isolated APPDATA: {os.environ['APPDATA']}", flush=True)
    print(f"message: {mw.OCR_GAME_MISMATCH_MESSAGE}", flush=True)
    print("=" * 70, flush=True)

    test_message_is_fixed_and_free_of_details()
    test_correct_selection_still_copies()
    test_wrong_selection_blocks_copy_and_notifies_once()
    test_wrong_result_is_fully_blocked()
    test_reselection_uses_new_profile()
    test_no_auto_switch_and_no_leak()
    test_worker_still_requires_explicit_game()

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
