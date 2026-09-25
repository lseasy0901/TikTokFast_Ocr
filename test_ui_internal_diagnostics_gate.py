#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
生产 UI 内部诊断入口的可见性开关 —— 验证脚本

背景：
    OCR 诊断 / 全 PSM 观测 / ROI 采样都是开发与离线基准用的内部手段，
    正式用户不需要看到。本轮把它们从生产 UI 上**隐藏**，但一行实现都不删。

开关（单一入口）：
    ``gui.main_window.SHOW_INTERNAL_DIAGNOSTICS``，默认 ``False``。

    它只控制"看不看得见"。被隐藏的四个入口 —— 「开始诊断」「导出诊断 CSV」
    「全 PSM 观测」「采集 ROI 样本」—— 对应的控件对象、信号连接、
    OCRDiagnostics、SampleCapture、observe_all_psm 全链路一律原样保留，
    置为 True 即完整恢复。

覆盖：
    A. 默认（开关 False）：四个入口不可见，但控件对象仍存在、仍挂在面板内
       ——证明是"隐藏"而不是"删除"
    B. 关闭后不留空白：隐藏行不占高度、不产生行间距（几何断言，不是看代码）
    C. 开关 True：四个入口完整恢复（可见、可点、文案与状态原样）
    D. 正式产品入口不受影响（导出 CSV / 历史 / ROI / 开始 OCR / 连接 …）
    E. 隐藏不改行为：勾选状态仍可读、开关仍可切换（_start_ocr 读的就是它）

