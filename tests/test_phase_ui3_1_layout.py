#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Phase UI-3.1 —— 主界面布局与交互修正 验证脚本

覆盖：
    A. 标题栏：图标操作区、窗口按钮语义、全屏与最大化互不取代
    B. 地址栏：**只有一个**直播地址显示区；历史二级入口；备注/清除可达
    C. OCR 面板：会话历史的写入时机（只在复制成功后）、时间戳、去重、弹性布局
    D. 窄窗口自动收起与用户主动选择的关系
    E. 全屏时 FramelessController 不再与顶部热区争抢缩放
    F. 性能栏：四组指标、组间分隔、授权状态独立

约定与仓库其它 test_*.py 一致：扁平脚本、check()/section() 自行统计，
直接 ``python test_phase_ui3_1_layout.py`` 运行，不依赖 pytest。

关键隔离：APPDATA / LOCALAPPDATA 指向临时目录；不联网、不启动直播、
不写用户真实配置。模态对话框一律打桩（无头环境里 exec() 会永久阻塞）。
"""

import os
import re
import sys
import tempfile
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

os.environ["APPDATA"] = tempfile.mkdtemp(prefix="dlv-ui31-appdata-")
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="dlv-ui31-local-")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QFrame,
    QLabel,
)

from gui import theme  # noqa: E402
import gui.main_window as mw  # noqa: E402
from gui.theme import Metrics  # noqa: E402
from gui.widgets import IconButton, WindowButton  # noqa: E402

app = QApplication.instance() or QApplication([])
theme.apply_theme(app)

# ======================================================================
# 测试脚手架
# ======================================================================
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
    """替身：记录被打开，exec() 不阻塞（模态在无头环境会永久挂起）。"""

    instances = []

    def __init__(self, manager, parent=None):
        self.manager = manager
        _StubActivationDialog.instances.append(self)

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
    pump(60)
    window.resize(width, height)
    pump(30)
    # 视为已激活：排除未激活遮罩对布局与点击的干扰
    window._access_status.set_license_active(None)
    pump(30)
    return window


# ======================================================================
# A. 标题栏
# ======================================================================
def test_title_bar() -> None:
    section("A. 标题栏")
    window = build_window()
    bar = window._title_bar

    # A1. 操作区是可识别、带 tooltip 的紧凑图标按钮
    # 玻璃 2.0 改版：常驻 [授权][设置]，低频入口（引导/全屏/联系作者）
    # 收进「⋯」溢出菜单 —— 按钮断言只覆盖常驻三项。
    icons = {
        "授权": window._license_btn,
        "设置": window._settings_btn,
        "更多": window._more_btn,
    }
    for name, btn in icons.items():
        check(isinstance(btn, IconButton), f"{name}是图标按钮（不再是文字按钮）")
        check(bool(btn.toolTip()), f"{name}有 tooltip：{btn.toolTip()[:18]!r}")
        check(
            btn.width() == Metrics.ICON_BUTTON_SIZE
            and btn.height() == Metrics.ICON_BUTTON_SIZE,
            f"{name}是 {Metrics.ICON_BUTTON_SIZE}×{Metrics.ICON_BUTTON_SIZE} 紧凑尺寸",
        )

    # A1b. 溢出菜单包含全部低频入口
    menu_texts = [a.text() for a in window._more_menu.actions()]
    for entry in ("全屏工作台", "使用引导", "联系作者"):
        check(entry in menu_texts, f"{entry}在「⋯」溢出菜单中：{menu_texts}")

    # A2. 图标各不相同（四个入口不能长成一个样）
    kinds = {b._kind for b in icons.values()}
    check(len(kinds) == 4, f"四个入口使用四种不同图标：{sorted(kinds)}")

    # A3. 「授权」仍是唯一被强调的入口
    check(window._license_btn.property("variant") == "primary",
          "授权按钮是唯一 primary")
    check(all(b.property("variant") != "primary"
              for k, b in icons.items() if k != "授权"),
          "其余图标按钮都不是 primary")

    # A4. 关键回归：最大化按钮必须做普通最大化，不能被全屏取代
    check(bar.max_button.toolTip() == "最大化",
          "窗口按钮中的最大化是普通最大化")
    check(bar.max_button._kind == WindowButton.MAX, "最大化按钮字形为 MAX")

    window.showMaximized()
    pump(30)
    check(window.isMaximized(), "最大化生效")
    check(bar.max_button._kind == WindowButton.RESTORE,
          "最大化后按钮字形切到还原")
    check(not window.isFullScreen(), "最大化不会变成全屏")

    window.showNormal()
    pump(30)
    check(bar.max_button._kind == WindowButton.MAX, "还原后字形回到最大化")

    window.close()
    pump(10)

    # A5. 最小化 / 关闭仍然接线 —— 直接点按钮看行为，而不是查信号表
    extra = build_window()
    extra._title_bar.min_button.click()
    pump(30)
    check(extra.isMinimized(), "点最小化按钮 → 窗口最小化")
    extra.showNormal()
    pump(20)
    extra._title_bar.close_button.click()
    pump(30)
    check(not extra.isVisible(), "点关闭按钮 → 窗口关闭")


# ======================================================================
# B. 地址栏
# ======================================================================
def test_address_bar() -> None:
    section("B. 顶部地址栏")
    window = build_window()

    # B1. 地址的唯一事实来源是输入框;历史走「历史按钮 + 卡片弹窗」,
    #     卡片只展示备注名/房间号/最近使用,**不展示 URL**(Phase UI-3.1
    #     重构后的形态;旧版"历史下拉显示 URL"已不存在)。
    check(not hasattr(window, "_recent_combo"),
          "旧版历史下拉已不存在(地址不会同时出现在两处)")
    check(window._url_input.placeholderText().find("live.douyin.com") >= 0,
          "地址输入框以占位文案指明用途")
    check(bool(window._history_btn.toolTip()),
          "历史入口有 tooltip 说明")

    # B2. 历史服务读写闭环(数据层;卡片渲染见 history_panel 冒烟)。
    #     现行接口是 record_success(成功进入房间才入史),数据层保留 URL。
    #     备注回调置空防弹窗:首次记录会触发备注询问模态框,offscreen
    #     下无人可关,测试会挂死(2026-10-25 实测)。
    window._history.set_remark_callback(None)
    window._history.clear()
    window._history.record_success("https://live.douyin.com/111", "111")
    window._history.record_success("https://live.douyin.com/222", "222")
    items = window._history.items()
    check(len(items) == 2 and all(getattr(it, "url", None) for it in items),
          f"历史服务收录 2 条且数据层保留 URL(仅 UI 不展示):{len(items)} 条")

    # B3. 历史卡片 UI 不显示 URL:渲染代码里 URL 只出现在注释/服务调用中
    import inspect
    from gui.history_panel import HistoryPopup
    src = inspect.getsource(HistoryPopup)
    code_lines = [l for l in src.splitlines()
                  if l.strip() and not l.strip().startswith("#")]
    shown_url = any("url" in l and ".get(" not in l and "service" not in l
                    and "item[" not in l for l in code_lines)
    check(not shown_url, "历史弹窗不以 URL 作为展示文案")

    # B4. 连接主操作固定在地址栏右端且为 primary
    check(window._connect_btn.text() in ("连接", "断开"),
          "连接/断开按钮存在")
    check(window._connect_btn.property("variant") == "primary",
          "连接按钮是 primary 强调样式")


def test_ocr_panel_history() -> None:
    section("C. OCR 面板与本次会话历史")
    window = build_window()

    # C1. 空状态：明确说明而不是一片空白
    first = window._session_list.item(0)
    check(first is not None and not first.flags(),
          "历史为空时有不可点击的空状态说明")
    check(first is not None and "复制成功" in first.text(),
          f"空状态文案说明记录口径：{first.text() if first else ''!r}")

    # C2. 关键回归：OCR 文本更新【不】写入历史
    window._on_ocr_text_updated("888888")
    pump(10)
    check(window._session_list.count() == 1 and not window._session_list.item(0).flags(),
          "OCR 读到文本不会被误记为「已复制成功」")
    check(window._lbl_ocr_text.text() == "888888",
          "当前房间码仍实时显示最新读数")

    # C3. 复制成功才写入，且带时间
    window._add_session_code("888888")
    pump(10)
    check(window._session_list.count() == 1, "复制成功后占位项被替换为真实记录")
    text = window._session_list.item(0).text()
    check(text.startswith("888888"), f"记录以房间码开头：{text!r}")
    check(re.search(r"\d{2}:\d{2}:\d{2}", text) is not None,
          "记录带复制时刻 HH:MM:SS")

    # C4. 同一房间号重复复制不堆行，只刷新时间
    window._add_session_code("888888")
    pump(10)
    check(window._session_list.count() == 1, "同一房间号重复复制不新增行")

    window._add_session_code("777777")
    pump(10)
    check(window._session_list.count() == 2, "新的房间号新增一行")
    check(window._session_list.item(0).text().startswith("777777"),
          "最新记录在最上面")

    # C5. 面板顺序：房间码 → 状态 → 历史 → 游戏 → ROI → 启停按钮
    panel = window._ocr_panel
    order = []
    lay = panel.layout()
    for i in range(lay.count()):
        w = lay.itemAt(i).widget()
        if w is None:
            continue
        if w is window._lbl_ocr_text:
            order.append("房间码")
        elif w is window._session_list:
            order.append("历史")
        elif w is window._ocr_game_combo:
            order.append("游戏")
        elif w is window._lbl_roi:
            order.append("ROI")
        elif w is window._ocr_btn:
            order.append("启停")
    check(order == ["房间码", "历史", "游戏", "ROI", "启停"],
          f"面板自上而下顺序正确：{order}")

    # C6. 弹性空间给历史，启停按钮仍贴在面板底部
    panel_h = panel.height()
    check(window._session_list.height() > 120,
          f"历史区吃掉剩余高度（{window._session_list.height()}px），中段没有空白带")
    check(window._ocr_btn.y() + window._ocr_btn.height() >= panel_h - 16,
          "OCR 启停按钮仍固定在面板底部")

    # C7. 未识别时是明确的占位文案，不是一个像结果的符号
    window._set_result_text("")
    check(window._lbl_ocr_text.text() == mw.OCR_RESULT_PLACEHOLDER,
          f"未识别占位文案为 {mw.OCR_RESULT_PLACEHOLDER!r}")
    check(not isinstance(window._lbl_ocr_text.text(), int),
          "占位文案是文字而不是 --")

    # C8. ROI 提示只剩一条最短键序（详细说明在帮助浮层，避免三处重复）
    check(len(window._roi_note.text()) < 30,
          f"ROI 行内提示已精简：{window._roi_note.text()!r}")
    check("Enter" in window._roi_note.text() and "Esc" in window._roi_note.text(),
          "ROI 提示仍写明 Enter 确认 / Esc 取消")

    window.close()
    pump(10)


# ======================================================================
# D. 窄窗口自动收起
# ======================================================================
def test_narrow_auto_collapse() -> None:
    section("D. 窄窗口自动收起")
    window = build_window(1400, 860)

    check(window._ocr_panel.isVisible(), "宽窗口下面板默认展开")

    # 1100×660 是约定要保留的「面板展开的最小尺寸」：阈值不能大于它，
    # 否则用户在这个尺寸就会看到面板被自动收掉。
    check(mw.AUTO_COLLAPSE_WIDTH <= Metrics.WINDOW_MIN_WIDTH,
          f"自动收起阈值 {mw.AUTO_COLLAPSE_WIDTH} 不高于展开态最小宽度 "
          f"{Metrics.WINDOW_MIN_WIDTH}")
    window.resize(Metrics.WINDOW_MIN_WIDTH, 660)
    pump(30)
    check(window._ocr_panel.isVisible(),
          f"窗口 {Metrics.WINDOW_MIN_WIDTH}×660 时面板仍是展开的")

    # 面板展开时窗口被 minimumWidth 卡在 1100，为了让自动收起这条兜底
    # 可被观察到，这里显式放宽下限再缩窄。
    window.setMinimumWidth(0)
    window.resize(1000, 700)
    pump(30)
    check(not window._ocr_panel.isVisible(), "窄于阈值时面板自动收起")
    check(window._panel_auto_hidden, "记下这是自动收起（而非用户选择）")
    check(window._expand_btn.isVisible(), "收起后右边缘出现展开入口")

    window.resize(1400, 860)
    pump(30)
    check(window._ocr_panel.isVisible(), "窗口放宽后面板自动恢复")

    # D2. 用户主动收起优先，不被自动逻辑顶回来
    window._on_collapse_clicked()
    pump(20)
    check(not window._ocr_panel.isVisible(), "用户点收起后面板隐藏")
    check(window._panel_user_choice, "记下这是用户的选择")

    window.resize(1000, 700)
    pump(20)
    window.resize(1400, 860)
    pump(30)
    check(not window._ocr_panel.isVisible(), "窗口宽窄变化不会顶回用户的收起")

    # D3. 用户主动展开后同样被尊重
    window._on_expand_clicked()
    pump(20)
    check(window._ocr_panel.isVisible(), "用户点展开后面板显示")
    check(not window._panel_auto_hidden, "用户展开后清除自动收起标记")

    # D4. 最小宽度随面板可见性联动：收起后窗口才能继续压窄，视频优先
    window._on_collapse_clicked()
    pump(20)
    check(window.minimumWidth() == Metrics.WINDOW_MIN_WIDTH_NARROW,
          f"面板收起后最小宽度放宽到 {Metrics.WINDOW_MIN_WIDTH_NARROW}")

    window._on_expand_clicked()
    pump(20)
    check(window.minimumWidth() == Metrics.WINDOW_MIN_WIDTH,
          f"面板展开后最小宽度回到 {Metrics.WINDOW_MIN_WIDTH}")

    window.resize(880, 660)
    pump(20)
    check(window.width() >= Metrics.WINDOW_MIN_WIDTH,
          "面板可见时窗口不会被压到最小宽度以下")

    window.close()
    pump(10)


# ======================================================================
# E. 全屏与顶部热区
# ======================================================================
def test_fullscreen_hotzone() -> None:
    section("E. 全屏 / 顶部热区 / 边缘缩放")
    window = build_window(1400, 860)
    controller = window._frameless

    def _move_to(y: int) -> QMouseEvent:
        return QMouseEvent(
            QEvent.Type.MouseMove, QPointF(200.0, float(y)),
            QPointF(200.0, float(y)), Qt.NoButton, Qt.NoButton, Qt.NoModifier)

    def _prime() -> None:
        """回到"光标未知"状态：清掉窗口光标，并同步清掉控制器的缓存。

        控制器只在光标形状**变化**时才设置（`_last_cursor`），这是它的正常
        优化；测试里手动 unsetCursor 却不清缓存，第二次就会读到"没变化"。
        """
        window.unsetCursor()
        controller._last_cursor = Qt.ArrowCursor

    # E1. 非全屏：顶部边缘仍然是缩放热区（原有能力保留）
    _prime()
    controller.eventFilter(window, _move_to(2))
    pump(5)
    check(window.cursor().shape() == Qt.SizeVerCursor,
          "非全屏时窗口上边缘仍显示垂直缩放光标")

    # E2. 全屏：不再是缩放热区，让位给「鼠标到顶滑出标题栏」。
    #     走真实的 set_fullscreen 同步接口，而不是给 isFullScreen 打补丁。
    controller.set_fullscreen(True)
    _prime()
    controller.eventFilter(window, _move_to(2))
    pump(5)
    check(window.cursor().shape() == Qt.ArrowCursor,
          "全屏时上边缘不再抢缩放光标（热区可用）")

    press = QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(200.0, 2.0),
        QPointF(200.0, 2.0), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    check(controller.eventFilter(window, press) is False,
          "全屏时顶部按下不会被当成窗口缩放拖拽")

    # 退出全屏后边缘缩放必须恢复
    controller.set_fullscreen(False)
    _prime()
    controller.eventFilter(window, _move_to(2))
    pump(5)
    check(window.cursor().shape() == Qt.SizeVerCursor,
          "退出全屏后边缘缩放恢复")

    # E3. 热区本身存在且只在全屏启用
    check(window._title_hot is not None, "顶部热区控件存在")
    check(window._title_hot.height() == 4, "热区高度为 4px")
    check(not window._title_hot.isVisible(), "非全屏时热区隐藏")
    check(window._title_hide_timer.interval() == 1000,
          "离开后 1 秒收起标题栏")

    # E4. 全屏时标题栏/地址栏可隐藏再滑出（不经真实鼠标也能验证状态机）
    window.isFullScreen = lambda: True
    try:
        window._show_title_chrome(False)
        check(not window._title_bar.isVisible() and not window._address_bar.isVisible(),
              "全屏时可以隐藏标题栏与地址栏")
        window._show_title_chrome(True)
        check(window._title_bar.isVisible() and window._address_bar.isVisible(),
              "热区触发后标题栏与地址栏一起滑出")
    finally:
        del window.isFullScreen

    window.close()
    pump(10)


# ======================================================================
# F. 性能栏
# ======================================================================
def test_perf_bar() -> None:
    section("F. 底部性能栏")
    window = build_window(1400, 860)

    groups = [w for w in window.statusBar().findChildren(QFrame)
              if w.objectName() == "perfDivider"]
    check(len(groups) == 3, f"四组指标之间有 3 条分隔线（实际 {len(groups)}）")

    # 组标签不再与首个条目重名（原先读作「解码 解码 —」）
    labels = [lbl.text() for lbl in window.statusBar().findChildren(QLabel)
              if lbl.objectName() == "perfGroup"]
    check(labels == ["链路", "解码", "渲染", "OCR"], f"四组标签：{labels}")

    values = {k: lbl.text() for k, lbl in window._perf_values.items()}
    check(values.get("decode_fps", "").startswith("输出"),
          f"解码组的输出帧率不再叫「解码」：{values.get('decode_fps')!r}")
    check(values.get("ocr_cost", "").endswith("—"),
          "没有测量源的指标如实显示 —")

    # 授权状态独立展示，不与技术指标混在同一组
    check(window._access_status_label.objectName() == "accessLabel",
          "授权状态是独立标签")
    check(window._access_status_label not in window._perf_values.values(),
          "授权状态没有混进四组技术指标里")

    # 诊断 / 错误有可读的展示位（状态栏瞬时消息区仍然存在）
    window.statusBar().showMessage("测试诊断信息")
    pump(10)
    check(window.statusBar().currentMessage() == "测试诊断信息",
          "状态栏可以显示诊断与错误消息，不被静默吞掉")

    window.close()
    pump(10)


def main() -> int:
    test_title_bar()
    test_address_bar()
    test_ocr_panel_history()
    test_narrow_auto_collapse()
    test_fullscreen_hotzone()
    test_perf_bar()

    print(f"\n{'=' * 52}")
    print(f"通过 {_PASSED} 项，失败 {_FAILED} 项")
    print("=" * 52)
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
