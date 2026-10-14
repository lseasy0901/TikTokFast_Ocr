# -*- coding: utf-8 -*-
"""在**真实渲染截图**上复验对比度（Phase 6）。

设计系统 MASTER.md §2.3 的对比度是按**不透明色**算的；玻璃表面是半透明的，
实际像素取决于合成结果。本脚本从真实截图采样合成后的背景色，再算文字对比度。

用法：<venv-python> contrast_real_pixels.py <screenshot.png>
"""
import os, sys, statistics
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

app = QApplication([])
img = QImage(sys.argv[1])
print(f"截图: {sys.argv[1]}  {img.width()}x{img.height()}")


def lin(c):
    c = c / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def lum(rgb):
    r, g, b = rgb
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def ratio(a, b):
    la, lb = lum(a), lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def hex2rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def sample_bg(x0, y0, w=40, h=14):
    """采样区域内的**中位数**颜色 —— 避开文字/图标像素。"""
    rs, gs, bs = [], [], []
    for y in range(y0, y0 + h):
        for x in range(x0, x0 + w):
            if 0 <= x < img.width() and 0 <= y < img.height():
                c = img.pixelColor(x, y)
                rs.append(c.red()); gs.append(c.green()); bs.append(c.blue())
    return (int(statistics.median(rs)), int(statistics.median(gs)),
            int(statistics.median(bs)))


TEXT = hex2rgb("#E9F0FF")
TEXT_DIM = hex2rgb("#A8B4C7")
ACCENT = hex2rgb("#80BFFF")
OK = hex2rgb("#64D9A0")

SURFACES = [
    ("标题栏 chrome", (600, 12)),
    ("地址栏 chrome", (600, 44)),
    ("OCR 玻璃面板", (1150, 340)),
    ("玻璃面板标题条", (1150, 92)),
    ("预览井 (scrim 下)", (700, 560)),
    ("底部状态栏", (620, 824)),
    ("输入框", (300, 50)),
]

print()
print(f"{'表面':<20}{'实测合成色':<18}{'主文字':>9}{'次级文字':>10}{'强调色':>9}{'成功色':>9}")
print("-" * 78)
worst = 99.0
for name, (x, y) in SURFACES:
    bg = sample_bg(x, y)
    vals = [ratio(fg, bg) for fg in (TEXT, TEXT_DIM, ACCENT, OK)]
    worst = min(worst, min(vals))
    mark = "OK" if min(vals) >= 4.5 else "!!"
    print(f"{name:<20}#{bg[0]:02X}{bg[1]:02X}{bg[2]:02X}          "
          f"{vals[0]:>8.2f} {vals[1]:>9.2f} {vals[2]:>8.2f} {vals[3]:>8.2f}  {mark}")

print()
print(f"全部组合中的最低对比度 = {worst:.2f}  ->  {'全部通过 AA 4.5:1' if worst >= 4.5 else '存在未达标组合'}")
