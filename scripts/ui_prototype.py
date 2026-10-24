# -*- coding: utf-8 -*-
"""LiveLens 新版主界面 —— **独立可预览的视觉原型**（Phase UI-1）。

这是什么：
    只做视觉与布局验证的原型。**不接入任何真实业务**：不解析直播、不起
    FFmpeg、不跑 OCR、不碰剪贴板/授权/更新。所有数值都是明确标注的模拟数据
    （界面右上角常驻「原型 · 模拟数据」徽标，预览区带水印）。

    正式应用的行为与文件在本阶段**完全未改动**；原型自带入口，可单独运行。

复用了什么（刻意最大化复用，避免另起一套）：
    gui.theme        apply_theme() / Palette / Metrics / tone_color
    gui.widgets      WindowButton / OverlayHost / chip_html / set_property /
                     h_line / enable_mouse_tracking
    gui.video_widget VideoWidget（等比缩放渲染 + ROI 叠加）

本原型自己实现、正式代码里没有的部分：
    顶部地址栏、底部分组性能条、右栏收起/展开、全屏工作台与标题栏滑出、
    Mica/Acrylic 背景（Win32 DWM，效果不支持时优雅降级为不透明石墨色）。

用法：
    venv/Scripts/python.exe ui_prototype.py                    # 正常窗口
    venv/Scripts/python.exe ui_prototype.py --state collapsed  # 右侧面板收起
    venv/Scripts/python.exe ui_prototype.py --state fullscreen # 全屏工作台
    venv/Scripts/python.exe ui_prototype.py --size 1100x660    # 指定窗口尺寸
    venv/Scripts/python.exe ui_prototype.py --shot out.png     # 截图后退出（验证用）
"""

import argparse
import ctypes
import os
import sys

import numpy as np
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gui import theme
from gui.theme import Metrics, Palette
from gui.video_widget import VideoWidget
from gui.widgets import OverlayHost, WindowButton, chip_html, h_line, set_property

# ----------------------------------------------------------------------
# 模拟数据（**仅原型**；正式界面不得接入这些值）
# ----------------------------------------------------------------------
MOCK_URL = "https://live.douyin.com/123456"
MOCK_HISTORY = ("https://live.douyin.com/722167467158", "https://live.douyin.com/123456")
MOCK_CODE = "ABX1234"
MOCK_HISTORY_CODES = ("ABX1234", "QWE5678", "TRY2468")
MOCK_STATUS = "LIVE"
MOCK_RESOLUTION = "1920 × 1080"
MOCK_FPS = "60.0"
#: 性能条：没有真实数据的一律显示 "—"（**不虚构端到端延迟**）
DASH = "—"

# ----------------------------------------------------------------------
# Studio Minimal 视觉令牌（**原型专用**；正式主题仍是 gui/theme.py，未改动）
# 方向：DaVinci Resolve 的工作台结构 + Linear 的极简排版。
# 中性深灰、细分隔线、明度差与留白建立层级；不用大面积彩色/发光/渐变。
# ----------------------------------------------------------------------
class Studio:
    BG = "#141518"           # 应用底（Mica 不可用时的兜底）
    SURFACE = "#1D1E22"      # 面板工作表面
    SURFACE_ALT = "#191A1E"  # 次级表面（工具栏）
    WELL = "#0B0C0E"         # 视频井（最暗，画面为视觉中心）
    LINE = "#303137"         # 1px 分隔线
    LINE_SOFT = "#25262B"    # 更弱的线
    TEXT = "#F0F0F2"         # 主要文字
    TEXT2 = "#90939B"        # 次要文字
    TEXT3 = "#6A6D74"        # 更弱（注释/单位）
    ACTION = "#D9DBE0"       # 主要操作色（浅中性实心）
    ACTION_TEXT = "#141518"
    OK = "#83B69A"           # 成功（低饱和绿）
    WARN = "#C8A96A"
    DANGER = "#C4736B"
    CHROME = "rgba(20, 21, 24, 0.72)"   # 框架层：半透明深色（Mica 只作底纹）
    HOVER = "rgba(255, 255, 255, 0.06)"
    PRESSED = "rgba(0, 0, 0, 0.25)"
    SELECT = "rgba(255, 255, 255, 0.08)"
    MONO = 'Consolas, "Cascadia Mono", monospace'
    RADIUS = 6               # 统一圆角（克制，不做卡片感）


