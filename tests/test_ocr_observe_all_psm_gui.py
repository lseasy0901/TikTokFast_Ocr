#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
全 PSM 观测 —— GUI → OCRWorker → RoomCodeRecognizer 端到端验证

链路里除了「什么时候跑」之外全部是真的：
    - GUI 的复选框 / 锁定 / 取消勾选等交互是真的；
    - OCRWorker 是真的（只把引擎与识别器换成桩、把线程换成同步驱动）；
    - RoomCodeRecognizer 与 OCRDiagnostics 是真的；
    - 复制请求走 OCRWorker 自己的判定与 copy_requested 信号。
只有 OCREngine 被桩掉（不加载 Tesseract），以及 QThread 被换成逐帧同步驱动
（保证可重复、不依赖调度）。

覆盖：
    A. 控件形状与初始状态：默认不勾选、未开诊断时禁用、位于现有 OCR 面板内
    B. 开启观测：诊断 CSV 记录完整 PSM 6 / 观测7 / 观测11
    C. 关闭观测：仍然提前停止，且不产生额外 OCR 调用
    D. 相同脚本输入下，开 / 关的复制请求序列完全一致
    E. 停止与重新启动后的开关状态（运行期锁定、停止后恢复、不残留旧状态）
    F. 所有游戏档的现有滑窗规则不变

