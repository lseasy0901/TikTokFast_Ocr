#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
UI 微调任务 01 —— ROI 操作入口迁移 验证脚本

迁移内容：把「选择识别区域」和「说明」从视频下方工具栏搬到右侧 OCR 面板的
「识别区域」一节。本脚本锁死迁移的四个不变量：

    A. 视频工具栏里不再有 ROI 入口（只剩直播状态 / 分辨率 / FPS）
    B. 两个入口都在 OCR 面板里，且**各只有一个**（没有重复控件）
    C. 用的还是原来那两个按钮实例，信号没有丢
    D. 1100×660 下两个入口都可见且没有被面板裁掉

约定与仓库其它 test_*.py 一致：扁平脚本、check()/section() 自行统计，
直接 ``python test_ui_tune_01_roi_entry.py`` 运行，不依赖 pytest。
临时数据目录隔离，不联网、不写用户配置。
"""

import os
import sys
import tempfile
import time

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

os.environ["APPDATA"] = tempfile.mkdtemp(prefix="dlv-tune01-appdata-")
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="dlv-tune01-local-")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QLabel,
    QPushButton,
)

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
    window.resize(width, height)
    pump(30)
    window._access_status.set_license_active(None)
    pump(20)
    return window


def visible_inside(child, container) -> bool:
    """子控件可见，且几何完全落在容器内（没被裁掉）。"""
    if not child.isVisible():
        return False
    rect = child.geometry()
    top_left = child.mapTo(container, rect.topLeft() - rect.topLeft())
    return (top_left.y() >= 0
            and top_left.y() + child.height() <= container.height())


def main() -> int:
    window = build_window()

    # ------------------------------------------------------------------
    section("A. 视频工具栏")
    # ------------------------------------------------------------------
    toolbar = window._video_container.parent().layout().itemAt(1).widget()
    check(toolbar.objectName() == "toolStrip", "找到视频下方工具栏")

    toolbar_buttons = toolbar.findChildren(QPushButton)
    check(not toolbar_buttons,
          f"工具栏里没有任何按钮（实际 {[b.text() for b in toolbar_buttons]}）")
    check(toolbar.findChild(type(window._roi_btn)) is None,
          "工具栏里找不到 ROI 按钮实例")

    chips = [c.text() for c in toolbar.findChildren(type(window._lbl_status))]
    check(chips == ["未连接", "分辨率 --", "FPS --"],
          f"工具栏只剩直播状态 / 分辨率 / FPS：{chips}")

    # ------------------------------------------------------------------
    section("B. OCR 面板里的两个入口")
    # ------------------------------------------------------------------
    panel = window._ocr_panel
    check(window._roi_btn.parent() is panel, "「选择识别区域」的父级是 OCR 面板")
    # 「说明」在标题行的容器里，只要该容器本身属于面板即可
    check(panel.isAncestorOf(window._roi_help_btn),
          "「说明」位于 OCR 面板之内")

    # 全窗口范围内不应出现第二个同名按钮
    all_buttons = window.findChildren(QPushButton)
    roi_select = [b for b in all_buttons if b.text() in ("选择识别区域", "重新框选")]
    help_btns = [b for b in all_buttons if b.text() == "说明"]
    check(roi_select == [window._roi_btn],
          f"「选择识别区域」全窗口只有一个实例（实际 {len(roi_select)} 个）")
    check(help_btns == [window._roi_help_btn],
          f"「说明」全窗口只有一个实例（实际 {len(help_btns)} 个）")

    # 「说明」在小节标题右侧：两者垂直方向重叠、且按钮在标题右边
    titles = [lbl for lbl in panel.findChildren(QLabel)
              if lbl.text() == "识别区域"]
    check(len(titles) == 1, "面板里有唯一的「识别区域」小节标题")
    if titles:
        title = titles[0]
        title_left = title.mapTo(panel, title.rect().topLeft()).x()
        help_left = window._roi_help_btn.mapTo(
            panel, window._roi_help_btn.rect().topLeft()).x()
        check(help_left > title_left, "「说明」在「识别区域」标题右侧")
        title_y = title.mapTo(panel, title.rect().topLeft()).y()
        help_y = window._roi_help_btn.mapTo(
            panel, window._roi_help_btn.rect().topLeft()).y()
        check(abs(help_y - title_y) < 20, "「说明」与标题在同一行")

    # 「选择识别区域」在 ROI 状态文字下方
    roi_state_y = window._lbl_roi.mapTo(
        panel, window._lbl_roi.rect().topLeft()).y()
    roi_btn_y = window._roi_btn.mapTo(panel, window._roi_btn.rect().topLeft()).y()
    check(roi_btn_y > roi_state_y, "「选择识别区域」在 ROI 状态文字下方")

    # ------------------------------------------------------------------
    section("C. 信号与行为")
    # ------------------------------------------------------------------
    check(window._roi_btn.isEnabled() is False,
          "未连接时「选择识别区域」仍按原逻辑禁用")
    check(window._roi_help_btn.isEnabled() is True,
          "「说明」随时可用（与连接状态无关）")

    # 信号仍然接着原来的处理器（trigger 会开模态弹窗，因此只查连接数）
    check(window._roi_btn.receivers("2clicked()") > 0,
          "「选择识别区域」仍接着 _on_roi_select_clicked")
    check(window._roi_help_btn.receivers("2clicked()") > 0,
          "「说明」仍接着 _on_roi_help_clicked")

    check(window._roi_help_btn.toolTip() != "", "「说明」带 tooltip")

    # ROI 行为未变：提示键序、状态联动都在
    check("Enter" in window._roi_note.text() and "Esc" in window._roi_note.text(),
          f"ROI 键序提示保留：{window._roi_note.text()!r}")
    window._lbl_roi.setText("未选择")
    window._roi_btn.setText("选择识别区域")
    window._on_access_status_changed()
    check(window._roi_btn.text() == "选择识别区域", "访问状态变化不改坏按钮文案")

    # ------------------------------------------------------------------
    section("D. 1100×660 下两个入口都可见")
    # ------------------------------------------------------------------
    window.resize(Metrics.WINDOW_MIN_WIDTH, 660)
    pump(40)
    check(window._ocr_panel.isVisible(),
          f"窗口 {Metrics.WINDOW_MIN_WIDTH}×660 时面板处于展开态")
    check(visible_inside(window._roi_help_btn, window._ocr_panel),
          "「说明」可见且未被面板裁剪")
    check(visible_inside(window._roi_btn, window._ocr_panel),
          "「选择识别区域」可见且未被面板裁剪")
    check(window._roi_btn.height() >= 28,
          f"「选择识别区域」仍是可点击高度（{window._roi_btn.height()}px）")
    check(window._roi_help_btn.height() >= 18,
          f"「说明」有可点中的高度（{window._roi_help_btn.height()}px）")
    check(visible_inside(window._ocr_btn, window._ocr_panel),
          "面板底部的 OCR 启停按钮没有被挤出去")

    window.close()
    pump(10)

    print(f"\n{'=' * 52}")
    print(f"通过 {_PASSED} 项，失败 {_FAILED} 项")
    print("=" * 52)
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