PROTO_QSS = f"""
/* ---- 窗口底：Mica 可用时整窗透明，标题栏与主体材质连续 ---- */
#windowRoot {{ background-color: {Studio.BG}; border: none; }}
#windowRoot[mica="true"] {{ background-color: transparent; }}

/* ---- 标题栏 / 地址栏 / 性能条：半透明深色"框架"层 ----
   刻意不是全透明：Mica 只作为很淡的底纹透出来，避免系统亮色主题下
   出现浅色标题栏与深色主体割裂（实测全透明会跟着系统主题变浅）。 */
#titleBar {{ background: {Studio.CHROME}; border-bottom: 1px solid {Studio.LINE_SOFT}; }}
#titleBar QLabel {{ color: {Studio.TEXT}; }}
#appLogo {{ background-color: {Studio.ACTION}; border-radius: 2px; }}
#appTitle {{ font-size: 13px; font-weight: 600; color: {Studio.TEXT};
             letter-spacing: 0.3px; }}
#appSubtitle {{ font-size: 11px; color: {Studio.TEXT3}; }}
#protoBadge {{ color: {Studio.WARN}; background: transparent;
               border: 1px solid {Studio.LINE}; border-radius: 4px;
               padding: 1px 6px; font-size: 10px; }}
#pureTitleButton {{ color: {Studio.TEXT2}; background: transparent;
                    border: none; border-radius: {Studio.RADIUS}px;
                    padding: 3px 10px; font-size: 12px; }}
#pureTitleButton:hover {{ background: {Studio.HOVER}; color: {Studio.TEXT}; }}
#pureTitleButton:pressed {{ background: {Studio.PRESSED}; }}

/* ---- 窗口控制：中性 ---- */
#winBtn, #winBtnClose {{ background: transparent; border: none;
                         border-radius: 0px; }}
#winBtn:hover {{ background: {Studio.HOVER}; }}
#winBtn:pressed {{ background: {Studio.PRESSED}; }}
#winBtnClose:hover {{ background: rgba(196, 115, 107, 0.85); }}
#winBtnClose:pressed {{ background: {Studio.PRESSED}; }}

/* ---- 顶部地址栏：透明；输入/下拉用表面色 + 细线 ---- */
#addressBar {{ background: {Studio.CHROME}; border-bottom: 1px solid {Studio.LINE}; }}
#addressBar QLineEdit {{ background: {Studio.SURFACE}; color: {Studio.TEXT};
                         border: 1px solid {Studio.LINE};
                         border-radius: {Studio.RADIUS}px; padding: 6px 10px; }}
#addressBar QLineEdit:focus {{ border-color: {Studio.TEXT3}; }}
#addressBar QComboBox {{ background: {Studio.SURFACE}; color: {Studio.TEXT2};
                         border: 1px solid {Studio.LINE};
                         border-radius: {Studio.RADIUS}px; padding: 5px 8px; }}

/* ---- 通用按钮：主操作 = 浅中性实心；次要 = 透明 + 细线 ---- */
QPushButton {{ background: transparent; color: {Studio.TEXT2};
               border: 1px solid {Studio.LINE};
               border-radius: {Studio.RADIUS}px; padding: 3px 10px; }}
QPushButton:hover {{ background: {Studio.HOVER}; color: {Studio.TEXT}; }}
QPushButton:pressed {{ background: {Studio.PRESSED}; }}
QPushButton:disabled {{ color: {Studio.TEXT3}; border-color: {Studio.LINE_SOFT}; }}
QPushButton[variant="primary"] {{ background: {Studio.ACTION};
                                  color: {Studio.ACTION_TEXT};
                                  border: 1px solid {Studio.ACTION};
                                  font-weight: 600; }}
QPushButton[variant="primary"]:hover {{ background: #E6E8EC;
                                        border-color: #E6E8EC; }}
QPushButton[variant="primary"]:pressed {{ background: #C7C9CF;
                                          border-color: #C7C9CF; }}

/* ---- 视频井：无卡片、无圆角、无描边，只有明度差 ---- */
#previewWell {{ background: {Studio.WELL}; border: none; }}

/* ---- 视频下工具栏：次级表面 + 顶部细线 ---- */
#toolStrip {{ background: {Studio.SURFACE_ALT}; border: none;
              border-top: 1px solid {Studio.LINE_SOFT}; }}

/* ---- 右侧 OCR 面板：一条左细线分隔，不用卡片边框 ---- */
#ocrPanel {{ background: {Studio.SURFACE}; border: none;
             border-left: 1px solid {Studio.LINE}; }}
#ocrPanel QLabel {{ color: {Studio.TEXT2}; }}
QSplitter::handle {{ background: transparent; }}

/* ---- 房间码：大号高对比，唯一视觉焦点 ---- */
#codeBig {{ color: {Studio.TEXT}; background: transparent;
            border: none; border-top: 1px solid {Studio.LINE_SOFT};
            border-bottom: 1px solid {Studio.LINE_SOFT};
            border-radius: 0px; padding: 14px 4px;
            font-family: {Studio.MONO}; font-size: 30px;
            font-weight: 600; letter-spacing: 6px; }}

/* ---- 状态芯片：纯文字 + 圆点，无底色无边框 ---- */
#metricChip {{ background: transparent; border: none; border-radius: 0px;
               padding: 0px; color: {Studio.TEXT2};
               font-family: {Studio.MONO}; font-size: 11px; }}
#metricChip[tone="ok"] {{ color: {Studio.OK}; }}
#metricChip[tone="warn"] {{ color: {Studio.WARN}; }}
#metricChip[tone="error"] {{ color: {Studio.DANGER}; }}
#metricChip[tone="accent"] {{ color: {Studio.TEXT2}; }}

/* ---- 分区标题：小号、字距略开、弱色 ---- */
#sectionLabel {{ color: {Studio.TEXT3}; font-size: 11px;
                 font-weight: 600; letter-spacing: 0.6px; }}

/* ---- 列表：无边框无圆角，选中用明度差 ---- */
QListWidget {{ background: transparent; border: none; color: {Studio.TEXT2};
               font-family: {Studio.MONO}; font-size: 12px; outline: none; }}
QListWidget::item {{ padding: 4px 6px; border-radius: 4px; }}
QListWidget::item:hover {{ background: {Studio.HOVER}; color: {Studio.TEXT}; }}
QListWidget::item:selected {{ background: {Studio.SELECT}; color: {Studio.TEXT}; }}

/* ---- 下拉弹层 ---- */
QComboBox QAbstractItemView {{ background: {Studio.SURFACE};
                               border: 1px solid {Studio.LINE};
                               color: {Studio.TEXT2}; outline: none; }}
QComboBox QAbstractItemView::item:hover {{ background: {Studio.HOVER};
                                           color: {Studio.TEXT}; }}
QComboBox QAbstractItemView::item:selected {{ background: {Studio.SELECT};
                                              color: {Studio.TEXT}; }}

/* ---- 底部性能栏：与标题栏同属"框架"层，透明 + 顶部细线 ---- */
#perfStrip {{ background: {Studio.CHROME}; border-top: 1px solid {Studio.LINE}; }}
#perfGroup {{ color: {Studio.TEXT3}; font-size: 11px; }}
#perfValue {{ color: {Studio.TEXT2}; font-size: 11px;
              font-family: {Studio.MONO}; }}
#protoNote {{ color: {Studio.TEXT3}; font-size: 11px; }}

/* ---- 折叠/展开条 ---- */
#collapseBtn, #expandBtn {{ color: {Studio.TEXT3}; background: transparent;
                            border: none; border-radius: {Studio.RADIUS}px;
                            padding: 2px 6px; }}
#collapseBtn:hover, #expandBtn:hover {{ background: {Studio.HOVER};
                                        color: {Studio.TEXT}; }}
"""


