#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Phase UI 液态玻璃改造 —— 顶部工具栏契约回归（Phase 4）。

覆盖：
    A. 标题栏结构：5 个图标按钮 + 分组分隔线，尺寸/提示/可访问名/焦点策略
    B. 信号接线保全：原 15 个交互项一个不少、连接数不为 0
    C. 齿轮图标几何：确为"带中心孔的齿轮"，不是旧的辐条星芒
    D. 死代码清理：_build_status_bar 已删除
    E. 行为回归：全屏切换、设置入口、帮助入口真实可用

约定与仓库其它 test_*.py 一致：扁平脚本、check()/section() 自行统计，
直接 ``python tests/ui_redesign/test_toolbar_contract.py`` 运行，不依赖 pytest。

关键隔离：APPDATA / LOCALAPPDATA 指向临时目录；不联网、不启动直播、
不写用户真实配置。模态对话框一律打桩（无头环境里 exec() 会永久阻塞）。
"""

from __future__ import annotations

import os
import sys
import tempfile

# --- 隔离用户目录（必须在导入项目模块之前） --------------------------------
_TEMP = tempfile.mkdtemp(prefix="uilg_toolbar_")
os.environ["APPDATA"] = os.path.join(_TEMP, "AppData", "Roaming")
os.environ["LOCALAPPDATA"] = os.path.join(_TEMP, "AppData", "Local")
os.environ["USERPROFILE"] = _TEMP
for _p in (os.environ["APPDATA"], os.environ["LOCALAPPDATA"]):
    os.makedirs(_p, exist_ok=True)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from gui import theme  # noqa: E402
from gui.theme import Metrics, Palette  # noqa: E402
from gui.widgets import IconButton, icon_pixmap  # noqa: E402

_FAILED = 0


def check(cond: bool, msg: str) -> None:
    global _FAILED
    if cond:
        print(f"  [ OK ] {msg}")
    else:
        _FAILED += 1
        print(f"  [FAIL] {msg}")


def section(title: str) -> None:
    print()
    print("=" * 68)
    print(title)
    print("=" * 68)


def pump(n: int = 20) -> None:
    app = QApplication.instance()
    for _ in range(n):
        app.processEvents()


def build_window():
    """构造主窗口，并把会阻塞的模态对话框打桩。"""
    import gui.main_window as mw

    class _StubDialog:
        def __init__(self, *a, **kw):
            self._shown = False

        def exec(self):
            return 0

        def show(self):
            self._shown = True

        def close(self):
            pass

    mw.SettingsDialog = _StubDialog      # type: ignore[assignment]
    mw.ActivationDialog = _StubDialog    # type: ignore[assignment]
    mw.ExpiredDialog = _StubDialog       # type: ignore[assignment]
    window = mw.MainWindow()
    window.maybe_show_first_run_guide = lambda *a, **kw: None
    return window


# ======================================================================
# A. 标题栏结构
# ======================================================================
def test_structure(window) -> None:
    section("A. 标题栏结构")

    # 玻璃 2.0 改版：低频入口（引导/联系作者/全屏）收进「⋯」溢出菜单，
    # 常驻图标按钮只有 授权 / 设置 / 更多 三个。
    buttons = {
        "激活许可证": window._license_btn,
        "设置": window._settings_btn,
        "更多(溢出)": window._more_btn,
    }

    for name, btn in buttons.items():
        check(isinstance(btn, IconButton), f"{name} 是自绘图标按钮")
        check(btn.objectName() == "titleIconBtn",
              f"{name} 落在标题栏操作区（objectName=titleIconBtn）")
        check(
            btn.width() == Metrics.ICON_BUTTON_SIZE
            and btn.height() == Metrics.ICON_BUTTON_SIZE,
            f"{name} 尺寸 {Metrics.ICON_BUTTON_SIZE}×{Metrics.ICON_BUTTON_SIZE}",
        )
        check(bool(btn.toolTip()), f"{name} 有 tooltip")
        check(bool(btn.accessibleName()),
              f"{name} 有可访问名称（屏幕阅读器可读）：{btn.accessibleName()!r}")
        check(btn.focusPolicy() == Qt.TabFocus,
              f"{name} 键盘可达（TabFocus，原为 NoFocus）")

    # 唯一强调入口
    check(window._license_btn.property("variant") == "primary",
          "激活许可证 是唯一 primary 入口")
    check(all(b.property("variant") != "primary"
              for k, b in buttons.items() if k != "激活许可证"),
          "其余图标按钮都不是 primary")

    # 三个常驻入口图标各不相同
    kinds = {window._license_btn._kind, window._settings_btn._kind,
             window._more_btn._kind}
    check(len(kinds) == 3, f"三个常驻入口使用三种不同图标：{sorted(kinds)}")

    # 溢出菜单：低频入口一个不少
    menu_texts = [a.text() for a in window._more_menu.actions()]
    for entry in ("全屏工作台", "使用引导", "联系作者"):
        check(entry in menu_texts, f"{entry} 在「⋯」溢出菜单中：{menu_texts}")

    # 分组顺序 + 分隔线
    layout = window._title_bar._actions_layout
    order = []
    for i in range(layout.count()):
        w = layout.itemAt(i).widget()
        order.append(w.objectName() if w is not None else "?")
    check(order == ["titleIconBtn"] * 2 + ["titleBarSeparator"] + ["titleIconBtn"],
          f"操作区分组顺序 = [激活][设置] │ [⋯]，实测 {order}")

    seps = [layout.itemAt(i).widget() for i in range(layout.count())
            if layout.itemAt(i).widget() is not None
            and layout.itemAt(i).widget().objectName() == "titleBarSeparator"]
    check(len(seps) == 1, "恰好一条分组分隔线")


# ======================================================================
# B. 信号接线保全（15 项）
# ======================================================================
def test_wiring(window) -> None:
    section("B. 信号接线保全（原有交互项一个不少）")

    clickable = {
        "T2 激活许可证": window._license_btn,
        "T4 设置": window._settings_btn,
        "T0 溢出菜单": window._more_btn,
        "A1 历史": window._history_btn,
        "A3 连接/断开": window._connect_btn,
        "P1 收起面板": getattr(window, "_collapse_btn", None),
        "P2 展开面板": window._expand_btn,
    }
    for name, btn in clickable.items():
        if btn is None:
            check(False, f"{name} 控件不存在")
            continue
        n = btn.receivers("2clicked()")
        check(n >= 1, f"{name} 仍有 clicked 连接（receivers={n}）")

    # 低频入口改为菜单动作：triggered 连接一个不少
    menu_actions = {a.text(): a for a in window._more_menu.actions()}
    for name, action in menu_actions.items():
        if not name:
            continue  # 分隔线
        n = action.receivers("2triggered(bool)")
        check(n >= 1, f"菜单项「{name}」仍有 triggered 连接（receivers={n}）")

    for name, act in (("A2a 编辑备注…", window._edit_note_action),
                      ("A2b 清除历史记录…", window._clear_recent_action)):
        n = act.receivers("2triggered()")
        check(n >= 1, f"{name} 仍有 triggered 连接（receivers={n}）")

    check(window._history_menu_btn.menu() is not None,
          "A2 ⋯ 溢出菜单仍挂着 QMenu")
    check(len(window._history_menu_btn.menu().actions()) >= 2,
          "⋯ 菜单仍含备注 / 清除两项")

    check(window._url_input.placeholderText() != "", "A4 地址输入框仍有占位提示")
    check(window._url_input.receivers("2returnPressed()") >= 1,
          "A4 地址输入框 Enter 仍触发连接")

    # 窗口控制三件套
    bar = window._title_bar
    check(bar.min_button.receivers("2clicked()") >= 1, "W1 最小化仍接线")
    check(bar.max_button.receivers("2clicked()") >= 1, "W2 最大化仍接线")
    check(bar.close_button.receivers("2clicked()") >= 1, "W3 关闭仍接线")


# ======================================================================
# C. 齿轮图标几何
# ======================================================================
def _ink_at(img, cx: float, cy: float, angle_deg: float, radius: float,
            radial_tol: float = 1.1, ang_tol_deg: float = 3.0) -> bool:
    """在「指定角度 + 指定半径附近」的极坐标小邻域里是否存在不透明像素。

    刻意不用方形窗口：方窗口的对角线半径比边长还大，会把远处轮廓（相邻齿的
    上升沿、齿根圆弧）误判成"这个半径上有墨"。极坐标采样才对应齿轮的几何。
    """
    import math
    steps_r = int(radial_tol / 0.4)
    steps_a = int(ang_tol_deg)
    for i in range(-steps_r, steps_r + 1):
        r = radius + i * 0.4
        if r <= 0:
            continue
        for j in range(-steps_a, steps_a + 1):
            a = math.radians(angle_deg + j)
            x = int(round(cx + math.cos(a) * r))
            y = int(round(cy + math.sin(a) * r))
            if 0 <= x < img.width() and 0 <= y < img.height():
                if img.pixelColor(x, y).alpha() > 40:
                    return True
    return False


def test_gear_geometry() -> None:
    section("C. 齿轮图标几何（真·齿轮，不是辐条星芒）")

    SIZE = 64
    pix = icon_pixmap(IconButton.SETTINGS, SIZE, color="#FFFFFF")
    img = pix.toImage()
    c = SIZE / 2.0
    s = SIZE * (6.0 / 28.0)
    r_out = s * 0.96
    r_root = s * 0.70
    r_hub = s * 0.30

    # 齿中心在 0°（base = i*45°），齿隙中心在 22.5°
    TOOTH = 0.0
    GAP = 22.5

    # 中心孔：圆心处应当没有墨
    check(not _ink_at(img, c, c, TOOTH, 0.0, radial_tol=0.9, ang_tol_deg=1),
          "中心是孔（圆心无墨）")

    # 中心孔外沿：hub 圆上有墨
    check(_ink_at(img, c, c, TOOTH, r_hub),
          "中心孔有闭合轮廓（hub 圆）")

    # 齿顶：齿中心方向在 r_out 处有轮廓
    check(_ink_at(img, c, c, TOOTH, r_out),
          f"齿顶在 {TOOTH}° 有轮廓（齿存在，r_out={r_out:.1f}）")

    # 齿隙：齿隙方向在 r_out 处**没有**墨 → 说明有齿隙，不是纯圆
    check(not _ink_at(img, c, c, GAP, r_out),
          f"齿隙在 {GAP}° 无轮廓（存在齿隙 → 是齿轮不是圆环）")

    # 齿根：齿隙方向在 r_root 处有轮廓（齿根圆弧）
    check(_ink_at(img, c, c, GAP, r_root),
          f"齿根在 {GAP}° 有轮廓（r_root={r_root:.1f}）")

    # 与旧画法的关键差异：旧画法在 r≈s*0.52 处有一整圈齿圈，
    # 且 8 根辐条会穿过该半径。新画法在该半径处（齿隙方向上）应当没有墨。
    r_old_ring = s * 0.52
    check(not _ink_at(img, c, c, GAP, r_old_ring, radial_tol=0.8),
          f"旧齿圈半径处无墨（r={r_old_ring:.1f} → 已不是辐条+齿圈画法）")
    check(not _ink_at(img, c, c, TOOTH, r_old_ring, radial_tol=0.8),
          "齿中心方向上旧齿圈半径处也无墨（辐条已不存在）")

    # 图标之间必须可区分
    rendered = {k: icon_pixmap(k, 32, color="#FFFFFF").toImage()
                for k in (IconButton.HELP, IconButton.LICENSE, IconButton.CONTACT,
                          IconButton.SETTINGS, IconButton.FULLSCREEN)}
    def sig(im):
        return bytes(im.constBits())
    pairs = [(a, b) for i, a in enumerate(rendered) for b in list(rendered)[i + 1:]]
    same = [(a, b) for a, b in pairs if sig(rendered[a]) == sig(rendered[b])]
    check(not same, f"五个图标两两渲染结果不同（重复对={same}）")

    check(Palette.ICON != Palette.TEXT_DIM,
          f"图标默认色已提亮：ICON={Palette.ICON} vs TEXT_DIM={Palette.TEXT_DIM}")


# ======================================================================
# D. 死代码清理
# ======================================================================
def test_dead_code(window) -> None:
    section("D. 死代码清理")
    check(not hasattr(window, "_build_status_bar"),
          "_build_status_bar 已删除（它会覆盖 _help_btn/_license_btn）")
    src = open(os.path.join(_PROJECT_ROOT, "gui", "main_window.py"),
               encoding="utf-8").read()
    check("_build_status_bar" not in src, "源码中已无 _build_status_bar 残留")


# ======================================================================
# E. 行为回归
# ======================================================================
def test_behaviour(window) -> None:
    section("E. 行为回归")

    window.show()
    pump(30)

    # 全屏切换：菜单动作文案必须跟着换（全屏工作台 ↔ 退出全屏工作台）
    action = window._fullscreen_action
    before = action.text()
    window._toggle_fullscreen()
    pump(40)
    check(window.isFullScreen(), "触发全屏动作 → 进入全屏")
    check(action.text() != before,
          f"全屏后动作文案切换：{before!r} → {action.text()!r}")
    window._toggle_fullscreen()
    pump(40)
    check(not window.isFullScreen(), "再触发 → 退出全屏")
    check(action.text() == before, "退出后动作文案切回")

    # 设置入口：打桩的 SettingsDialog 应当被打开
    import gui.main_window as mw
    opened = []
    real_settings = mw.SettingsDialog

    class _Spy:
        def __init__(self, *a, **kw):
            opened.append(kw.get("version"))

        def exec(self):
            return 0

    mw.SettingsDialog = _Spy  # type: ignore[assignment]
    try:
        window._settings_btn.click()
        pump(10)
        check(len(opened) == 1, "点设置按钮 → 打开设置中心")
    finally:
        mw.SettingsDialog = real_settings  # type: ignore[assignment]

    # 帮助入口：引导打桩后应当被调用（_on_help_clicked → _open_onboarding）
    hits = []
    window._open_onboarding = lambda *a, **kw: hits.append(kw.get("first_run", a))
    window._on_help_clicked()
    pump(10)
    check(len(hits) == 1, f"帮助入口仍能打开使用引导（调用={hits}）")
    check(hits == [False], "回顾模式进入引导（first_run=False）")

    # 最大化仍是普通最大化（不能被全屏取代）
    window.showMaximized()
    pump(30)
    check(window.isMaximized() and not window.isFullScreen(),
          "最大化按钮是普通最大化，不会变成全屏")
    window.showNormal()
    pump(20)

    window.close()
    pump(10)


def main() -> int:
    theme.configure_high_dpi()
    app = QApplication.instance() or QApplication(sys.argv)

    window = build_window()
    try:
        test_structure(window)
        test_wiring(window)
        test_gear_geometry()
        test_dead_code(window)
        test_behaviour(window)
    finally:
        try:
            window.close()
        except Exception:  # noqa: BLE001
            pass
        pump(5)

    print()
    print("=" * 68)
    if _FAILED:
        print(f"结果：失败 {_FAILED} 项")
    else:
        print("结果：全部通过")
    print("=" * 68)
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
