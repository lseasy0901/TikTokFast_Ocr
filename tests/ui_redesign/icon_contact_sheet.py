# -*- coding: utf-8 -*-
"""图标对比：把每个自绘图标渲染成大尺寸对照图。

用途：验证「设置」齿轮图标在真实绘制代码下的可辨识度（Phase 4 验收证据）。
输出一张 PNG 对照表，左侧是改造前的画法（离线复刻），右侧是当前画法。

用法：
    <venv-python> tests/ui_redesign/icon_contact_sheet.py --out <path.png>
"""

from __future__ import annotations

import argparse
import math
import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from PySide6.QtCore import QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import (QColor, QFont, QPainter, QPainterPath, QPen,  # noqa: E402
                           QPixmap)
from PySide6.QtWidgets import QApplication  # noqa: E402

from gui import theme  # noqa: E402
from gui.theme import Palette  # noqa: E402
from gui.widgets import IconButton, _draw_icon_glyph, icon_pixmap  # noqa: E402

CELL = 96          # 每格边长
PAD = 14
COLS = 3


def _draw_old_gear(painter, cx: float, cy: float, s: float) -> None:
    """改造前的齿轮画法（8 根辐条 + 齿圈 + 中心孔），用于对照。

    刻意从原实现复制过来（见 PHASE2_UI_AUDIT §3.5 引用的旧代码），
    这样对照图证明的是"画法差异"，而不是"我把新画法画得更粗"。
    """
    teeth = s * 0.86
    ring = s * 0.52
    for i in range(8):
        angle = math.radians(i * 45.0)
        dx, dy = math.cos(angle), math.sin(angle)
        painter.drawLine(QPointF(cx + dx * ring, cy + dy * ring),
                         QPointF(cx + dx * teeth, cy + dy * teeth))
    painter.drawEllipse(QRectF(cx - ring, cy - ring, ring * 2, ring * 2))
    hole = s * 0.22
    painter.drawEllipse(QRectF(cx - hole, cy - hole, hole * 2, hole * 2))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="docs/ui-redesign/evidence/screenshots/icon_contact_sheet.png")
    args = parser.parse_args()

    theme.configure_high_dpi()
    app = QApplication(sys.argv)

    kinds = [
        (IconButton.HELP, "help"),
        (IconButton.LICENSE, "license"),
        (IconButton.CONTACT, "contact"),
        (IconButton.SETTINGS, "settings"),
        (IconButton.FULLSCREEN, "fullscreen"),
        (IconButton.FULLSCREEN_EXIT, "fullscreenExit"),
    ]

    # 3 列：改造前(小尺寸) / 改造后(小尺寸) / 改造后(放大)
    rows = len(kinds)
    w = PAD + COLS * (CELL + PAD)
    h = PAD + rows * (CELL + PAD) + 46
    sheet = QPixmap(w, h)
    sheet.fill(QColor(Palette.APP_BG))

    p = QPainter(sheet)
    p.setRenderHint(QPainter.Antialiasing, True)

    title_font = QFont()
    title_font.setPixelSize(13)
    title_font.setBold(True)
    p.setFont(title_font)
    p.setPen(QPen(QColor(Palette.TEXT)))
    p.drawText(QRectF(PAD, 6, w - 2 * PAD, 22), Qt.AlignLeft | Qt.AlignVCenter,
               "标题栏图标对照：改造前(28px/s=6) → 改造后(30px, 等比) → 放大 4x")
    p.drawText(QRectF(PAD, 24, w - 2 * PAD, 20), Qt.AlignLeft | Qt.AlignVCenter,
               f"实际渲染尺寸 = {theme.Metrics.ICON_BUTTON_SIZE}px；图标色 = {Palette.ICON}")

    small_font = QFont()
    small_font.setPixelSize(11)

    y0 = 46
    for r, (kind, label) in enumerate(kinds):
        cy = y0 + PAD + r * (CELL + PAD) + CELL / 2
        for c in range(COLS):
            cx = PAD + c * (CELL + PAD) + CELL / 2
            cell = QRectF(cx - CELL / 2, cy - CELL / 2, CELL, CELL)
            # 格子底：模拟标题栏的深色
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(Palette.CHROME_BG))
            p.drawRoundedRect(cell.adjusted(2, 2, -2, -2), 8, 8)

        # 列 0：改造前画法，按旧尺寸 28px 渲染
        p.setPen(QPen(QColor(Palette.TEXT_DIM)))
        p.setBrush(Qt.NoBrush)
        pen = p.pen()
        pen.setWidthF(1.2)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        cx0 = PAD + CELL / 2
        if kind == IconButton.SETTINGS:
            _draw_old_gear(p, cx0, cy, 6.0)
        else:
            _draw_icon_glyph(p, kind, cx0, cy, 6.0)

        # 列 1：改造后画法，真实 30px 尺寸
        p.setPen(QPen(QColor(Palette.ICON)))
        pen = p.pen()
        pen.setWidthF(1.4)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        cx1 = PAD + (CELL + PAD) + CELL / 2
        _draw_icon_glyph(p, kind, cx1, cy, theme.Metrics.ICON_BUTTON_SIZE * (6.0 / 28.0))

        # 列 2：改造后画法放大 4x（看几何形状本身）
        cx2 = PAD + 2 * (CELL + PAD) + CELL / 2
        _draw_icon_glyph(p, kind, cx2, cy, 24.0)

        # 标签
        p.setFont(small_font)
        p.setPen(QPen(QColor(Palette.TEXT_MUTED)))
        p.drawText(QRectF(PAD, cy + CELL / 2 + 2, CELL, 16),
                   Qt.AlignCenter, label)
        p.setPen(QPen(QColor(Palette.TEXT_DIM)))
        p.drawText(QRectF(PAD + (CELL + PAD), cy + CELL / 2 + 2, CELL, 16),
                   Qt.AlignCenter, "30px 实际")
        p.drawText(QRectF(PAD + 2 * (CELL + PAD), cy + CELL / 2 + 2, CELL, 16),
                   Qt.AlignCenter, "4x 放大")

    # 再画一排"和截图同尺寸"的真实 IconButton 图标（用 icon_pixmap 同一条代码路径）
    p.end()

    out = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    ok = sheet.save(out, "PNG")
    print(f"saved={ok} path={out} size={sheet.width()}x{sheet.height()}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
