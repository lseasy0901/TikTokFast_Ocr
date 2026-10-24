#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
UI 微调任务 02 —— 顶部操作区与 OCR 面板列宽对齐 验证脚本

锁死的不变量：

    A. 顶部右侧操作区的左右边缘 == 下方 OCR 面板的左右边缘
    B. 该宽度**来自** QSplitter 的实时几何，不是另立的固定常量
    C. 窗口缩放 / 最大化 / 还原 / 拖分隔条 / 收起展开后仍然对齐
    D. 分隔线对齐：上下两行的"列间距"落在同一段像素上
    E. 面板收起时不留固定宽度空白，也不重叠；输入框吃满剩余空间
    F. 历史下拉弹性占满剩余空间，连接按钮宽度不被压缩

约定与仓库其它 test_*.py 一致：扁平脚本、check()/section() 自行统计，
直接 ``python test_ui_tune_02_address_alignment.py`` 运行，不依赖 pytest。
临时数据目录隔离，不联网、不写用户配置。
"""

import os
import sys
import tempfile
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

os.environ["APPDATA"] = tempfile.mkdtemp(prefix="dlv-tune02-appdata-")
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="dlv-tune02-local-")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QSizePolicy  # noqa: E402

from gui import theme  # noqa: E402
from gui.theme import Metrics  # noqa: E402
import gui.main_window as mw  # noqa: E402

app = QApplication.instance() or QApplication([])
theme.apply_theme(app)

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


def pump(rounds: int = 40, delay: float = 0.005) -> None:
    for _ in range(rounds):
        app.processEvents()
        time.sleep(delay)


class _StubActivationDialog:
    """替身：模态弹窗在无头环境会永久阻塞。"""

    def __init__(self, manager, parent=None):
        pass

    def exec(self):
        return 0

    def result_data(self):
        return None


def build_window(width: int = 1400, height: int = 860):
    real = mw.ActivationDialog
    mw.ActivationDialog = _StubActivationDialog
    try:
        window = mw.MainWindow()
        window.show()
    finally:
        mw.ActivationDialog = real
    pump(50)
    window._access_status.set_license_active(None)
    window.resize(width, height)
    pump(30)
    return window


def span(widget, ref) -> tuple[int, int]:
    """控件在 ref 坐标系里的 [左, 右) 像素区间。"""
    rect = widget.geometry()
    top_left = widget.mapTo(ref, rect.topLeft() - rect.topLeft())
    return top_left.x(), top_left.x() + widget.width()


def action_span(window) -> tuple[int, int]:
    """顶部右侧操作区：从历史下拉左边缘到连接按钮右边缘。

    左边缘应与 OCR 面板的**外**左缘重合（这样上下分割线才落在同一段像素），
    右边缘与面板**内容**的右缘重合（面板贴到窗口边，按钮不能也贴死）。
    """
    return (span(window._recent_combo, window)[0],
            span(window._connect_btn, window)[1])


def panel_span(window) -> tuple[int, int]:
    """操作区应当占据的区间：面板外左缘 → 面板内容右缘。"""
    margins = window._ocr_panel.layout().contentsMargins()
    left, right = span(window._ocr_panel, window)
    return left, right - margins.right()


def report_alignment(window, tag: str) -> bool:
    return action_span(window) == panel_span(window)


def main() -> int:
    window = build_window()

    # ------------------------------------------------------------------
    section("A. 1400×860 / 1100×660 顶底对齐")
    # ------------------------------------------------------------------
    for width, height in ((1400, 860), (1100, 660)):
        window.showNormal()
        window.resize(width, height)
        pump(40)
        actions = action_span(window)
        panel = panel_span(window)
        check(actions == panel,
              f"{width}×{height} 操作区{actions} == 面板{panel}")
        check(window._ocr_panel.isVisible(), f"{width}×{height} 面板处于展开态")

    # ------------------------------------------------------------------
    section("B. 宽度取自 QSplitter 实时几何（不是固定常量）")
    # ------------------------------------------------------------------
    window.resize(1400, 860)
    pump(30)
    check(window._recent_combo.width()
          == window._ocr_panel.width() - mw._ADDR_ACTIONS_CHROME,
          "下拉宽度 = 面板实际宽度 - 操作区固定开销（不是写死的 310 常量）")

    # 直接把分隔条拖到非默认位置：顶部必须跟着变，而不是停在 310
    window._body_splitter.setSizes([700, 400])
    pump(40)
    check(window._recent_combo.width() == 400 - mw._ADDR_ACTIONS_CHROME,
          f"分隔条拖到 400 后下拉跟着变宽（实际 {window._recent_combo.width()}）")
    check(report_alignment(window, "拖分隔条"), "拖分隔条后左右边缘仍对齐")

    window._body_splitter.setSizes([1000, 300])
    pump(40)
    check(window._recent_combo.width() == 300 - mw._ADDR_ACTIONS_CHROME,
          f"分隔条拖回 300 后下拉跟着变窄（实际 {window._recent_combo.width()}）")
    check(report_alignment(window, "拖分隔条"), "再次拖动后仍对齐")

    # ------------------------------------------------------------------
    section("C. 缩放 / 最大化 / 还原")
    # ------------------------------------------------------------------
    for width, height in ((1600, 900), (1200, 700), (1400, 860)):
        window.showNormal()
        window.resize(width, height)
        pump(40)
        check(report_alignment(window, "resize"), f"{width}×{height} 缩放后对齐")

    window.showMaximized()
    pump(40)
    # 注意：offscreen 平台的虚拟屏只有 880 宽，最大化后窗口反而低于
    # AUTO_COLLAPSE_WIDTH，面板会被自动收起 —— 那是无头环境的属性，不是
    # 产品行为。这里按"面板是否可见"分别断言，两种情况都必须自洽。
    if window._ocr_panel.isVisible():
        check(report_alignment(window, "maximized"), "最大化后对齐")
    else:
        check(window.width() < mw.AUTO_COLLAPSE_WIDTH,
              "最大化后屏幕过窄触发自动收起（无头环境）")
        check(window._recent_combo.width() == Metrics.OCR_PANEL_WIDTH
              - mw._ADDR_ACTIONS_CHROME,
              "该情形下操作区退回设计宽度，没有留固定宽度空白")
    window.showNormal()
    window.resize(1400, 860)
    pump(40)
    check(report_alignment(window, "restored"), "还原后对齐")

    # ------------------------------------------------------------------
    section("D. 上下两行的列间距落在同一段像素上")
    # ------------------------------------------------------------------
    top_gap = (span(window._url_input, window)[1],
               span(window._recent_combo, window)[0])
    bottom_gap = (span(window._ocr_panel, window)[0] - Metrics.SPLITTER_HANDLE,
                  span(window._ocr_panel, window)[0])
    check(top_gap == bottom_gap,
          f"上方列间距{top_gap} == 下方分隔条{bottom_gap}")
    check(window._address_bar.layout().spacing() == Metrics.SPLITTER_HANDLE,
          "地址栏列间距复用了 SPLITTER_HANDLE 常量")
    check(window._url_input.x() == 12,
          f"地址输入框左边缘与视频区左边缘同为 12（实际 {window._url_input.x()}）")

    # ------------------------------------------------------------------
    section("E. 面板收起时的适配")
    # ------------------------------------------------------------------
    window._on_collapse_clicked()
    pump(40)
    check(not window._ocr_panel.isVisible(), "面板已收起")
    check(window._connect_btn.width() == 96,
          "收起态：连接按钮仍是固定 96 宽，没有被压扁")
    check(window._recent_combo.width() == Metrics.OCR_PANEL_WIDTH
          - mw._ADDR_ACTIONS_CHROME,
          f"收起态：操作区退回设计宽度（下拉 {window._recent_combo.width()}）")
    check(window._url_input.width() > window.width() // 2,
          f"输入框吃满剩余空间（宽 {window._url_input.width()}）")

    # 无重叠：操作区内控件两两不重叠
    children = [window._recent_combo, window._history_menu_btn,
                window._connect_btn]
    ordered = sorted(children, key=lambda c: span(c, window)[0])
    gaps_ok = all(span(ordered[i], window)[1] <= span(ordered[i + 1], window)[0]
                  for i in range(len(ordered) - 1))
    check(gaps_ok, "收起态：控件之间没有重叠")

    window._on_expand_clicked()
    pump(40)
    check(report_alignment(window, "re-expanded"), "重新展开后恢复对齐")

    # ------------------------------------------------------------------
    section("F. 弹性与固定宽度")
    # ------------------------------------------------------------------
    check(window._recent_combo.sizePolicy().horizontalPolicy()
          == QSizePolicy.Expanding,
          "历史记录按钮是 Expanding（占据剩余空间）")
    check("历史记录" in window._recent_combo.placeholderText()
          or window._recent_combo.placeholderText() == "历史记录",
          f"历史记录入口文案：{window._recent_combo.placeholderText()!r}")

    connect_widths = []
    for width in (1100, 1400, 1600):
        window.showNormal()
        window.resize(width, 860)
        pump(30)
        connect_widths.append(window._connect_btn.width())
    check(set(connect_widths) == {96},
          f"连接按钮在 1100/1400/1600 下宽度都是 96（实际 {connect_widths}）")

    # 面板越宽，下拉越宽 —— 证明它确实在吃掉剩余空间
    window._body_splitter.setSizes([1000, 400])
    pump(40)
    wide_combo = window._recent_combo.width()
    window._body_splitter.setSizes([1100, 300])
    pump(40)
    narrow_combo = window._recent_combo.width()
    check(wide_combo > narrow_combo,
          f"面板更宽时下拉更宽（{wide_combo} > {narrow_combo}）")

    window.close()
    pump(10)

    print(f"\n{'=' * 52}")
    print(f"通过 {_PASSED} 项，失败 {_FAILED} 项")
    print("=" * 52)
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
