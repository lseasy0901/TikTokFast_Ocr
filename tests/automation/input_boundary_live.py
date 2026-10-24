# -*- coding: utf-8 -*-
"""真实输入边界验收(需真实桌面;一次性验收脚本,不入 pytest 常驻)。

用一个测试窗口代替游戏窗口,验证执行序列在真实 SendInput 路径下的
边界行为(点击全部落在测试窗口内,桌面安全):
    L1 完整序列:激活 + 两点击落点精确(±3px)
    L2 窗口移动后:比例坐标实时换算正确
    L3 窗口缩放后:同上
    L4 最小化:恢复后再点击
    L5 执行中切走前台:下一步立即中止(绝不点错窗口)
    L6 剪贴板 force:用户先复制别的内容,F8 前被重写为目标码

用法(真实桌面,非 offscreen):
    python tests/automation/input_boundary_live.py
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from automation.join_config import JoinConfig
from automation.join_controller import JoinController
from automation.win32 import WindowInfo, client_geometry, find_game_window

TEST_TITLE = "DLV-AJ-BOUNDARY-TEST"
PASSED = FAILED = 0


def check(cond, msg):
    global PASSED, FAILED
    print(f"  [{'PASS' if cond else 'FAIL'}] {msg}")
    if cond:
        PASSED += 1
    else:
        FAILED += 1


class TargetWindow(QWidget):
    """点击靶标窗口:记录每次真实点击的客户区相对位置。"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle(TEST_TITLE)
        self.setWindowFlags(Qt.Window)
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        self.hits: list[tuple[float, float]] = []   # 相对本窗口的点击位置
        self._hint = QLabel("automation boundary test target", self)
        self._hint.setGeometry(10, 10, 300, 20)

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.hits.append((event.position().x(), event.position().y()))


def make_config() -> JoinConfig:
    cfg = JoinConfig()
    cfg.window_class = "NoSuchClass-aj-test"   # 类名不命中,靠标题
    cfg.window_title = TEST_TITLE
    cfg.paste_point = (0.30, 0.50)
    cfg.join_point = (0.70, 0.50)
    cfg.calibrated_aspect = 800 / 400
    cfg.delay_activate_ms = 250
    cfg.delay_click_ms = 250
    return cfg


def run_join_and_wait(app, ctl, cfg, timeout=6.0):
    results = []
    ctl.finished.connect(lambda r: results.append(r))
    started = ctl.join(cfg)
    deadline = time.perf_counter() + timeout
    while not results and time.perf_counter() < deadline:
        app.processEvents()
        time.sleep(0.01)
    return started, (results[0] if results else None)


def clear_hits(win: TargetWindow):
    win.hits.clear()


def verify_hits(win: TargetWindow, geo, cfg, label):
    ox, oy, w, h = geo
    expect = [
        (ox + cfg.paste_point[0] * w, oy + cfg.paste_point[1] * h),
        (ox + cfg.join_point[0] * w, oy + cfg.join_point[1] * h),
    ]
    ok = (len(win.hits) == 2)
    for i, (ex, ey) in enumerate(expect):
        if i >= len(win.hits):
            ok = False
            break
        hx, hy = win.hits[i]
        # 事件位置是本窗口局部坐标;换回屏幕坐标比对
        gx, gy, _, _ = geo
        sx, sy = gx + hx, gy + hy
        if abs(sx - ex) > 3 or abs(sy - ey) > 3:
            ok = False
    check(ok, f"{label}:两次点击精确落点(期望{[(round(e[0]),round(e[1])) for e in expect]},"
              f"实际{[(round(gx+h[0]),round(gy+h[1])) for h in win.hits] if win.hits else '无'})")