# ----------------------------------------------------------------------
# Mica / Acrylic（Win32 DWM；不支持时优雅降级）
# ----------------------------------------------------------------------
def _is_win11_22h2_or_newer() -> bool:
    """Mica 需要 Windows 11 22H2（build 22621）及以上。"""
    if os.name != "nt":
        return False
    try:
        return sys.getwindowsversion().build >= 22621
    except Exception:  # noqa: BLE001
        return False


def enable_backdrop(hwnd: int, kind: str = "mica") -> bool:
    """给窗口启用 DWM 系统背景（mica / acrylic）。

    这不是 PySide6 的能力，需要用 Win32 的 DwmSetWindowAttribute；
    失败（系统版本不足 / API 不存在 / 被策略禁用）一律返回 False，
    由调用方退回不透明石墨底色 —— **不做任何视觉上的欺骗**。
    """
    if not _is_win11_22h2_or_newer():
        return False
    #: DWMWA_SYSTEMBACKDROP_TYPE = 38；2 = Mica，3 = Acrylic，4 = Tabbed
    backdrop = {"mica": 2, "acrylic": 3}.get(kind)
    if backdrop is None:
        return False
    try:
        dwm = ctypes.windll.dwmapi
        # 顺序很重要：先声明「本窗口是深色」，再设置背景类型 —— 反过来的话
        # DWM 会先按系统浅色主题合成 Mica，浅色背景一旦生效不会自动重算。
        dark = ctypes.c_int(1)
        dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), ctypes.sizeof(dark))
        value = ctypes.c_int(backdrop)
        hr = dwm.DwmSetWindowAttribute(hwnd, 38, ctypes.byref(value),
                                       ctypes.sizeof(value))
        if hr != 0:
            return False
        # 圆角（33 = DWMWA_WINDOW_CORNER_PREFERENCE）
        corner = ctypes.c_int(2)
        dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(corner), ctypes.sizeof(corner))
        return True
    except Exception:  # noqa: BLE001
        return False