约定与仓库其它 test_*.py 一致：扁平脚本、check()/section() 自行统计，
直接 ``python test_ocr_observe_all_psm_gui.py`` 运行，不依赖 pytest。
不联网、不写用户配置（APPDATA / LOCALAPPDATA 已被指向临时目录）。
"""

import os
import sys
import tempfile
import threading
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

os.environ["APPDATA"] = tempfile.mkdtemp(prefix="dlv-obs-gui-appdata-")
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="dlv-obs-gui-local-")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np                                              # noqa: E402
from PySide6.QtWidgets import QApplication                      # noqa: E402

import gui.main_window as mw                                    # noqa: E402
from gui import theme                                           # noqa: E402
from ocr import worker as worker_mod                            # noqa: E402
from ocr.profiles import AUTO, DELTA_FORCE, DELTA_FORCE_1V1, VALORANT  # noqa: E402
from ocr.roi_manager import ROI, ROIManager                     # noqa: E402
from ocr.room_code import (                                     # noqa: E402
    _MIN_WINDOW_RESIDUE,
    RoomCodeRecognizer,
)
from ocr.temporal import TemporalResolver                       # noqa: E402

app = QApplication.instance() or QApplication([])
theme.apply_theme(app)

#: 原件留底，测试结束时还原（本脚本会把 mw.QMessageBox 换成非模态替身）
_REAL_MESSAGE_BOX = mw.QMessageBox

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


def pump(rounds: int = 20, delay: float = 0.002) -> None:
    for _ in range(rounds):
        app.processEvents()
        time.sleep(delay)


FRAME = np.zeros((20, 80, 3), dtype=np.uint8)
PROFILE = DELTA_FORCE_1V1.profile_id

#: 帧脚本：PSM 6 给出「零纠正」的码，PSM 7/11 却给出【另一个】零纠正的码。
#: 这是最坏情形——若观测读法进了仲裁就会变成「读法歧义」-> 无结果。
DISAGREEING = [(6, "BFRG9516"), (7, "BFRG9510"), (11, "BFRG9510")]


# ======================================================================
# 桩：真 OCRWorker 的子类
# ======================================================================
class ScriptedEngine:
    """脚本 = 每帧一份 [(psm, 文本), ...]；脚本用完后重复最后一项。"""

    def __init__(self, frames):
        self._frames = list(frames)
        self._index = 0
        self.calls = 0
        self.readings_served = 0

    def iter_readings(self, image, preprocess: bool = True):
        self.calls += 1
        readings = self._frames[min(self._index, len(self._frames) - 1)]
        self._index += 1
        for psm, text in readings:
            self.readings_served += 1
            yield (psm, text)


class FakeBuffer:
    """最小帧缓冲替身：只实现 OCR 与关窗清理会用到的那几个入口。"""

    def __init__(self, frame=None):
        self._frame = frame

    def get(self):
        return self._frame

    def clear(self):
        self._frame = None


class StubWorker(worker_mod.OCRWorker):
    """真 OCRWorker，但把引擎/识别器换成桩，并把线程换成同步驱动。"""

    script = []
    built = []

    def __init__(self, **kwargs):
        engine = ScriptedEngine(type(self).script)
        kwargs["engine"] = engine
        kwargs["recognizer"] = RoomCodeRecognizer(
            engine,
            profile=kwargs["profile"],
            resolver=TemporalResolver(),
            diagnostics=kwargs.get("diagnostics"),
            observe_all_psm=kwargs.get("observe_all_psm", False),
        )
        super().__init__(**kwargs)
        self.engine = engine
        self.start_calls = 0
        self.stop_calls = 0
        type(self).built.append(self)

    def start_recognition(self) -> None:
        # 不真起线程：测试逐帧同步驱动，结果可重复
        self.start_calls += 1

    def stop_recognition(self) -> None:
        self.stop_calls += 1


class _StubActivationDialog:
    """替身：模态弹窗在无头环境会永久阻塞。"""

    def __init__(self, manager, parent=None):
        pass

    def exec(self):
        return 0

    def result_data(self):
        return None


class _StubMessageBox:
    """替身：QMessageBox 是模态的，无头环境下会永久阻塞。调用被记下来供断言。"""

    calls = []

    @staticmethod
    def _record(title, text):
        _StubMessageBox.calls.append((title, text))
        return 0

    warning = staticmethod(lambda parent, title, text, *a, **k:
                           _StubMessageBox._record(title, text))
    information = staticmethod(lambda parent, title, text, *a, **k:
                               _StubMessageBox._record(title, text))
    critical = staticmethod(lambda parent, title, text, *a, **k:
                            _StubMessageBox._record(title, text))


def build_window():
    real_dialog = mw.ActivationDialog
    mw.ActivationDialog = _StubActivationDialog
    mw.OCRWorker = StubWorker
    mw.QMessageBox = _StubMessageBox          # 冲突提示等模态弹窗的替身
    _StubMessageBox.calls = []
    try:
        window = mw.MainWindow()
        window.show()
    finally:
        mw.ActivationDialog = real_dialog
    pump(30)
    window._access_status.set_license_active(None)
    # 让 _start_ocr 的前置检查全部通过（不涉及网络，也不改配置）
    window._connected = True
    window._frame_buffer = FakeBuffer(FRAME)
    roi_manager = ROIManager()
    roi_manager.set_roi(ROI(0, 0, 60, 20, 80, 20))
    window._roi_manager = roi_manager
    pump(10)
    return window


def select_profile(window, profile_id):
    """用真实下拉框选中游戏档（走 _on_ocr_game_changed，不直接改字段）。"""
    combo = window._ocr_game_combo
    for index in range(combo.count()):
        if combo.itemData(index) == profile_id:
            combo.setCurrentIndex(index)
            pump(5)
            return True
    return False


def restore_worker_class() -> None:
    mw.OCRWorker = worker_mod.OCRWorker


def drive_frames(worker, count, frames_pump=10):
    """在一根独立线程里逐帧执行 —— 和真 worker 的线程模型一致。

    这一点很关键：信号跨线程时是【排队投递】的，GUI 的复制槽会在
    _perform_recognition() 的 finish() 之后才跑，诊断才能把复制结果回填给
    产生该请求的那次执行。若在同一根线程里直接驱动，槽会在 emit() 里同步
    跑掉（那时记录还没入库），复制结果永远回填不上 —— 那是测试假象。
    """
    for _ in range(count):
        thread = threading.Thread(target=worker._perform_recognition)
        thread.start()
        thread.join()
        pump(frames_pump)


def start_with_script(window, script, frames=3):
    """走真实的 _start_ocr：GUI 构造 OCRWorker 并接上全部信号。"""
    StubWorker.script = list(script)
    select_profile(window, PROFILE)   # 脚本按 1v1 的 8 位码写，档位必须对上
    window._start_ocr()
    worker = window._ocr_worker
    worker.started.emit()             # 与真线程一样，用信号通知 GUI
    copies = []
    worker.copy_requested.connect(copies.append)
    drive_frames(worker, frames)
    return worker, copies


# ======================================================================
# A. 控件形状与初始状态
# ======================================================================
def test_widget_shape() -> None:
    section("A. 控件形状与初始状态")

    window = build_window()
    obs = window._diag_obs_check

    check(obs.text() == "全 PSM 观测", f"文案 = {obs.text()!r}")
    check(not obs.isChecked(), "默认不勾选")
    check(not obs.isEnabled(), "诊断未开启时不可勾选")

    # 不新增独立页面：复选框就在现有 OCR 面板里，与诊断按钮同处一栏
    panel = window._ocr_panel
    check(panel.isAncestorOf(obs), "复选框位于现有 OCR 面板之内（没有新页面）")
    check(panel.isAncestorOf(window._diag_btn),
          "与「开始诊断」按钮同处一个面板")

    # 开启诊断 -> 可勾选；关闭诊断 -> 回到禁用并清除勾选
    window._on_diag_toggled()
    check(window._diagnostics.enabled, "点「开始诊断」后诊断开启")
    check(obs.isEnabled(), "诊断开启后复选框可勾选")

    obs.setChecked(True)
    check(obs.isChecked(), "可以勾选")

    window._on_diag_toggled()
    check(not window._diagnostics.enabled, "再次点击后诊断关闭")
    check(not obs.isChecked(), "关闭诊断同时清除勾选（界面与实际行为一致）")
    check(not obs.isEnabled(), "关闭诊断后复选框重新禁用")

    restore_worker_class()
    window.close()


# ======================================================================
# B. 开启观测：诊断 CSV 记录完整 PSM
# ======================================================================
def test_enabled_records_all_psm() -> None:
    section("B. 开启观测：诊断 CSV 记录完整 PSM 6/7/11")

    window = build_window()
    window._on_diag_toggled()                       # 开始诊断
    window._diag_obs_check.setChecked(True)         # 勾上全 PSM 观测

    worker, copies = start_with_script(window, [DISAGREEING], frames=1)

    check(worker.observe_all_psm is True,
          "OCRWorker 收到了 observe_all_psm=True")
    check(worker._recognizer.observe_all_psm is True,
          "识别器确实带着观测开关（GUI → worker → recognizer 贯通）")

    records = list(window._diagnostics.records)
    check(len(records) >= 1, f"诊断有记录：{len(records)} 条")
    trace = records[-1]
    labels = [n.label for n in trace.readings]
    check(labels == ["6", "观测7", "观测11"],
          f"三档读法全部入账：{labels}")
    check(worker.engine.readings_served == 3,
          f"引擎确实被取了 3 个读法：{worker.engine.readings_served}")

    csv_text = window._diagnostics.to_csv_text()
    check("观测7:BFRG9510" in csv_text and "观测11:BFRG9510" in csv_text,
          "导出的诊断 CSV 里带着「观测」标签与补跑读法")
    check("原本会在 PSM 6 提前停止" in csv_text,
          "CSV 说明原本会在哪一档提前停止")

    window._stop_ocr()
    restore_worker_class()
    window.close()


# ======================================================================
# C. 关闭观测：仍然提前停止
# ======================================================================
def test_disabled_still_early_stops() -> None:
    section("C. 关闭观测：仍然提前停止，无额外 OCR 调用")

    window = build_window()
    window._on_diag_toggled()                       # 诊断开、观测不勾

    worker, _ = start_with_script(window, [DISAGREEING], frames=1)

    check(worker.observe_all_psm is False, "OCRWorker 收到 observe_all_psm=False")
    check(worker._recognizer.observe_all_psm is False, "识别器没带观测开关")

    trace = list(window._diagnostics.records)[-1]
    labels = [n.label for n in trace.readings]
    check(labels == ["6"], f"只记录到提前停止的那一档：{labels}")
    check(worker.engine.readings_served == 1,
          f"引擎只被取了 1 个读法（没有额外 OCR 调用）："
          f"{worker.engine.readings_served}")
    check(trace.notes == ["提前停止于 PSM 6（该读法零纠正零折叠）"],
          f"备注措辞与从前一致：{trace.notes}")

    window._stop_ocr()
    restore_worker_class()
    window.close()


# ======================================================================
# D. 开 / 关的复制请求序列完全一致
# ======================================================================
def test_copy_sequence_identical() -> None:
    section("D. 相同脚本输入下，开 / 关的复制请求序列完全一致")

    def run(observe: bool):
        window = build_window()
        window._on_diag_toggled()
        window._diag_obs_check.setChecked(observe)
        worker, copies = start_with_script(window, [DISAGREEING], frames=3)
        result = {
            "copies": list(copies),
            "worker_text": window._last_text if hasattr(window, "_last_text") else None,
            "diag_copy": [t.copy_requested for t in window._diagnostics.records
                          if t.copy_requested],
            "diag_result": [t.copy_result for t in window._diagnostics.records
                            if t.copy_result],
            "arbitration": [t.arbitration for t in window._diagnostics.records],
            "readings_served": worker.engine.readings_served,
            "dialogs": list(_StubMessageBox.calls),
        }
        window._stop_ocr()
        restore_worker_class()
        window.close()
        return result

    off = run(False)
    on = run(True)

    check(off["copies"] == on["copies"] == ["BFRG9516"],
          f"复制请求信号一致：关闭={off['copies']} 开启={on['copies']}")
    check(off["diag_copy"] == on["diag_copy"] == ["BFRG9516"],
          f"诊断记录的复制请求一致：关闭={off['diag_copy']} "
          f"开启={on['diag_copy']}")
    check(off["diag_result"] == on["diag_result"] == ["成功"],
          f"复制结果一致（成功写入剪贴板）：关闭={off['diag_result']} "
          f"开启={on['diag_result']}")
    check(off["arbitration"] == on["arbitration"],
          f"每帧仲裁结论一致：{on['arbitration']}")

    # 最坏情形下尤其关键：PSM 7/11 与 PSM 6 分歧，开启后结果【没有】变成
    # 「读法歧义」-> 无结果，仍然是 PSM 6 的 BFRG9516。
    check(off["arbitration"] == ["选定:BFRG9516"] * 3,
          f"分歧读法没有污染仲裁：{off['arbitration']}")
    check(on["readings_served"] == 9 and off["readings_served"] == 3,
          f"额外开销只体现在读法次数上：关闭 {off['readings_served']} 次 / "
          f"开启 {on['readings_served']} 次")

    # 全程不该出现任何模态提示（尤其是「选错游戏」）
    check(off["dialogs"] == [] and on["dialogs"] == [],
          f"没有触发任何模态提示：关闭={off['dialogs']} 开启={on['dialogs']}")


# ======================================================================
# E. 停止 / 重新启动后的开关状态
# ======================================================================
def test_restart_state() -> None:
    section("E. 停止与重新启动后的开关状态")

    window = build_window()
    obs = window._diag_obs_check
    window._on_diag_toggled()

    # 第一轮：勾上观测
    window._diag_obs_check.setChecked(True)
    StubWorker.script = [DISAGREEING]
    select_profile(window, PROFILE)
    window._start_ocr()
    worker_1 = window._ocr_worker
    worker_1.started.emit()
    check(worker_1.observe_all_psm is True, "第一轮：worker 开启观测")
    check(not obs.isEnabled(), "识别运行期间复选框被锁定")

    # 运行期锁定时，观测状态不会漂移
    check(obs.isChecked(), "锁定期间勾选状态保持")

    window._stop_ocr()
    check(obs.isEnabled(), "停止后复选框恢复可编辑")
    check(obs.isChecked(), "停止后仍保留用户此前的勾选（不擅自清空）")

    # 第二轮：取消勾选后重启 -> 不得残留上一轮的观测状态
    window._diag_obs_check.setChecked(False)
    select_profile(window, PROFILE)
    window._start_ocr()
    worker_2 = window._ocr_worker
    worker_2.started.emit()
    check(worker_2 is not worker_1, "重新启动确实新建了 worker")
    check(worker_2.observe_all_psm is False,
          "第二轮：worker 的观测开关为 False（没有残留）")
    check(worker_2._recognizer.observe_all_psm is False,
          "第二轮：识别器也没有残留观测状态")

    worker_2._perform_recognition()
    trace = list(window._diagnostics.records)[-1]
    check([n.label for n in trace.readings] == ["6"],
          f"第二轮实际行为是提前停止：{[n.label for n in trace.readings]}")

    window._stop_ocr()
    check(not window._ocr_running, "停止后 _ocr_running 归位")

    restore_worker_class()
    window.close()


# ======================================================================
# F. 所有游戏档的滑窗规则不变
# ======================================================================
def test_sliding_rule_unchanged() -> None:
    section("F. 所有游戏档的滑窗规则不变（残差 >= 2）")

    check(_MIN_WINDOW_RESIDUE == 2, f"残差下限仍是 {_MIN_WINDOW_RESIDUE}")

    class _FakeEngine:
        def recognize(self, image, preprocess=True):
            return ""

    def code_of(profile_id, text):
        result = RoomCodeRecognizer(_FakeEngine(), profile=profile_id)._resolve_text(text)
        return result.code if result is not None else None

    # 每个档：残差 1 一律拒绝（本规则），残差 >= 2 的相邻文本仍可滑窗
    cases = (
        # (档, 残差1 的串, 残差>=2 的相邻文本串, 期望结果)
        (DELTA_FORCE_1V1.profile_id, "ABCD12345", "ABCD1234xy", "ABCD1234"),
        (DELTA_FORCE.profile_id, "ABC1234x", "ABC1234other", "ABC1234"),
        (VALORANT.profile_id, "ABC123x", "ABC123xy", "ABC123"),
        (AUTO.profile_id, "ABC123X", "ABC1234other", "ABC1234"),
    )
    for profile_id, residue_one, glue, expected in cases:
        got_one = code_of(profile_id, residue_one)
        check(got_one is None,
              f"{profile_id}: 残差 1 的 {residue_one!r} -> 无结果（得到 {got_one!r}）")
        got_glue = code_of(profile_id, glue)
        check(got_glue == expected,
              f"{profile_id}: 残差 >= 2 的 {glue!r} -> {expected!r}"
              f"（得到 {got_glue!r}）")

    # auto 档是多格式（7/8/6 位同时尝试），残差门按【每个格式各自】计算：
    # 'ABC1234x' 对 7 位格式被拒（残差 1）、对 8 位格式被拒（末位 x 不可纠正），
    # 但对 6 位格式残差是 2，于是 6 位窗口仍然生效 -> 'ABC123'。
    # 这是多格式滑窗的既有结果（Phase 8 起如此），不是残差门的漏洞。
    check(code_of(AUTO.profile_id, "ABC1234x") == "ABC123",
          "auto 档 'ABC1234x' -> 'ABC123'（6 位格式残差为 2，属既有行为）")

    # auto 档的质量排序不变：8 位码走 EXACT，不被 7 位格式截断
    check(code_of(AUTO.profile_id, "ABCD1234") == "ABCD1234",
          "auto 档 'ABCD1234' 仍走 EXACT（不被截成 BCD1234）")

    # 回归日志里的三个异常串在 1v1 档仍然不给任何候选
    for raw in ("UJIMI8506", "QORTP8641", "TYGH57380"):
        check(code_of(DELTA_FORCE_1V1.profile_id, raw) is None,
              f"1v1 档 {raw!r} 仍然无候选")


# ======================================================================
# G. 换档重建识别器时观测开关不丢（第二条构造路径）
# ======================================================================
def test_profile_switch_keeps_flag() -> None:
    section("G. 换档重建识别器时，观测开关照样透传")

    from ocr.diagnostics import OCRDiagnostics

    roi_manager = ROIManager()
    roi_manager.set_roi(ROI(0, 0, 60, 20, 80, 20))

    for observe in (True, False):
        engine = ScriptedEngine([DISAGREEING])
        diagnostic = OCRDiagnostics(enabled=True)
        worker = worker_mod.OCRWorker(
            frame_buffer_getter=lambda: FakeBuffer(FRAME),
            roi_manager=roi_manager,
            engine=engine,                     # 有引擎 + 已建识别器 -> 换档会重建
            interval_ms=20,
            profile=PROFILE,
            diagnostics=diagnostic,
            observe_all_psm=observe,
        )
        worker._ensure_recognizer()
        check(worker._recognizer.observe_all_psm is observe,
              f"初始识别器带开关 {observe}")

        worker.set_profile(DELTA_FORCE.profile_id)   # 换到 7 位档，重建识别器
        check(worker._recognizer.observe_all_psm is observe,
              f"换档重建后的识别器仍是 {observe}（第二条构造路径也透传）")

        worker.set_profile(PROFILE)
        check(worker._recognizer.observe_all_psm is observe,
              f"换回来之后仍然是 {observe}")


def main() -> int:
    try:
        test_widget_shape()
        test_enabled_records_all_psm()
        test_disabled_still_early_stops()
        test_copy_sequence_identical()
        test_restart_state()
        test_sliding_rule_unchanged()
        test_profile_switch_keeps_flag()
    finally:
        restore_worker_class()
        mw.QMessageBox = _REAL_MESSAGE_BOX

    print(f"\n{'=' * 52}")
    print(f"通过 {_PASSED} 项，失败 {_FAILED} 项")
    print("=" * 52)
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
