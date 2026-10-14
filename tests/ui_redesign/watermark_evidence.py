# -*- coding: utf-8 -*-
"""水印视觉证据：证明"确实在动"且"始终在预览框内"（Phase 5）。

输出：
    1. 三张连续时刻的真实窗口截图（水印位置不同 → 证明在运动）
    2. 一张轨迹图：把 N 步的水印矩形叠画在同一张图上（证明覆盖范围与不越界）

用法：
    <venv-python> tests/ui_redesign/watermark_evidence.py --outdir <dir>
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile

_TEMP = tempfile.mkdtemp(prefix="uilg_wm_ev_")
os.environ["APPDATA"] = os.path.join(_TEMP, "AppData", "Roaming")
os.environ["LOCALAPPDATA"] = os.path.join(_TEMP, "AppData", "Local")
os.environ["USERPROFILE"] = _TEMP
for _p in (os.environ["APPDATA"], os.environ["LOCALAPPDATA"]):
    os.makedirs(_p, exist_ok=True)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# 刻意**不**使用 offscreen 平台：offscreen 下 Qt 不做系统字体回退，
# 中文会渲染成方框（实测），截图就无法作为"真实运行"的证据。
# 本脚本需要真实平台；它会短暂弹出窗口，这是可接受的代价。

from PySide6.QtCore import QRect, Qt  # noqa: E402
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from gui import theme  # noqa: E402
from gui.theme import Palette  # noqa: E402


def pump(app, n=20):
    for _ in range(n):
        app.processEvents()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default="docs/ui-redesign/evidence/screenshots")
    args = ap.parse_args()
    outdir = os.path.abspath(args.outdir)
    os.makedirs(outdir, exist_ok=True)

    theme.configure_high_dpi()
    app = QApplication(sys.argv)

    import gui.main_window as mw

    class _Stub:
        def __init__(self, *a, **kw):
            pass

        def exec(self):
            return 0

        def show(self):
            pass

        def close(self):
            pass

    mw.SettingsDialog = _Stub          # type: ignore[assignment]
    mw.ActivationDialog = _Stub        # type: ignore[assignment]
    mw.ExpiredDialog = _Stub           # type: ignore[assignment]

    window = mw.MainWindow()
    window.maybe_show_first_run_guide = lambda *a, **kw: None
    window.show()
    pump(app, 60)

    wm = window._watermark
    # 用空帧占位：未连接直播，水印在空白预览区上运动（任务书场景之一）
    window._sync_watermark(True)
    pump(app, 40)
    print("watermark visible:", wm.isVisible(), "animating:", wm.is_animating())
    print("bounds:", wm._bounds())

    # ---- 1. 三个时刻的截图 -------------------------------------------
    positions = []
    for i in range(3):
        for _ in range(40):          # 推进一段，确保位置明显不同
            wm.step()
        pump(app, 10)
        r = wm.watermark_rect()
        positions.append((r.x(), r.y()))
        path = os.path.join(outdir, f"phase5_watermark_t{i}.png")
        window.grab().save(path, "PNG")
        print(f"t{i}: rect={r} -> {path}")

    moved = len(set(positions)) == 3
    print("distinct positions:", positions, "moved:", moved)

    # ---- 2. 轨迹图 ----------------------------------------------------
    bounds = wm._bounds()
    w, h = wm.watermark_size()
    W, H = bounds.width(), bounds.height()
    trail = QPixmap(W, H)
    trail.fill(QColor(Palette.PANEL_BG))
    p = QPainter(trail)
    p.setRenderHint(QPainter.Antialiasing, True)

    from PySide6.QtGui import QFont
    cap_font = QFont()
    cap_font.setFamilies(["Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI"])
    cap_font.setPixelSize(13)
    p.setFont(cap_font)

    # 合法范围边框
    p.setPen(QPen(QColor(Palette.ACCENT_EDGE)))
    p.setBrush(Qt.NoBrush)
    p.drawRect(0, 0, W - 1, H - 1)

    # 重放：从左上角出发，用与组件相同的纯函数推进
    from gui.watermark import bounce_step
    x = y = 0.0
    vx, vy = wm.SPEED, wm.SPEED * 0.72
    hits = {"left": 0, "right": 0, "top": 0, "bottom": 0, "corner": 0}
    prev_vx, prev_vy = vx, vy
    STEPS = 4000
    for i in range(STEPS):
        x, y, vx, vy = bounce_step(x, y, vx, vy, w, h, W, H)
        corner_here = (vx != prev_vx and vy != prev_vy)
        if corner_here:
            hits["corner"] += 1
        elif vx != prev_vx:
            hits["left" if vx > 0 else "right"] += 1
        elif vy != prev_vy:
            hits["top" if vy > 0 else "bottom"] += 1
        prev_vx, prev_vy = vx, vy
        if i % 30 == 0:
            p.setPen(QPen(QColor(128, 191, 255, 30)))
            p.setBrush(Qt.NoBrush)
            p.drawRect(int(x), int(y), w, h)

    p.setPen(QPen(QColor(Palette.FOCUS_RING), 2))
    p.setBrush(Qt.NoBrush)
    p.drawRect(int(x), int(y), w, h)

    p.setPen(QPen(QColor(Palette.TEXT)))
    p.drawText(QRect(8, H - 24, W - 16, 20), Qt.AlignLeft | Qt.AlignVCenter,
               f"4000 步轨迹（每 30 步画一次，蓝框=当前位置）｜边界 {W}×{H}｜水印 {w}×{h}"
               f"｜碰撞 左{hits['left']} 右{hits['right']} 上{hits['top']} "
               f"下{hits['bottom']} 角{hits['corner']}")

    # ---- 角落反弹示意（内嵌小图，右下角） ----------------------------
    iw, ih = 300, 190
    ix, iy = W - iw - 16, 16
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(9, 11, 16, 220))
    p.drawRoundedRect(ix - 8, iy - 8, iw + 16, ih + 16, 8, 8)
    p.setPen(QPen(QColor(Palette.ACCENT_EDGE)))
    p.setBrush(Qt.NoBrush)
    p.drawRect(ix, iy, iw, ih)

    # 用等速 + 贴近右下角起点，保证在少量步数内必现角落反弹
    cw, ch = 90, 24
    cx_, cy_ = float(iw - cw - 4), float(ih - ch - 4)
    cvx, cvy = 4.0, 3.0
    cprev = (cvx, cvy)
    ccorner = 0
    for i in range(260):
        cx_, cy_, cvx, cvy = bounce_step(cx_, cy_, cvx, cvy, cw, ch, iw, ih)
        if cvx != cprev[0] and cvy != cprev[1]:
            ccorner += 1
            p.setPen(QPen(QColor(100, 217, 160, 200), 1))
            p.drawRect(ix + int(cx_) - 2, iy + int(cy_) - 2, cw + 4, ch + 4)
        cprev = (cvx, cvy)
        if i % 6 == 0:
            p.setPen(QPen(QColor(128, 191, 255, 60)))
            p.drawRect(ix + int(cx_), iy + int(cy_), cw, ch)

    p.setPen(QPen(QColor(Palette.TEXT)))
    p.drawText(QRect(ix, iy + ih + 2, iw, 18), Qt.AlignLeft | Qt.AlignVCenter,
               f"角落反弹示意：260 步内两轴同时反转 {ccorner} 次（绿框=角落时刻）")

    p.end()

    trail_path = os.path.join(outdir, "phase5_watermark_trail.png")
    trail.save(trail_path, "PNG")
    print("trail ->", trail_path)
    print("collisions:", hits, "corner_demo:", ccorner)

    window.close()
    pump(app, 10)
    return 0 if moved else 1


if __name__ == "__main__":
    raise SystemExit(main())