# ----------------------------------------------------------------------
# 模拟画面（仅原型：本地生成一张带水印的静帧）
# ----------------------------------------------------------------------
def mock_frame(w: int = 1280, h: int = 720) -> np.ndarray:
    """画一张"视频画面"占位图：中性灰阶（无彩色渐变）+ 明显的原型水印。

    刻意保持中性：Studio Minimal 要求低饱和，占位画面也不引入彩色。
    """
    frame = np.full((h, w, 3), 22, dtype=np.uint8)          # 中性深灰
    for y in range(h):
        v = 22 + (y * 14) // h                               # 极弱的灰阶过渡
        frame[y, :] = (v, v, v)
    import cv2
    box = (w // 8 - 12, h // 2 - 20, w // 8 + 460, h // 2 + 70)
    cv2.rectangle(frame, (box[0], box[1]), (box[2], box[3]), (34, 34, 34), -1)
    cv2.putText(frame, "MOCK VIDEO  -  PROTOTYPE", (w // 8, h // 2),
                cv2.FONT_HERSHEY_SIMPLEX, 1.0, (150, 150, 152), 2, cv2.LINE_AA)
    cv2.putText(frame, "1920x1080 60fps (simulated)", (w // 8, h // 2 + 40),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (108, 108, 112), 1, cv2.LINE_AA)
    return frame


# ----------------------------------------------------------------------
# 小组件
# ----------------------------------------------------------------------
def _chip(text: str, tone: str = "neutral", dot: bool = False) -> QLabel:
    chip = QLabel()
    chip.setObjectName("metricChip")
    chip.setTextFormat(Qt.RichText)
    chip.setText(chip_html(text, tone) if dot else text)
    chip.setProperty("tone", tone)
    return chip


def _proto_button(text: str, primary: bool = False) -> QPushButton:
    btn = QPushButton(text)
    btn.setObjectName("statusButton")
    btn.setFixedHeight(30)
    if primary:
        set_property(btn, "variant", "primary")
    return btn


class Toolbar(QFrame):
    """视频下方的简洁工具栏：状态 / 分辨率 / FPS / ROI 快捷入口。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("toolStrip")
        self.setFixedHeight(44)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 0, 12, 0)
        row.setSpacing(10)
        row.addWidget(_chip(f"{MOCK_STATUS}", "ok", dot=True))
        row.addWidget(_chip(MOCK_RESOLUTION))
        row.addWidget(_chip(f"FPS {MOCK_FPS}"))
        row.addStretch(1)
        self.roi_btn = _proto_button("选择识别区域")
        self.roi_btn.setToolTip("拖动框选 → 松开保留选区 → Enter 确认 / Esc 取消")
        self.help_btn = _proto_button("说明")
        self.help_btn.setToolTip("框选方式：拖动后松开不会立即确认，"
                                 "按 Enter 才确认，按 Esc 取消")
        row.addWidget(self.roi_btn)
        row.addWidget(self.help_btn)


class PerfStrip(QFrame):
    """底部性能条：分组显示；**无真实数据一律 “—”**，不虚构端到端延迟。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("perfStrip")
        self.setFixedHeight(30)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 0, 12, 0)
        row.setSpacing(18)
        groups = (
            ("链路", (("首帧", DASH), ("跳帧", DASH), ("管道", DASH))),
            ("解码", (("解码FPS", DASH), ("显示FPS", DASH))),
            ("渲染", (("set_frame", DASH), ("paint", DASH))),
            ("OCR", (("识别", DASH), ("端到端", DASH))),
        )
        for name, items in groups:
            row.addWidget(self._group(name, items))
        row.addStretch(1)
        note = QLabel("原型：无真实数据，全部显示 —")
        note.setObjectName("protoNote")
        row.addWidget(note)

    @staticmethod
    def _group(name: str, items) -> QWidget:
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        head = QLabel(f"{name}:")
        head.setObjectName("perfGroup")
        lay.addWidget(head)
        for label, value in items:
            lay.addWidget(QLabel(f"{label}")) if False else None
            lbl = QLabel(f"{label} {value}")
            lbl.setObjectName("perfValue")
            lay.addWidget(lbl)
        return box


class OcrPanel(QFrame):
    """右侧 OCR 面板（约 310px）：房间码优先 → 会话历史 → 游戏/ROI → 底部 OCR 按钮。"""

    WIDTH = 310

    def __init__(self, on_collapse=None, parent=None):
        super().__init__(parent)
        self.setObjectName("ocrPanel")
        self.setFixedWidth(self.WIDTH)
        col = QVBoxLayout(self)
        col.setContentsMargins(14, 12, 14, 12)
        col.setSpacing(8)      # 660px 高度下仍不挤压（实测 10px 会溢 7px）

        head = QHBoxLayout()
        title = QLabel("OCR 控制")
        title.setObjectName("sectionLabel")
        head.addWidget(title)
        head.addStretch(1)
        self.collapse_btn = _proto_button("»")
        self.collapse_btn.setObjectName("collapseBtn")
        self.collapse_btn.setFixedWidth(28)
        self.collapse_btn.setToolTip("收起右侧面板")
        if on_collapse is not None:
            self.collapse_btn.clicked.connect(on_collapse)
        head.addWidget(self.collapse_btn)
        col.addLayout(head)

        # 1) 当前房间码 —— 视觉优先级最高
        code_label = QLabel("当前房间码")
        code_label.setObjectName("sectionLabel")
        col.addWidget(code_label)
        self.code_box = QLabel(MOCK_CODE)
        self.code_box.setObjectName("codeBig")
        self.code_box.setAlignment(Qt.AlignmentFlag.AlignCenter)
        col.addWidget(self.code_box)
        self.state_chip = _chip("已复制到剪贴板", "ok", dot=True)
        col.addWidget(self.state_chip)

        col.addWidget(h_line())

        # 2) 本次会话识别历史
        hist_label = QLabel("本次会话")
        hist_label.setObjectName("sectionLabel")
        col.addWidget(hist_label)
        self.history = QListWidget()
        # 会话历史是唯一可变高元素：最小 72px，其余让给固定件，
        # 这样 1100x660（最小窗口）下底部 OCR 按钮不会被挤掉。
        self.history.setMinimumHeight(72)
        self.history.setMaximumHeight(120)
        for code in MOCK_HISTORY_CODES:
            self.history.addItem(QListWidgetItem(code))
        col.addWidget(self.history)

        col.addWidget(h_line())

        # 3) 游戏与 ROI
        game_label = QLabel("游戏")
        game_label.setObjectName("sectionLabel")
        col.addWidget(game_label)
        self.game = QComboBox()
        self.game.addItem("三角洲行动", "delta_force")
        self.game.addItem("无畏契约", "valorant")
        col.addWidget(self.game)

        roi_label = QLabel("OCR 区域")
        roi_label.setObjectName("sectionLabel")
        col.addWidget(roi_label)
        self.roi_hint = QLabel("未框选 · 拖动后按 Enter 确认，Esc 取消（原型）")
        self.roi_hint.setObjectName("protoNote")
        col.addWidget(self.roi_hint)

        col.addStretch(1)

        # 4) OCR 启停固定在面板底部
        self.ocr_btn = _proto_button("开始OCR", primary=True)
        self.ocr_btn.setFixedHeight(36)
        col.addWidget(self.ocr_btn)


# ----------------------------------------------------------------------
# 主窗口（原型）
# ----------------------------------------------------------------------
class PrototypeWindow(QMainWindow):
    def __init__(self, state: str = "normal"):
        super().__init__()
        self._state = state
        self._backdrop = "none"
        self.setWindowFlag(Qt.FramelessWindowHint, True)
        self.setMinimumSize(1100, 660)
        self.resize(1400, 860)

        root = QFrame()
        root.setObjectName("windowRoot")
        self._root = root
        root_col = QVBoxLayout(root)
        root_col.setContentsMargins(0, 0, 0, 0)
        root_col.setSpacing(0)

        root_col.addWidget(self._build_title_bar())
        root_col.addWidget(self._build_address_bar())

        # 主体：预览 | OCR 面板（QSplitter，收起时面板隐藏）
        self._splitter = QSplitter(Qt.Horizontal)
        self._splitter.setObjectName("mainSplitter")
        self._splitter.setChildrenCollapsible(False)
        self._splitter.setHandleWidth(Metrics.SPLITTER_HANDLE)
        self._splitter.addWidget(self._build_preview())
        self._ocr_panel = OcrPanel(on_collapse=self.toggle_panel)
        self._splitter.addWidget(self._ocr_panel)
        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 0)
        self._splitter.setSizes([1000, OcrPanel.WIDTH])

        # 收起后重新展开的入口：贴右边缘的细条（比"没有入口"诚实）
        body_row = QHBoxLayout()
        body_row.setContentsMargins(0, 0, 0, 0)
        body_row.setSpacing(0)
        body_row.addWidget(self._splitter, 1)
        self._expand_btn = _proto_button("«")
        self._expand_btn.setObjectName("expandBtn")
        self._expand_btn.setFixedWidth(20)
        self._expand_btn.setToolTip("展开右侧 OCR 面板")
        self._expand_btn.clicked.connect(lambda: self.set_panel_visible(True))
        self._expand_btn.hide()
        body_row.addWidget(self._expand_btn)
        body_container = QWidget()
        body_container.setLayout(body_row)
        root_col.addWidget(body_container, 1)

        root_col.addWidget(self._build_bottom())
        self.setCentralWidget(root)

        # 全屏工作台：顶部热区滑出标题栏（含地址栏），离开约 1 秒收起
        self._top_hot = self._build_top_hotzone()
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(1000)
        self._hide_timer.timeout.connect(self._maybe_autohide)

        self._title_bar.installEventFilter(self)
        self._address_bar.installEventFilter(self)

        from gui.widgets import enable_mouse_tracking
        enable_mouse_tracking(root)

        self._apply_state(state)

    # ---------- 构建 ----------
    def _build_title_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("titleBar")
        bar.setFixedHeight(Metrics.TITLEBAR_HEIGHT)
        row = QHBoxLayout(bar)
        row.setContentsMargins(12, 0, 0, 0)
        row.setSpacing(8)

        logo = QLabel()
        logo.setObjectName("appLogo")
        logo.setFixedSize(14, 14)
        row.addWidget(logo)
        title = QLabel("LiveLens")
        title.setObjectName("appTitle")
        row.addWidget(title)
        sub = QLabel("低延迟播放工作台 · 原型")
        sub.setObjectName("appSubtitle")
        row.addWidget(sub)

        badge = QLabel("原型 · 模拟数据")
        badge.setObjectName("protoBadge")
        row.addSpacing(6)
        row.addWidget(badge)
        row.addStretch(1)

        for text in ("帮助", "授权", "设置"):
            btn = QPushButton(text)
            btn.setObjectName("pureTitleButton")
            btn.setFixedHeight(26)
            row.addWidget(btn)

        self._title_bar = bar
        self.min_button = WindowButton(WindowButton.MIN)
        self.max_button = WindowButton(WindowButton.MAX)
        self.close_button = WindowButton(WindowButton.CLOSE)
        self.min_button.clicked.connect(self.showMinimized)
        self.max_button.setToolTip("全屏工作台（仅标题栏按钮，无快捷键）")
        self.max_button.clicked.connect(self.toggle_fullscreen)
        self.close_button.clicked.connect(self.close)
        for b in (self.min_button, self.max_button, self.close_button):
            row.addWidget(b)
        return bar

    def _build_address_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("addressBar")
        bar.setFixedHeight(52)
        row = QHBoxLayout(bar)
        row.setContentsMargins(12, 8, 12, 8)
        row.setSpacing(8)

        self.url_input = QLineEdit(MOCK_URL)
        self.url_input.setPlaceholderText("粘贴抖音直播间地址，例如 https://live.douyin.com/…")
        self.url_input.setClearButtonEnabled(True)
        self.history_combo = QComboBox()
        for item in MOCK_HISTORY:
            self.history_combo.addItem(item)
        self.history_combo.setFixedWidth(260)
        self.connect_btn = _proto_button("连接", primary=True)
        self.connect_btn.setFixedWidth(96)

        row.addWidget(self.url_input, 1)
        row.addWidget(self.history_combo)
        row.addWidget(self.connect_btn)
        self._address_bar = bar
        return bar

    def _build_preview(self) -> QWidget:
        well = QFrame()
        well.setObjectName("previewWell")
        col = QVBoxLayout(well)
        col.setContentsMargins(12, 12, 12, 12)
        col.setSpacing(10)

        self._overlay_host = OverlayHost()
        self._video = VideoWidget()
        self._video.set_frame(mock_frame())
        host_lay = QVBoxLayout(self._overlay_host) if False else None
        vb = QVBoxLayout(self._overlay_host)
        vb.setContentsMargins(0, 0, 0, 0)
        vb.addWidget(self._video)
        col.addWidget(self._overlay_host, 1)
        col.addWidget(Toolbar())
        return well

    def _build_bottom(self) -> QWidget:
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(PerfStrip())
        return box

    def _build_top_hotzone(self) -> QFrame:
        hot = QFrame(self)
        hot.setFixedHeight(4)
        hot.setStyleSheet("background: transparent;")
        hot.setMouseTracking(True)
        hot.installEventFilter(self)
        hot.setToolTip("把鼠标移到屏幕顶部可滑出标题栏")
        return hot

    # ---------- 状态 ----------
    def _apply_state(self, state: str) -> None:
        self._state = state
        if state == "collapsed":
            self.set_panel_visible(False)
        elif state == "fullscreen":
            self.toggle_fullscreen()

    def toggle_panel(self) -> None:
        self.set_panel_visible(not self._ocr_panel.isVisible())

    def set_panel_visible(self, visible: bool) -> None:
        self._ocr_panel.setVisible(visible)
        if visible:
            self._splitter.setSizes([max(600, self.width() - OcrPanel.WIDTH),
                                     OcrPanel.WIDTH])
            self.setMinimumWidth(1100)
        else:
            self.setMinimumWidth(880)      # 收起后窄窗口可用
        self._sync_expand_button()

    def _sync_expand_button(self) -> None:
        """收起时显示贴右边缘的展开条；全屏时同样"先收起再展开"可用。"""
        if not hasattr(self, "_expand_btn"):
            return
        self._expand_btn.setVisible(not self._ocr_panel.isVisible())

    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
            self._title_bar.show()
            self._address_bar.show()
            self._top_hot.hide()
            self._hide_timer.stop()
        else:
            self.showFullScreen()
            self._top_hot.show()
            self._top_hot.raise_()
            self._position_hotzone()
            self._title_bar.hide()         # 全屏默认隐藏，鼠标到顶部再滑出
            self._address_bar.hide()
            if hasattr(self, "_expand_btn"):
                self._expand_btn.hide()

    # ---------- 全屏标题栏滑出/收起 ----------
    def _position_hotzone(self) -> None:
        self._top_hot.setGeometry(0, 0, self.width(), 4)

    def eventFilter(self, obj, event):  # noqa: N802
        from PySide6.QtCore import QEvent
        hot = getattr(self, "_top_hot", None)
        if hot is not None and obj is hot and event.type() == QEvent.Type.Enter:
            if self.isFullScreen():
                self._title_bar.show()
                self._address_bar.show()
                self._title_bar.raise_()
                self._address_bar.raise_()
                self._hide_timer.start()
        elif obj in (self._title_bar, self._address_bar):
            if event.type() == QEvent.Type.Leave:
                if self.isFullScreen():
                    self._hide_timer.start()
            elif event.type() == QEvent.Type.Enter:
                self._hide_timer.stop()
        return super().eventFilter(obj, event)

    def _maybe_autohide(self) -> None:
        if not self.isFullScreen() or self._top_hot.underMouse():
            return
        if self._title_bar.underMouse() or self._address_bar.underMouse():
            self._hide_timer.start()       # 鼠标还在栏上，继续等
            return
        self._title_bar.hide()
        self._address_bar.hide()

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._position_hotzone()

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        # Mica 需要窗口已创建句柄；失败则保持不透明石墨底（优雅降级）
        if self._backdrop == "none":
            ok = enable_backdrop(int(self.winId()), "mica")
            self._backdrop = "mica" if ok else "opaque"
            if ok:
                self.setAttribute(Qt.WA_TranslucentBackground, True)
                # Mica 生效时让窗口底透明，标题栏/地址栏/性能条与主体同材质
                set_property(self._root, "mica", "true")
                self._root.style().unpolish(self._root)
                self._root.style().polish(self._root)
                # 部分系统在窗口完成合成前设置会退回浅色，显示后再补一次
                QTimer.singleShot(150, lambda: enable_backdrop(int(self.winId()),
                                                               "mica"))
            print("[prototype] backdrop=%s" % self._backdrop)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="normal",
                    choices=["normal", "collapsed", "fullscreen"])
    ap.add_argument("--size", default=None, help="例如 1100x660")
    ap.add_argument("--shot", default=None, help="截图输出路径（验证用，截完退出）")
    args = ap.parse_args()

    app = QApplication.instance() or QApplication([])
    theme.apply_theme()                       # 复用正式主题（未修改）
    app.setStyleSheet(app.styleSheet() + PROTO_QSS)

    win = PrototypeWindow(state=args.state)
    if args.size:
        w, h = (int(v) for v in args.size.lower().split("x"))
        win.resize(w, h)
    win.show()

    if args.shot:
        def grab():
            if win.isFullScreen():
                win.showFullScreen()
            win.grab().save(args.shot)
            print("[prototype] shot -> %s (%dx%d, backdrop=%s)"
                  % (args.shot, win.width(), win.height(), win._backdrop))
            app.quit()
        QTimer.singleShot(400, grab)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