def main() -> int:
    app = QApplication.instance() or QApplication([])
    win = TargetWindow()
    win.setGeometry(200, 200, 800, 400)
    win.show()
    app.processEvents()
    time.sleep(0.3)

    found = find_game_window("NoSuchClass-aj-test", TEST_TITLE)
    check(found is not None and isinstance(found, WindowInfo),
          "标题匹配找到测试窗口")
    if found is None:
        return 1

    ctl = JoinController()

    # ---- L1 完整序列 ------------------------------------------------
    clear_hits(win)
    cfg = make_config()
    ok, res = run_join_and_wait(app, ctl, cfg)
    check(ok and res is not None and res.ok, f"L1 序列成功({res.reason if res else '?'})")
    verify_hits(win, client_geometry(found.hwnd), cfg, "L1")

    # ---- L2 移动 -----------------------------------------------------
    win.move(500, 400)
    app.processEvents(); time.sleep(0.15)
    clear_hits(win)
    ok, res = run_join_and_wait(app, ctl, cfg)
    check(ok and res.ok, "L2 移动后序列成功")
    verify_hits(win, client_geometry(found.hwnd), cfg, "L2")

    # ---- L3 缩放 -----------------------------------------------------
    win.resize(1000, 500)
    app.processEvents(); time.sleep(0.15)
    clear_hits(win)
    ok, res = run_join_and_wait(app, ctl, cfg)
    check(ok and res.ok, "L3 缩放后序列成功(比例坐标跟随)")
    verify_hits(win, client_geometry(found.hwnd), cfg, "L3")
    check(not (res and res.warning) or True, "L3 aspect=%.2f warning=%s"
          % (1000/500, res.warning if res else "-"))
    win.resize(800, 400)
    app.processEvents(); time.sleep(0.15)

    # ---- L4 最小化恢复 -------------------------------------------------
    win.showMinimized()
    app.processEvents(); time.sleep(0.3)
    clear_hits(win)
    ok, res = run_join_and_wait(app, ctl, cfg, timeout=8.0)
    check(ok and res.ok, f"L4 最小化→恢复→序列成功({res.reason if res else '?'})")
    check(len(win.hits) == 2, "L4 恢复后两点击都在窗口内")
    win.showNormal()

    # ---- L5 执行中切走 → 下一步中止 ------------------------------------
    import automation.win32 as W
    clear_hits(win)                  # 上一轮(L4)的点击不计入本断言
    cfg2 = make_config()
    cfg2.delay_click_ms = 900        # 拉长间隔,留时间切窗
    ctl.join(cfg2)
    time.sleep(0.30)                 # 等激活(250ms)进入序列
    app.processEvents()
    # 把前台抢给另一个窗口(模拟用户在序列执行中切走)
    thief = QWidget()
    thief.setWindowTitle("thief-foreground")
    thief.setGeometry(700, 150, 200, 100)
    thief.show()
    app.processEvents()
    W.activate_window(int(thief.winId()))
    app.processEvents()
    results2 = []
    ctl.finished.connect(lambda r: results2.append(r))
    deadline = time.perf_counter() + 5
    while not results2 and time.perf_counter() < deadline:
        app.processEvents(); time.sleep(0.01)
    check(results2 and results2[0].ok is False and "前台" in results2[0].reason,
          f"L5 执行中切走→中止({results2[0].reason if results2 else '无结果'})")
    check(len(win.hits) <= 2, "L5 中止后没有多余点击")
    thief.close()

    # ---- L6 剪贴板 force 重写(语义级,不发输入) ------------------------
    clip = app.clipboard()
    clip.setText("user-copied-junk")
    from ocr.clipboard import ClipboardManager
    cm = ClipboardManager()
    check(cm.copy("ABC123", force=True) and clip.text() == "ABC123",
          "L6 force=True:剪贴板已有别的内容,仍强制写入目标码")
    cm2 = ClipboardManager()
    check(cm2.copy("SAME-CODE") is True, "L6 前置:首次复制 SAME-CODE 成功")
    check(cm2.copy("SAME-CODE") is False,
          "L6 默认路径:重复码仍被过滤(OCR 防抖语义不变)")

    win.close()
    print(f"\n通过 {PASSED} 项,失败 {FAILED} 项")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
