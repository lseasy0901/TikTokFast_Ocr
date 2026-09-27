#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
UI-3.1 主界面布局截图脚本

把正式工作的真实渲染结果抓成 PNG，用于人工核对布局（对齐、留白、控件高度、
字体层级、面板收起行为）。**不启动直播、不联网、不写用户配置** —— 全部走
offscreen 平台 + 临时数据目录。

用法：
    python capture_ui_layout.py [输出目录]        # 默认 ui_screenshots/

说明：``QWidget.grab()`` 渲染的是控件树本身，因此
    - 能看到：QSS、排版、间距、控件尺寸、文字层级、面板收起状态
    - 看不到：Mica / Acrylic 系统背景（那由 DWM 在真实桌面上合成）
Mica 与真实鼠标交互属于人工验证项，见模块末尾的说明。
"""

import os
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

# 必须在导入 utils.license_store / gui.main_window 之前设置
os.environ["APPDATA"] = tempfile.mkdtemp(prefix="dlv-ui31-appdata-")
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="dlv-ui31-local-")

# 默认 offscreen（无头、可重复、不打断桌面）。加 --native 走真实 Windows 平台：
# 才能拿到真正的中文字体（offscreen 下 CJK 会渲染成豆腐块），也才看得到 DWM
# 合成的 Mica —— 但会在桌面上短暂弹出一个真实窗口。
if "--native" not in sys.argv:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# --dpi 1.5 → 模拟 150% 缩放：尺寸按逻辑像素给，抓出来的位图是 1.5 倍，
# 用来核对布局在高 DPI 下有没有被挤坏（而不是像素级清晰度）。
if "--dpi" in sys.argv:
    _i = sys.argv.index("--dpi")
    _factor = sys.argv[_i + 1] if _i + 1 < len(sys.argv) else "1"
    os.environ["QT_SCALE_FACTOR"] = _factor
    os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "1"

from PySide6.QtWidgets import QApplication  # noqa: E402

from gui import theme  # noqa: E402
from gui.theme import Metrics  # noqa: E402
import gui.main_window as mw  # noqa: E402

#: 目标窗口尺寸：(文件名, 宽, 高)
SIZES = (
    ("1400x860_normal", 1400, 860),
    ("1100x660_min", 1100, 660),
    ("880x660_narrow", 880, 660),
)

DEFAULT_OUT = os.path.join(PROJECT_ROOT, "ui_screenshots")


def pump(app, rounds: int = 40) -> None:
    import time
    for _ in range(rounds):
        app.processEvents()
        time.sleep(0.005)


def capture_guide_spotlight(app, window, out_dir: str, suffix: str):
    """抓聚光灯的前两步，并在真实平台上复核"遮罩不拦截鼠标"。"""
    from PySide6.QtCore import QPoint, Qt
    from gui.onboarding import GUIDE_ADVANCE, GUIDE_NEVER
    from utils.first_run import FirstRunStore

    window.showNormal()
    window._on_expand_clicked()
    window.resize(1400, 860)
    pump(app, 30)

    # 让引导处于"待弹出"状态，再走真实的触发入口
    import tempfile
    window._first_run_store = FirstRunStore(data_dir=tempfile.mkdtemp())
    window._first_run_guide_done = False
    window.maybe_show_first_run_guide()
    pump(app, 30)

    spotlight = window._guide_spotlight
    if spotlight is None:
        print("  [WARN] 聚光灯未启动，跳过该截图")
        return None

    def probe(widget):
        return app.widgetAt(widget.mapToGlobal(
            QPoint(widget.width() // 2, widget.height() // 2)))

    url_hit = probe(window._url_input)
    connect_hit = probe(window._connect_btn)
    print(f"  聚光灯命中测试：输入框={type(url_hit).__name__} "
          f"连接按钮={'自身' if connect_hit is window._connect_btn else '未命中'}")
    print(f"  高亮孔={spotlight.mask._holes}")

    path = os.path.join(out_dir, f"main_1400x860_spotlight_step1{suffix}.png")
    window.grab().save(path)
    print(f"  1400x860 聚光灯第一步 -> {path}")

    # 第二步「选择游戏」：高亮右侧 OCR 面板里的真实游戏下拉框
    spotlight.notify_completed()          # 连接完成 → 自动进入第二步
    pump(app, 40)
    step2 = window._guide_spotlight
    if step2 is not None:
        print(f"  第二步高亮孔={step2.mask._holes}（游戏下拉框）")
        path2 = None
        path2 = os.path.join(
            out_dir, f"main_1400x860_spotlight_step2{suffix}.png")
        window.grab().save(path2)
        print(f"  1400x860 聚光灯第二步 -> {path2}")

    # 第四步「开始自动识别」：高亮右侧面板里的真实 OCR 按钮
    window._guide_spotlight.notify_completed()        # 第二步 → 第三步
    pump(app, 40)
    window._guide_spotlight.notify_completed()        # 第三步 → 第四步
    pump(app, 40)
    step4 = window._guide_spotlight
    if step4 is not None:
        print(f"  第四步高亮孔={step4.mask._holes}（开始 OCR 按钮）")
        path4 = os.path.join(
            out_dir, f"main_1400x860_spotlight_step4{suffix}.png")
        window.grab().save(path4)
        print(f"  1400x860 聚光灯第四步 -> {path4}")

    # 收尾：关掉聚光灯，别把状态带进后面的截图
    window._guide_spotlight.finish(GUIDE_NEVER)
    pump(app, 20)
    return [p for p in (path, path2 if step2 is not None else None) if p]


def capture_step3_and_roi(app, window, out_dir: str, suffix: str):
    """抓第三步的两种状态：初始聚光灯、以及框选松开鼠标后的轻量提示。

    第二次必须在**真实桌面**上抓：框选窗口是一个独立的最大化窗口，
    提示条又是另一个顶层窗口，只有抓屏幕才能同时拍到它们。
    """
    import numpy as np
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from gui.onboarding import GUIDE_ADVANCE, GUIDE_NEVER
    from utils.first_run import FirstRunStore

    window.showNormal()
    window._on_expand_clicked()
    window.resize(1400, 860)
    pump(app, 30)

    # 走到第三步：两步 notify_completed
    window._first_run_store = FirstRunStore(data_dir=tempfile.mkdtemp())
    window._first_run_guide_done = False
    window.maybe_show_first_run_guide()
    pump(app, 30)
    if window._guide_spotlight is None:
        print("  [WARN] 聚光灯未启动，跳过第三步截图")
        return []
    window._guide_spotlight.notify_completed()
    pump(app, 30)
    window._guide_spotlight.notify_completed()
    pump(app, 40)

    spotlight = window._guide_spotlight
    if spotlight is None or spotlight.step_index != 2:
        print("  [WARN] 未进入第三步，跳过")
        return []
    print(f"  第三步高亮孔={spotlight.mask._holes}（选择识别区域按钮）")

    written = []
    path = os.path.join(out_dir, f"main_1400x860_spotlight_step3{suffix}.png")
    window.grab().save(path)
    written.append(path)
    print(f"  1400x860 聚光灯第三步 -> {path}")

    # 打开真实框选窗口并模拟一次拖拽，再抓屏幕
    class _Buf:
        frame_id = 0
        def get(self):
            # 造一帧"画面"，让框选窗口有内容可画
            frame = np.zeros((720, 1280, 3), dtype=np.uint8)
            frame[:, :] = (28, 30, 34)
            frame[300:420, 500:780] = (60, 64, 72)
            return frame
        def clear(self):
            pass

    window._frame_buffer = _Buf()
    window._connected = True
    window._roi_btn.setEnabled(True)
    window._roi_btn.click()
    pump(app, 40)

    selector = window._roi_selector
    if selector is None:
        print("  [WARN] 框选窗口未打开，跳过提示条截图")
        return written

    def _ev(kind, x, y, buttons=Qt.LeftButton):
        return QMouseEvent(kind, QPointF(x, y), QPointF(x, y),
                           buttons, Qt.LeftButton, Qt.NoModifier)

    selector.mousePressEvent(_ev(QMouseEvent.Type.MouseButtonPress, 420, 300))
    selector.mouseMoveEvent(_ev(QMouseEvent.Type.MouseMove, 820, 430))
    selector.mouseReleaseEvent(_ev(QMouseEvent.Type.MouseButtonRelease, 820, 430))
    pump(app, 40)

    hint = spotlight._hint
    print(f"  提示条可见={hint.isVisible()} 位置={hint.geometry()}")

    # 抓屏幕：框选窗口与提示条都是顶层窗口，主窗口的 grab() 拍不到
    screen = app.primaryScreen()
    shot = screen.grabWindow(0)
    # 只保留"选区 + 提示条"这一段，避免把整屏其他内容也存下来
    top = max(0, min(300, hint.geometry().top()) - 40)
    bottom = max(430, hint.geometry().bottom()) + 40
    band = shot.copy(0, top, shot.width(), min(bottom, shot.height()) - top)
    path2 = os.path.join(out_dir, f"main_1400x860_roi_selected{suffix}.png")
    band.save(path2)
    written.append(path2)
    print(f"  1400x860 框选松开后 -> {path2}")

    # 收尾
    selector.close()
    pump(app, 20)
    window._frame_buffer = None
    if window._guide_spotlight is not None:
        window._guide_spotlight.finish(GUIDE_NEVER)
    pump(app, 20)
    return written


def capture_review_mode(app, window, out_dir: str, suffix: str):
    """抓回顾模式（标题栏 ?）的第 1 步与第 4 步。"""
    from gui.onboarding import GUIDE_REVIEW_CLOSED
    from utils.first_run import FirstRunStore

    window.showNormal()
    window._on_expand_clicked()
    window.resize(1400, 860)
    pump(app, 30)

    window._first_run_store = FirstRunStore(data_dir=tempfile.mkdtemp())
    window._first_run_guide_done = False
    window._on_help_clicked()          # 等价于点标题栏的 ?
    pump(app, 30)
    spotlight = window._guide_spotlight
    if spotlight is None or not spotlight.review_mode:
        print("  [WARN] 回顾模式未启动，跳过截图")
        return []

    written = []
    print(f"  回顾第1步 高亮孔={spotlight.mask._holes} 挡板={len(spotlight._shields)}")
    path = os.path.join(out_dir, f"main_1400x860_review_step1{suffix}.png")
    window.grab().save(path)
    written.append(path)
    print(f"  1400x860 回顾第 1 步 -> {path}")

    for _ in range(3):
        window._guide_spotlight.card.next_btn.click()
        pump(app, 30)
    step4 = window._guide_spotlight
    if step4 is not None:
        print(f"  回顾第4步 高亮孔={step4.mask._holes}")
        path4 = os.path.join(out_dir, f"main_1400x860_review_step4{suffix}.png")
        window.grab().save(path4)
        written.append(path4)
        print(f"  1400x860 回顾第 4 步 -> {path4}")
        step4.finish(GUIDE_REVIEW_CLOSED)
    pump(app, 20)
    return written


def capture_contact_popup(app, window, out_dir: str, suffix: str):
    """抓标题栏「联系作者」按钮与展开后的联系方式卡片。

    刻意**不用**整屏抓图：那会把桌面上其它窗口一并拍进来（既不相关，
    也侵犯隐私）。这里分别渲染主窗口与卡片，再按实际相对位置合成 ——
    两者都是我们自己的控件，画面既完整又只含本程序。
    """
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QPainter, QPixmap

    window.showNormal()
    window._on_expand_clicked()
    window.move(60, 60)
    window.resize(1400, 860)
    pump(app, 40)

    window._contact_btn.click()
    pump(app, 40)
    popup = window._contact_popup
    if popup is None or not popup.isVisible():
        print("  [WARN] 联系作者卡片未弹出，跳过截图")
        return []
    print(f"  标题栏按钮={[b._kind for b in window._title_bar._actions.findChildren(object) if hasattr(b, '_kind')]}")
    print(f"  卡片位置={popup.geometry()} 尺寸={popup.size().toTuple()}")

    win_pix = window.grab()
    card_pix = popup.grab()
    offset = popup.mapToGlobal(QPoint(0, 0)) - window.mapToGlobal(QPoint(0, 0))

    canvas = QPixmap(win_pix.size())
    canvas.fill(Qt.transparent)
    painter = QPainter(canvas)
    painter.drawPixmap(0, 0, win_pix)
    painter.drawPixmap(offset, card_pix)
    painter.end()

    # 只留标题栏那一段（含卡片），避免整张 1400×860 全是空的预览区
    bottom = min(canvas.height(), offset.y() + card_pix.height() + 24)
    band = canvas.copy(0, 0, canvas.width(), max(1, bottom))
    path = os.path.join(out_dir, f"titlebar_contact_popup{suffix}.png")
    band.save(path)
    print(f"  标题栏 + 联系作者卡片 -> {path}")

    popup.close()
    pump(app, 20)
    return [path]


def capture_unactivated_contact(app, window, out_dir: str, suffix: str):
    """抓未激活界面：遮罩里的两个按钮 + 展开的联系方式卡片。"""
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QPainter, QPixmap

    window.showNormal()
    window.move(60, 60)
    window.resize(1400, 860)
    pump(app, 30)
    # 明确置为未激活
    window._access_status.set_license_inactive("未激活")
    pump(app, 40)

    overlay = window._activation_overlay
    if not overlay.isVisible():
        print("  [WARN] 未激活遮罩不可见，跳过截图")
        return []
    overlay._contact_btn.click()
    pump(app, 40)
    popup = window._contact_popup
    if popup is None or not popup.isVisible():
        print("  [WARN] 联系作者卡片未弹出，跳过截图")
        return []
    print(f"  遮罩按钮={[(b.text()) for b in (overlay._activate_btn, overlay._contact_btn)]}")
    print(f"  卡片位置={popup.geometry()}")

    win_pix = window.grab()
    card_pix = popup.grab()
    offset = popup.mapToGlobal(QPoint(0, 0)) - window.mapToGlobal(QPoint(0, 0))

    canvas = QPixmap(win_pix.size())
    canvas.fill(Qt.transparent)
    painter = QPainter(canvas)
    painter.drawPixmap(0, 0, win_pix)
    painter.drawPixmap(offset, card_pix)
    painter.end()

    bottom = min(canvas.height(), offset.y() + card_pix.height() + 40)
    band = canvas.copy(0, 0, canvas.width(), max(1, bottom))
    path = os.path.join(out_dir, f"unactivated_contact_popup{suffix}.png")
    band.save(path)
    print(f"  未激活 + 联系作者卡片 -> {path}")

    popup.close()
    pump(app, 20)
    return [path]


def main() -> int:
    positional = [a for a in sys.argv[1:]
                  if not a.startswith("--") and not a.replace(".", "").isdigit()]
    out_dir = positional[0] if positional else DEFAULT_OUT
    scale = float(os.environ.get("QT_SCALE_FACTOR", "1") or 1)
    suffix = "" if scale == 1 else f"_dpi{int(scale * 100)}"
    os.makedirs(out_dir, exist_ok=True)

    app = QApplication.instance() or QApplication([])
    theme.apply_theme(app)

    window = mw.MainWindow()
    window.show()
    pump(app, 80)

    # 视为已激活：遮罩收起，看到的是真实工作台（未激活态另有截图）
    window._access_status.set_license_active(None)
    pump(app, 30)

    written = []
    for name, width, height in SIZES:
        window.showNormal()
        # 面板展开时窗口被 minimumWidth 卡在 1100，直接 resize(880) 会被夹回去。
        # 现实里的顺序是：用户先收起面板（窗口最小宽度随之放宽到 880），
        # 再把窗口拉窄 —— 这里照同样的顺序来，抓到的才是真实状态。
        if width < Metrics.WINDOW_MIN_WIDTH:
            window._on_collapse_clicked()
        else:
            window._on_expand_clicked()
        pump(app, 30)
        window.resize(width, height)
        pump(app, 40)
        path = os.path.join(out_dir, f"main_{name}{suffix}.png")
        window.grab().save(path)
        written.append(path)
        print(f"  {width}x{height} -> {path}")

    # 1400×860 收起 OCR 面板：核对顶部操作区是否同步适配（不留固定宽度空白）
    window.showNormal()
    window._on_expand_clicked()
    pump(app, 20)
    window.resize(1400, 860)
    pump(app, 30)
    window._on_collapse_clicked()
    pump(app, 40)
    path = os.path.join(out_dir, f"main_1400x860_panel_collapsed{suffix}.png")
    window.grab().save(path)
    written.append(path)
    print(f"  1400x860 面板收起 -> {path}")

    # UI-4.1 第一步聚光灯：必须在真实平台上抓 —— 它靠"遮罩不拦截鼠标"工作，
    # 而 widgetAt 在 offscreen 下对地址栏按钮不可靠，只有真实窗口系统能证明。
    written.extend(capture_guide_spotlight(app, window, out_dir, suffix))
    written.extend(capture_step3_and_roi(app, window, out_dir, suffix))
    written.extend(capture_review_mode(app, window, out_dir, suffix))
    written.extend(capture_contact_popup(app, window, out_dir, suffix))
    written.extend(capture_unactivated_contact(app, window, out_dir, suffix))

    # 未激活态（呼吸遮罩 + 激活入口）单独一张，便于核对遮罩排版
    window._on_expand_clicked()
    window.resize(1400, 860)
    window._access_status.set_license_inactive("未激活")
    pump(app, 40)
    path = os.path.join(out_dir, f"main_1400x860_unactivated{suffix}.png")
    window.grab().save(path)
    written.append(path)
    print(f"  未激活态 -> {path}")

    window.close()
    print(f"\n共 {len(written)} 张，输出目录：{out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