约定与仓库其它 test_*.py 一致：扁平脚本、check()/section() 自行统计，
直接 ``python test_ui_internal_diagnostics_gate.py`` 运行，不依赖 pytest。
APPDATA / LOCALAPPDATA 指向临时目录，不写用户真实配置；不联网、不起识别线程。
"""

import os
import sys
import tempfile
import time

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

os.environ["APPDATA"] = tempfile.mkdtemp(prefix="dlv-diaggate-appdata-")
os.environ.setdefault("LOCALAPPDATA", tempfile.mkdtemp(prefix="dlv-diaggate-local-"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QCheckBox, QWidget  # noqa: E402

import gui.main_window as mw  # noqa: E402

app = QApplication.instance() or QApplication([])

WIN_W, WIN_H = 1280, 800

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
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def pump(rounds: int = 30, delay: float = 0.005) -> None:
    for _ in range(rounds):
        app.processEvents()
        time.sleep(delay)


class _StubActivation:
    """替掉激活对话框，避免构造主窗口时弹出模态窗口。"""

    def __init__(self, *a, **k):
        pass

    def exec(self):
        return 0

    def result_data(self):
        return None


def build_window(show_internal: bool):
    """按指定的开关值构造并显示一个主窗口。

    开关在构造时才被读取，所以在构造**之前**改模块属性即可控制这一个窗口，
    不会污染其它窗口 —— 两个开关值各建一个窗口就能直接比对几何。
    """
    real_flag = mw.SHOW_INTERNAL_DIAGNOSTICS
    real_dialog = mw.ActivationDialog
    mw.SHOW_INTERNAL_DIAGNOSTICS = show_internal
    mw.ActivationDialog = _StubActivation
    try:
        window = mw.MainWindow()
        window.resize(WIN_W, WIN_H)
        window.show()
    finally:
        mw.SHOW_INTERNAL_DIAGNOSTICS = real_flag
        mw.ActivationDialog = real_dialog
    pump(40)
    window._access_status.set_license_active(None)
    pump(20)
    return window


#: 四个内部诊断入口的「控件存在性」判定：属性名 -> 期望文案
INTERNAL_ENTRIES = {
    "_diag_btn": "开始诊断",
    "_diag_export_btn": "导出诊断 CSV",
    "_diag_obs_check": "全 PSM 观测",
    "_diag_capture_check": "采集 ROI 样本",
}

#: 承载上述入口的三行布局容器（隐藏它们 = 隐藏整行，不留空白）
INTERNAL_ROWS = ("_diag_row", "_diag_obs_row", "_diag_capture_row")


def row_of(window, attr: str):
    """行容器没有单独存属性，按控件的父控件取回那一行。"""
    control = getattr(window, {"_diag_row": "_diag_btn",
                               "_diag_obs_row": "_diag_obs_check",
                               "_diag_capture_row": "_diag_capture_check"}[attr])
    return control.parentWidget()


def top_in(panel, widget) -> int:
    """widget 相对于 panel 的顶边 y 坐标。"""
    return widget.mapTo(panel, QPoint(0, 0)).y()


def gap_above(panel, target) -> int:
    """target 与它上方最近一个**可见**直接子控件之间的垂直空隙（像素）。

    行控件若仍占位，这个空隙就会多出整行的身高；不留空白则应当只剩布局
    spacing。这是对"没有空白带 / 没有不可见占位控件"的直接测量。
    """
    top = top_in(panel, target)
    bottoms = []
    for child in panel.findChildren(
            QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly):
        if child is target or not child.isVisible():
            continue
        bottom = top_in(panel, child) + child.height()
        if bottom <= top:
            bottoms.append(bottom)
    return -1 if not bottoms else top - max(bottoms)


# ======================================================================
section("前置：开关本身")
# ======================================================================
check(hasattr(mw, "SHOW_INTERNAL_DIAGNOSTICS"),
      "存在单一开关 gui.main_window.SHOW_INTERNAL_DIAGNOSTICS")
check(mw.SHOW_INTERNAL_DIAGNOSTICS is False,
      f"开关默认 False（实际 {mw.SHOW_INTERNAL_DIAGNOSTICS!r}）")
check(isinstance(mw.SHOW_INTERNAL_DIAGNOSTICS, bool), "开关是布尔值，不是字符串/数字")

window_off = build_window(False)
window_on = build_window(True)

# 开关必须是在构造时读取的：改回默认值后新建的窗口应当回到隐藏
window_default = build_window(mw.SHOW_INTERNAL_DIAGNOSTICS)
check(not window_default._diag_btn.isVisible(),
      "开关值在构造时被读取（改回 False 后新建窗口即隐藏）")


# ======================================================================
section("A. 默认（开关 False）：入口不可见，但实现完整保留")
# ======================================================================
for attr, text in INTERNAL_ENTRIES.items():
    control = getattr(window_off, attr, None)
    check(control is not None,
          f"{attr} 控件对象仍然存在（隐藏，不是删除）")
    if control is None:
        continue
    check(not control.isVisible(), f"「{text}」默认不可见")
    check(control.text() == text,
          f"「{text}」文案原样保留（实际 {control.text()!r}）")
    check(window_off._ocr_panel.isAncestorOf(control),
          f"「{text}」仍挂在 OCR 面板内（调用能力未变）")

# 行容器本身也必须隐藏，否则会留下一条空白带
for attr in INTERNAL_ROWS:
    row = row_of(window_off, attr)
    check(row is not None and not row.isVisible(),
          f"{attr} 行容器被隐藏（不留空白行）")

# 实现对象照旧存在且在同一个面板里
check(window_off._diagnostics is not None, "OCRDiagnostics 实例照旧创建")
check(window_off._sample_capture is not None, "SampleCapture 实例照旧创建")
check(window_off._diagnostics.enabled is False, "诊断默认仍是关闭状态")
check(window_off._diag_capture_check.isChecked() is False,
      "「采集 ROI 样本」默认仍未勾选（隐藏不改默认值）")
check(getattr(window_off, "_on_diag_toggled", None) is not None
      and getattr(window_off, "_on_export_diagnostics_clicked", None) is not None
      and getattr(window_off, "_on_sample_capture_toggled", None) is not None,
      "三个回调方法仍在（实现未删）")


# ======================================================================
section("B. 关闭后不留空白：几何断言")
# ======================================================================
spacing = window_off._ocr_panel.layout().spacing()
y_off = top_in(window_off._ocr_panel, window_off._session_list)
y_on = top_in(window_on._ocr_panel, window_on._session_list)
print(f"  （会话列表顶边：开关关闭 {y_off}px，开关开启 {y_on}px，"
      f"列间距 {spacing}px）")

check(y_off < y_on,
      f"关闭后下游控件上移，说明三行确实让出了高度（{y_off} < {y_on}）")

gap = gap_above(window_off._ocr_panel, window_off._session_list)
print(f"  （会话列表与上方最近可见控件的空隙：{gap}px）")
check(0 <= gap <= spacing + 2,
      f"关闭后会话列表上方只剩布局间距，没有空白带（空隙 {gap}px ≤ {spacing + 2}px）")

# 反向对照：让出的高度必须恰好等于三行本身的身高 + 它们各自占的行间距。
# 这正是「关闭后不留空白」的精确表述：少掉的只有那三行，不多也不少。
rows_h = sum(row_of(window_on, attr).height() for attr in INTERNAL_ROWS)
spacings = 3 * spacing          # 三行各占一条行间距
expected_shift = rows_h + spacings
actual_shift = y_on - y_off
print(f"  （三行身高合计 {rows_h}px + 3×{spacing}px 间距 = {expected_shift}px，"
      f"实际让出 {actual_shift}px）")
check(abs(actual_shift - expected_shift) <= 2,
      f"让出的高度恰为三行本身（{actual_shift}px ≈ {expected_shift}px）"
      f"—— 没有多留空白，也没有隐藏占位")

# 不可见但仍占位的控件：隐藏行的实际高度不计入布局
for attr in INTERNAL_ROWS:
    row = row_of(window_off, attr)
    check(not row.isVisible(),
          f"{attr} 不是「不可见但仍占位」（由上面的空隙断言共同保证）")


# ======================================================================
section("C. 开关 True：四个入口完整恢复")
# ======================================================================
for attr, text in INTERNAL_ENTRIES.items():
    control = getattr(window_on, attr)
    check(control.isVisible(), f"「{text}」开关开启后可见")
    check(control.text() == text, f"「{text}」文案不变（实际 {control.text()!r}）")

for attr in INTERNAL_ROWS:
    check(row_of(window_on, attr).isVisible(), f"{attr} 行容器可见")

# 恢复后仍可交互：诊断开关能点、勾选状态能改
window_on._diag_btn.click()
pump(10)
check(window_on._diagnostics.enabled is True,
      "开关开启后「开始诊断」仍可点击并生效")
window_on._diag_obs_check.setChecked(True)
pump(10)
check(window_on._diag_obs_check.isChecked() is True,
      "开关开启后「全 PSM 观测」仍可勾选")

# 与隐藏状态下的行为完全一致：同样能开能关
window_off._diag_btn.click()
pump(10)
check(window_off._diagnostics.enabled is True,
      "隐藏状态下回调仍可调用（调用能力保留，不只是「看得见」）")
window_off._diag_btn.click()
pump(10)
check(window_off._diagnostics.enabled is False, "隐藏状态下可再次关闭诊断")
check(window_off._diag_obs_check.isChecked() is False,
      "关闭诊断时勾选仍被同步清除（原逻辑未变）")

# 初始 enabled 状态：隐藏不影响「诊断未开启时观测不可勾选」这条原逻辑
check(window_off._diag_obs_check.isEnabled() is False,
      "隐藏状态下「诊断未开启 -> 观测不可勾选」的原逻辑仍然成立")


# ======================================================================
section("D. 正式产品入口未被误隐藏")
# ======================================================================
PRODUCT_ENTRIES = {
    "_export_csv_btn": "导出 CSV（本次会话房间码流水）",
    "_ocr_btn": "开始OCR",
    "_roi_btn": "选择识别区域",
    "_roi_help_btn": "识别区域说明",
    "_connect_btn": "连接",
    "_help_btn": "使用引导",
    "_license_btn": "激活许可证",
}
for attr, text in PRODUCT_ENTRIES.items():
    control = getattr(window_off, attr, None)
    check(control is not None, f"{text} 控件存在")
    if control is None:
        continue
    check(control.isVisible(), f"{text} 默认仍然可见（未被误隐藏）")

check(window_off._session_list.isVisible(), "本次会话列表仍然可见")
check(window_off._ocr_game_combo.isVisible(), "游戏选择仍然可见")

# 两个导出必须区分清楚：隐藏的是诊断导出，房间码导出照旧
check("诊断" in window_off._diag_export_btn.text()
      and "诊断" not in window_off._export_csv_btn.text(),
      "「导出 CSV」与「导出诊断 CSV」仍是两个不同的入口")
check(not window_off._diag_export_btn.isVisible()
      and window_off._export_csv_btn.isVisible(),
      "关闭的只有诊断导出，房间码导出不受影响")

# 房间码流水仍然真实工作（正式功能没有被开关波及）
before = window_off._copy_log.count
window_off._copy_log.record("HJKT6402")
check(window_off._copy_log.count == before + 1,
      "房间码记录流水照常写入（正式产品功能未受影响）")


# ======================================================================
section("E. 隐藏不改行为：_start_ocr 读到的观测开关")
# ======================================================================
# _start_ocr 会读 _diag_obs_check.isChecked() 作为 observe_all_psm。
# 控件必须继续存在，否则那条路径会 AttributeError。这里只验证读取路径可走通，
# 不真的启动识别线程（不起线程、不加载 Tesseract）。
check(getattr(window_off, "_diag_obs_check", None) is not None,
      "_diag_obs_check 在隐藏后仍然存在（_start_ocr 读取它不会 AttributeError）")
check(window_off._diag_obs_check.isChecked() is False,
      "隐藏后读取到 False —— observe_all_psm 仍为默认关闭")

import inspect  # noqa: E402

src = inspect.getsource(mw.MainWindow._start_ocr)
check("_diag_obs_check.isChecked()" in src,
      "生产代码确实在 _start_ocr 里读取该控件（因此它必须保留）")
check("observe_all_psm" in src, "observe_all_psm 参数照旧传递（行为未变）")


# ======================================================================
section("F. 开关只影响本窗口，不残留全局状态")
# ======================================================================
check(mw.SHOW_INTERNAL_DIAGNOSTICS is False,
      f"两个窗口构造后开关值已复原，未污染全局（实际 {mw.SHOW_INTERNAL_DIAGNOSTICS!r}）")

# 默认开启开关的话，不应影响任何非 UI 模块：OCR / 抓流 / 授权都不读它
for module_name in ("ocr.diagnostics", "ocr.sample_capture", "ocr.worker",
                    "ocr.room_code", "stream.adapters.douyin"):
    module = __import__(module_name, fromlist=["_"])
    check(not hasattr(module, "SHOW_INTERNAL_DIAGNOSTICS"),
          f"{module_name} 不读取这个 UI 开关（只关入口，不动功能）")

for window in (window_off, window_on, window_default):
    window.close()
pump(10)

print(f"\n{'=' * 74}")
print(f"通过 {_PASSED} 项，失败 {_FAILED} 项")
print("=" * 74)
sys.exit(1 if _FAILED else 0)
