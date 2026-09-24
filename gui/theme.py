# -*- coding: utf-8 -*-
"""
LiveLens 深色主题（UI 改版）

设计来源
--------
视觉语言同时参考两个项目，但只取「原则」，不引入任何运行时依赖：

    QtModernRedux  —— 沉稳的工具风：低对比边框、克制的强调色、
                      单一调色板驱动全部控件、几何尺寸集中管理。
    QFluentWidgets —— 现代交互细节：hover / pressed 靠透明度微调而不是换色、
                      卡片面板 = 1px 边框 + 顶部微亮边、
                      明确的状态色（success / warning / critical）。

落地方式
--------
- 所有颜色集中在 :class:`Palette`（**带类型注解的常量**），QSS 里写 ``{TOKEN}``
  占位符，:func:`build_stylesheet` 用 ``__annotations__`` 做一次替换。
  换肤 = 改这一个类，不需要在 QSS 里翻十六进制。
  （该「注解即占位符表」的手法来自 QtModernRedux 的 apl_style。）
- 同一套颜色同时喂给 :class:`QPalette`，让 QSS 覆盖不到的绘制
  （文本光标、占位符文字、原生控件内部）保持一致。

层级约定
--------
    APP_BG          窗口底色（最暗）
      └ DOCK_BG     Dock 面板
          └ DOCK_HEADER_BG   Dock 标题栏
      └ ELEVATED   面板上的控件（按钮 / 下拉框）
          └ HOVER / PRESSED  交互反馈
"""

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

__all__ = [
    "Palette",
    "Metrics",
    "build_stylesheet",
    "apply_theme",
    "configure_high_dpi",
    "tone_color",
    "enable_backdrop",
    "apply_dialog_acrylic",
]


# ======================================================================
# 调色板：深蓝 / 蓝灰、低对比边框、蓝色主强调色
# ======================================================================
class Palette:
    """全局颜色常量（注解表即 QSS 占位符表，不要删注解）。"""

    # ---- 背景层级 ----
    APP_BG: str = "#141518"          # 窗口底色
    CHROME_BG: str = "#1D1E22"       # 标题栏 / 状态栏
    DOCK_BG: str = "#1D1E22"         # Dock 面板
    DOCK_HEADER_BG: str = "#1D1E22"  # Dock 面板标题栏
    PANEL_BG: str = "#0B0C0E"        # 预览区（最暗，让画面成为焦点）
    POPUP_BG: str = "#1D1E22"        # 下拉弹层
    INPUT_BG: str = "#17181C"        # 输入框
    ELEVATED: str = "#24262B"        # 面板之上的控件
    HOVER: str = "#2A2C31"
    PRESSED: str = "#101114"
    DISABLED_BG: str = "#191A1E"
    CHIP_BG: str = "transparent"
    #: 「框架层」（标题栏/地址栏/性能栏）：半透明深色 —— 让 Mica 只作为
    #: 很淡的底纹，避免系统亮色主题下出现浅色标题栏与深色主体割裂。
    CHROME: str = "rgba(20, 21, 24, 0.72)"
    #: 视频井：最暗的一层，让画面成为绝对视觉中心
    WELL: str = "#0B0C0E"
    #: 浮层遮罩：盖在视频井之上的半透明深色（未激活遮罩用它压暗画面）
    SCRIM: str = "rgba(11, 12, 14, 0.88)"
    #: 主操作（浅色实心按钮）上的文字色
    ACCENT_INK: str = "#141518"

    # ---- 边框：低对比，1px + 顶部微亮边区分层级 ----
    BORDER: str = "#303137"
    BORDER_TOP: str = "#3A3B41"
    BORDER_SOFT: str = "#26272C"
    BORDER_STRONG: str = "#3A3B41"

    # ---- 文本 ----
    TEXT: str = "#F0F0F2"
    TEXT_DIM: str = "#90939B"
    TEXT_MUTED: str = "#6A6D74"
    TEXT_DISABLED: str = "#4C4E54"

    # ---- 强调色（蓝）----
    ACCENT: str = "#D9DBE0"
    ACCENT_HOVER: str = "#E6E8EC"
    ACCENT_PRESSED: str = "#C7C9CF"
    ACCENT_TEXT: str = "#E8E9ED"
    ACCENT_DIM: str = "rgba(255, 255, 255, 0.08)"
    ACCENT_EDGE: str = "rgba(255, 255, 255, 0.18)"

    # ---- 状态色 ----
    OK_TEXT: str = "#83B69A"
    OK_BG: str = "rgba(131, 182, 154, 0.12)"
    OK_BORDER: str = "rgba(131, 182, 154, 0.28)"
    WARN_TEXT: str = "#C8A96A"
    WARN_BG: str = "rgba(200, 169, 106, 0.12)"
    WARN_BORDER: str = "rgba(200, 169, 106, 0.28)"
    DANGER: str = "#B4554E"
    DANGER_TEXT: str = "#D08A82"
    DANGER_BG: str = "rgba(180, 85, 78, 0.12)"
    DANGER_BORDER: str = "rgba(180, 85, 78, 0.28)"
    DANGER_HOVER: str = "#C4736B"

    # ---- 字体 ----
    FONT_FAMILY: str = '"Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei"'
    MONO_FAMILY: str = 'Consolas, "Cascadia Mono", monospace'
    FONT_SIZE: str = "12px"
    FONT_SIZE_SMALL: str = "11px"


# ======================================================================
# 几何尺寸：集中管理，避免 QSS 与 Python 里的数字各说各话
# ======================================================================
class Metrics:
    """控件几何常量。"""

    TITLEBAR_HEIGHT: int = 38       # 自定义标题栏高度
    WINDOW_BUTTON_WIDTH: int = 46   # 标题栏窗口按钮宽度
    ICON_BUTTON_SIZE: int = 28      # 标题栏操作区的紧凑图标按钮边长
    DOCK_HEADER_HEIGHT: int = 30
    DOCK_WIDTH_LEFT: int = 268      # 左侧配置 Dock
    DOCK_WIDTH_RIGHT: int = 276     # 右侧 OCR Dock
    DOCK_MIN_WIDTH: int = 224
    DOCK_MAX_WIDTH: int = 380
    SPLITTER_HANDLE: int = 8
    RESIZE_MARGIN: int = 5          # 无边框窗口的边角拖拽热区
    RADIUS: int = 6
    #: 右侧 OCR 面板宽度（Studio Minimal 工作台）。DOCK_WIDTH_RIGHT 是
    #: 旧三栏布局的常量，保留以免影响其它引用。
    OCR_PANEL_WIDTH: int = 310
    OCR_PANEL_WIDTH_MIN: int = 260
    OCR_PANEL_WIDTH_MAX: int = 420
    WINDOW_MIN_WIDTH: int = 1100
    #: 面板收起后允许的最小窗口宽度（窄窗口优先保留视频区）
    WINDOW_MIN_WIDTH_NARROW: int = 880
    WINDOW_MIN_HEIGHT: int = 660


#: 状态语气 → 圆点颜色。用于状态下拉/芯片，保证「明显但克制」。
_TONE_DOT = {
    "neutral": "#6E7C8C",
    "ok": "#3FB950",
    "warn": "#D29922",
    "error": "#E5534B",
    "accent": "#3D8BFD",
}


def tone_color(tone: str) -> str:
    """取状态语气对应的圆点颜色。"""
    return _TONE_DOT.get(tone, _TONE_DOT["neutral"])


# ======================================================================
# QSS
# ======================================================================
_QSS = """
/* ==================== 基础 ==================== */
QWidget {
    font-family: {FONT_FAMILY};
    font-size: {FONT_SIZE};
    color: {TEXT};
}

QMainWindow, QDialog {
    background-color: {APP_BG};
}

/* 无边框窗口的 1px 外沿（底部由状态栏补齐） */
#windowRoot {
    background-color: {APP_BG};
    border: none;
}

/* Mica 生效时整窗透明：标题栏/地址栏/性能栏用 CHROME 叠一层深色 */
#windowRoot[mica="true"] {
    background-color: transparent;
}

QToolTip {
    background-color: {POPUP_BG};
    color: {TEXT};
    border: 1px solid {BORDER_STRONG};
    border-radius: 4px;
    padding: 4px 8px;
    font-size: {FONT_SIZE_SMALL};
}

/* ==================== 标题栏 ==================== */
#titleBar {
    background-color: {CHROME};
    border-bottom: 1px solid {BORDER_SOFT};
}

#appLogo {
    background-color: {ACCENT};
    border-radius: 3px;
}

#appTitle {
    font-size: 13px;
    font-weight: 600;
    color: {TEXT};
}

#appSubtitle {
    font-size: {FONT_SIZE_SMALL};
    color: {TEXT_MUTED};
}

/* 标题栏操作区：紧凑图标按钮（自绘字形，QSS 只管背景与反馈）。
   必须显式清掉 QPushButton 基类的 min-height 与 padding —— QSS 的 min-height
   优先于 setFixedSize()，不清掉按钮会变成 28×34，与窗口按钮不在一条基线上。 */
#titleIconBtn {
    background: transparent;
    border: 1px solid transparent;
    border-radius: {RADIUS}px;
    padding: 0px;
    min-height: 0px;
    min-width: 0px;
}

#titleIconBtn:hover {
    background-color: {HOVER};
    border-color: {BORDER_SOFT};
}

#titleIconBtn:pressed {
    background-color: {PRESSED};
}

/* 「授权」是操作区唯一被强调的入口 */
#titleIconBtn[variant="primary"] {
    border-color: {BORDER};
    background-color: {ELEVATED};
}

#titleIconBtn[variant="primary"]:hover {
    background-color: {HOVER};
    border-color: {BORDER_STRONG};
}

#winBtn {
    background: transparent;
    border: none;
    border-radius: 0px;
}

#winBtn:hover {
    background-color: {HOVER};
}

#winBtn:pressed {
    background-color: {PRESSED};
}

#winBtnClose {
    background: transparent;
    border: none;
    border-radius: 0px;
}

#winBtnClose:hover {
    background-color: {DANGER};
}

#winBtnClose:pressed {
    background-color: {DANGER_HOVER};
}

/* ==================== Dock 面板 ==================== */
#dockPanel {
    background-color: {DOCK_BG};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
}

#dockHeader {
    background-color: {DOCK_HEADER_BG};
    border-bottom: 1px solid {BORDER};
    border-top-left-radius: {RADIUS}px;
    border-top-right-radius: {RADIUS}px;
}

#dockTitleBar {
    background-color: {ACCENT};
    border-radius: 1px;
}

#dockTitle {
    font-size: {FONT_SIZE_SMALL};
    font-weight: 600;
    color: {TEXT_DIM};
}

/* Dock 内的小节标题 / 字段说明 */
#sectionLabel {
    font-size: {FONT_SIZE_SMALL};
    font-weight: 600;
    color: {TEXT_MUTED};
}

#fieldLabel {
    font-size: {FONT_SIZE_SMALL};
    color: {TEXT_MUTED};
}

#valueLabel {
    font-size: {FONT_SIZE};
    color: {TEXT};
}

#captionLabel {
    font-size: {FONT_SIZE_SMALL};
    color: {TEXT_MUTED};
}

#hLine {
    background-color: {BORDER_SOFT};
    border: none;
    min-height: 1px;
    max-height: 1px;
}

/* ==================== 按钮 ==================== */
QPushButton {
    background-color: {ELEVATED};
    border: 1px solid {BORDER};
    border-top: 1px solid {BORDER_TOP};
    border-radius: 5px;
    color: {TEXT};
    padding: 4px 12px;
    min-height: 24px;
}

QPushButton:hover {
    background-color: {HOVER};
    border-color: {BORDER_STRONG};
}

QPushButton:pressed {
    background-color: {PRESSED};
    color: {TEXT_DIM};
}

QPushButton:disabled {
    background-color: {DISABLED_BG};
    color: {TEXT_DISABLED};
    border-color: {BORDER_SOFT};
    border-top-color: {BORDER_SOFT};
}

/* 主操作：蓝色实心 */
QPushButton[variant="primary"] {
    background-color: {ACCENT};
    border: 1px solid {ACCENT};
    color: {ACCENT_INK};
    font-weight: 600;
}

QPushButton[variant="primary"]:hover {
    background-color: {ACCENT_HOVER};
    border-color: {ACCENT_HOVER};
}

QPushButton[variant="primary"]:pressed {
    background-color: {ACCENT_PRESSED};
    border-color: {ACCENT_PRESSED};
    color: {ACCENT_INK};
}

QPushButton[variant="primary"]:disabled {
    background-color: {DISABLED_BG};
    border-color: {BORDER_SOFT};
    color: {TEXT_DISABLED};
}

/* 危险 / 停止态 */
QPushButton[variant="danger"] {
    background-color: {DANGER_BG};
    border: 1px solid {DANGER_BORDER};
    color: {DANGER_TEXT};
    font-weight: 600;
}

QPushButton[variant="danger"]:hover {
    background-color: {DANGER};
    border-color: {DANGER};
    color: #FFFFFF;
}

QPushButton[variant="danger"]:pressed {
    background-color: {DANGER_HOVER};
    border-color: {DANGER_HOVER};
    color: #FFFFFF;
}

QPushButton[variant="danger"]:disabled {
    background-color: {DISABLED_BG};
    border-color: {BORDER_SOFT};
    color: {TEXT_DISABLED};
}

/* 状态栏里的紧凑按钮 */
#statusButton {
    padding: 2px 10px;
    min-height: 20px;
    font-size: {FONT_SIZE_SMALL};
}

/* ==================== 输入框 ==================== */
QLineEdit {
    background-color: {INPUT_BG};
    border: 1px solid {BORDER};
    border-bottom: 1px solid {BORDER_STRONG};
    border-radius: 5px;
    color: {TEXT};
    padding: 3px 9px;
    min-height: 24px;
    selection-background-color: {ACCENT};
    selection-color: #FFFFFF;
}

QLineEdit:hover {
    border-color: {BORDER_STRONG};
}

/* 聚焦：整圈变强调色，比只换底色更易读 */
QLineEdit:focus {
    border: 1px solid {ACCENT};
    background-color: {INPUT_BG};
}

QLineEdit:disabled {
    background-color: {DISABLED_BG};
    color: {TEXT_DISABLED};
    border-color: {BORDER_SOFT};
}

/* ==================== 下拉框 ==================== */
QComboBox {
    background-color: {ELEVATED};
    border: 1px solid {BORDER};
    border-top: 1px solid {BORDER_TOP};
    border-radius: 5px;
    color: {TEXT};
    padding: 3px 26px 3px 9px;
    min-height: 24px;
}

QComboBox:hover {
    background-color: {HOVER};
    border-color: {BORDER_STRONG};
}

QComboBox:focus {
    border-color: {ACCENT};
}

QComboBox:on {
    border-color: {ACCENT};
    background-color: {HOVER};
}

QComboBox:disabled {
    background-color: {DISABLED_BG};
    color: {TEXT_DISABLED};
    border-color: {BORDER_SOFT};
}

QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: 22px;
    border: none;
    background: transparent;
}

QComboBox::down-arrow {
    image: none;
    width: 0;
    height: 0;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 4px solid {TEXT_DIM};
    margin-right: 9px;
}

QComboBox::down-arrow:disabled {
    border-top-color: {TEXT_DISABLED};
}

QComboBox QAbstractItemView {
    background-color: {POPUP_BG};
    border: 1px solid {BORDER_STRONG};
    border-radius: {RADIUS}px;
    color: {TEXT};
    padding: 4px;
    outline: none;
    selection-background-color: {ACCENT_DIM};
    selection-color: {TEXT};
}

QComboBox QAbstractItemView::item {
    min-height: 24px;
    padding: 3px 8px;
    border-radius: 4px;
}

QComboBox QAbstractItemView::item:hover {
    background-color: {HOVER};
}

QComboBox QAbstractItemView::item:selected {
    background-color: {ACCENT_DIM};
    color: {TEXT};
}

/* ==================== 列表（场景 / 配置） ==================== */
QListWidget {
    background: transparent;
    border: none;
    outline: none;
}

QListWidget::item {
    border-radius: 5px;
    padding: 6px 8px;
    margin: 1px 0px;
    color: {TEXT_DIM};
}

QListWidget::item:hover {
    background-color: {HOVER};
    color: {TEXT};
}

QListWidget::item:selected {
    background-color: {ACCENT_DIM};
    color: {TEXT};
}

/* ==================== 滚动条 ==================== */
QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 2px;
}

QScrollBar::handle:vertical {
    background-color: {BORDER_STRONG};
    border-radius: 3px;
    min-height: 24px;
}

QScrollBar::handle:vertical:hover {
    background-color: {TEXT_DISABLED};
}

QScrollBar:horizontal {
    background: transparent;
    height: 10px;
    margin: 2px;
}

QScrollBar::handle:horizontal {
    background-color: {BORDER_STRONG};
    border-radius: 3px;
    min-width: 24px;
}

QScrollBar::add-line, QScrollBar::sub-line {
    width: 0px;
    height: 0px;
    background: none;
    border: none;
}

QScrollBar::add-page, QScrollBar::sub-page {
    background: transparent;
}

/* ==================== 分隔条 ==================== */
QSplitter::handle {
    background-color: transparent;
}

QSplitter::handle:horizontal {
    width: {SPLITTER_HANDLE}px;
}

QSplitter::handle:vertical {
    height: {SPLITTER_HANDLE}px;
}

QSplitter::handle:hover {
    background-color: {BORDER};
}

/* ==================== 状态栏 ==================== */
QStatusBar {
    background-color: {CHROME_BG};
    color: {TEXT_MUTED};
    border-top: 1px solid {BORDER};
    border-left: 1px solid {BORDER_STRONG};
    border-right: 1px solid {BORDER_STRONG};
    border-bottom: 1px solid {BORDER_STRONG};
}

/* 性能栏内容高度：默认的 18px 让四组指标挤成一条难以分辨的长串，
   这里给到 ~24px，让组标签、组间分隔线与读数各自有呼吸空间。
   QStatusBar 的左侧瞬时消息区（诊断 / 错误文案）共用这条高度。 */
QStatusBar QLabel, QStatusBar QPushButton {
    min-height: 22px;
}

QStatusBar::item {
    border: none;
}

QStatusBar QLabel {
    color: {TEXT_MUTED};
    font-size: {FONT_SIZE_SMALL};
}

/* 状态芯片：等宽字体，读数不跳动 */
#metricChip {
    background-color: transparent;
    border: none;
    border-radius: 0px;
    padding: 0px 2px;
    color: {TEXT_DIM};
    font-family: {MONO_FAMILY};
    font-size: {FONT_SIZE_SMALL};
}

#metricChip[tone="ok"] {
    color: {OK_TEXT};
}

#metricChip[tone="warn"] {
    color: {WARN_TEXT};
}

#metricChip[tone="error"] {
    color: {DANGER_TEXT};
}

#metricChip[tone="accent"] {
    color: {ACCENT_TEXT};
}

/* ==================== 预览区 ==================== */
#previewFrame {
    background-color: {WELL};
    border: none;
}

#previewOverlay {
    color: {TEXT_MUTED};
    font-size: 14px;
    background: transparent;
    padding: 20px;
}

/* 未激活遮罩（Phase UI-3）：品牌呼吸 + 激活入口。
   刻意用半透明遮罩而不是纯色：未连接时底下是空的视频井，有画面时也能
   让用户看见「程序在工作，只是还没授权」。 */
#activationOverlay {
    background-color: {SCRIM};
    border: none;
}

#activationBrand {
    font-size: 26px;
    font-weight: 600;
    color: {TEXT};
    letter-spacing: 3px;
}

#activationPrompt {
    font-size: 14px;
    color: {TEXT_DIM};
}

#activationHint {
    font-size: 11px;
    color: {TEXT_MUTED};
}

#copyToast {
    background-color: {POPUP_BG};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    color: {OK_TEXT};
    padding: 8px 18px;
    font-weight: 600;
    font-size: 13px;
}

/* OCR 识别结果 */
#resultBox {
    background-color: transparent;
    border-top: 1px solid {BORDER_SOFT};
    border-bottom: 1px solid {BORDER_SOFT};
    border-left: none;
    border-right: none;
    border-radius: 0px;
    color: {TEXT};
    font-family: {MONO_FAMILY};
    font-size: 30px;
    font-weight: 600;
    letter-spacing: 6px;
    padding: 14px 4px;
}

#resultBox[filled="true"] {
    color: {TEXT};
    border-color: {BORDER};
    background-color: transparent;
}


/* ==================== Studio Minimal：新布局专用 ==================== */
/* 顶部地址栏（框架层） */
#addressBar {
    background-color: {CHROME};
    border-bottom: 1px solid {BORDER};
}

#addressBar QLineEdit {
    background-color: {INPUT_BG};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    padding: 6px 10px;
    color: {TEXT};
}

#addressBar QLineEdit:focus {
    border-color: {TEXT_DISABLED};
}

#addressBar QComboBox {
    background-color: {INPUT_BG};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    padding: 5px 8px;
}

/* 历史二级入口（⋯）：与地址栏其余控件同高、同底色，但更窄更安静 */
#historyMenuBtn {
    background-color: {INPUT_BG};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    color: {TEXT_DIM};
    padding: 0px;
    min-height: 0px;
    font-size: 14px;
}

#historyMenuBtn:hover {
    background-color: {HOVER};
    color: {TEXT};
    border-color: {BORDER_STRONG};
}

#historyMenuBtn::menu-indicator {
    image: none;
    width: 0px;
}

/* 视频下方工具栏（次级表面 + 顶部细线） */
#toolStrip {
    background-color: {DOCK_BG};
    border-top: 1px solid {BORDER_SOFT};
}

/* 右侧 OCR 面板：一条左细线分隔，不用卡片边框 */
#ocrPanel {
    background-color: {DOCK_BG};
    border-left: 1px solid {BORDER};
}

/* 面板内小节标题右侧的窄操作入口（如「识别区域」旁的「说明」）。
   与 sectionLabel 同一字号层级，不能长得像一个普通按钮，
   否则会跟下方的「选择识别区域」抢注意力。
   同样要显式清掉 QPushButton 基类的 min-height（它的优先级高于
   setFixedHeight，不清掉按钮会被撑到 34px 高）。 */
#sectionAction {
    background: transparent;
    border: 1px solid transparent;
    border-radius: 4px;
    color: {TEXT_MUTED};
    font-size: {FONT_SIZE_SMALL};
    padding: 0px 6px;
    min-width: 0px;
    /* 固定 18px：既跟小节标题同一视觉层级，又留出可点中的高度。
       在 QSS 里钉死高度而不是 setFixedHeight —— QSS 的 min/max-height
       优先级更高，混用会得到 13px 这种"看起来对、点起来太小"的结果。 */
    min-height: 18px;
    max-height: 18px;
}

#sectionAction:hover {
    background-color: {HOVER};
    border-color: {BORDER_SOFT};
    color: {TEXT};
}

#sectionAction:pressed {
    background-color: {PRESSED};
}

#ocrPanel QLabel {
    color: {TEXT_DIM};
}

/* 底部性能栏：与标题栏同属框架层 */
#perfStrip {
    background-color: {CHROME};
    border-top: 1px solid {BORDER};
}

/* 性能栏组间分隔：一条很短的竖线，避免四组指标连成一行密集字符串 */
#perfDivider {
    background-color: {BORDER};
    border: none;
    margin: 4px 8px;
}

#perfGroup {
    color: {TEXT_MUTED};
    font-size: {FONT_SIZE_SMALL};
}

#perfValue {
    color: {TEXT_DIM};
    font-size: {FONT_SIZE_SMALL};
    font-family: {MONO_FAMILY};
}

/* 会话识别历史：无边框列表，选中用明度差 */
/* 本次会话历史：面板里唯一可伸缩的区域，用一层很浅的内嵌底色把它标成
   "一块有边界的列表"，这样它在没有记录时是一处明确的空状态，而不是一条空白。 */
#sessionList {
    background-color: {INPUT_BG};
    border: 1px solid {BORDER_SOFT};
    border-radius: {RADIUS}px;
    color: {TEXT_DIM};
    font-family: {MONO_FAMILY};
    font-size: 12px;
    padding: 4px;
    outline: none;
}

#sessionList::item {
    padding: 3px 6px;
    border-radius: 4px;
}

#sessionList::item:hover {
    background-color: {HOVER};
    color: {TEXT};
}

#sessionList::item:selected {
    background-color: {ACCENT_DIM};
    color: {TEXT};
}

/* 场景/配置列表（右侧面板内的游戏选择） */
#sceneList {
    background: transparent;
    border: none;
    color: {TEXT_DIM};
    outline: none;
}

#sceneList::item {
    padding: 4px 6px;
    border-radius: 4px;
}

#sceneList::item:hover {
    background-color: {HOVER};
    color: {TEXT};
}

#sceneList::item:selected {
    background-color: {ACCENT_DIM};
    color: {TEXT};
}

/* 面板收起/展开 */
#collapseBtn, #expandBtn {
    color: {TEXT_MUTED};
    background: transparent;
    border: none;
    border-radius: {RADIUS}px;
    padding: 2px 6px;
}

#collapseBtn:hover, #expandBtn:hover {
    background-color: {HOVER};
    color: {TEXT};
}

/* 分隔线控件：细分隔线代替卡片边框 */
#hLine {
    background-color: {BORDER_SOFT};
    border: none;
}

QSplitter::handle {
    background: transparent;
}

/* ==================== 对话框 ==================== */
QMessageBox {
    background-color: {DOCK_BG};
}

QMessageBox QLabel {
    color: {TEXT};
    font-size: 13px;
}

QMessageBox QPushButton {
    min-width: 78px;
    min-height: 26px;
}

QMenu {
    background-color: {POPUP_BG};
    border: 1px solid {BORDER_STRONG};
    border-radius: {RADIUS}px;
    padding: 4px;
    color: {TEXT};
}

QMenu::item {
    padding: 5px 18px;
    border-radius: 4px;
}

QMenu::item:selected {
    background-color: {ACCENT_DIM};
    color: {TEXT};
}

/* 授权状态文案：默认低调，异常时由 tone 属性提升对比 */
#accessLabel {
    font-size: {FONT_SIZE_SMALL};
    color: {TEXT_MUTED};
    padding-right: 4px;
}

#accessLabel[tone="ok"] {
    color: {OK_TEXT};
}

#accessLabel[tone="warn"] {
    color: {WARN_TEXT};
}

#accessLabel[tone="error"] {
    color: {DANGER_TEXT};
}
"""


def build_stylesheet() -> str:
    """把 :class:`Palette` / :class:`Metrics` 的常量填进 QSS 占位符。

    走 ``cls.__annotations__`` 而不是 ``vars(cls)["__annotations__"]``：
    Python 3.14 起注解是惰性求值的（PEP 649），不再预先写进类字典。
    """
    qss = _QSS
    for source in (Palette, Metrics):
        for token, _type in getattr(source, "__annotations__", {}).items():
            qss = qss.replace("{%s}" % token, str(getattr(source, token)))
    return qss


def _build_palette() -> QPalette:
    """QSS 覆盖不到的绘制（光标、占位文字、原生弹层）走 QPalette。"""
    p = QPalette()
    p.setColor(QPalette.Window, QColor(Palette.APP_BG))
    p.setColor(QPalette.WindowText, QColor(Palette.TEXT))
    p.setColor(QPalette.Base, QColor(Palette.INPUT_BG))
    p.setColor(QPalette.AlternateBase, QColor(Palette.DOCK_BG))
    p.setColor(QPalette.Text, QColor(Palette.TEXT))
    p.setColor(QPalette.PlaceholderText, QColor(Palette.TEXT_MUTED))
    p.setColor(QPalette.Button, QColor(Palette.ELEVATED))
    p.setColor(QPalette.ButtonText, QColor(Palette.TEXT))
    p.setColor(QPalette.BrightText, QColor("#FFFFFF"))
    p.setColor(QPalette.Highlight, QColor(Palette.ACCENT))
    p.setColor(QPalette.HighlightedText, QColor("#FFFFFF"))
    p.setColor(QPalette.ToolTipBase, QColor(Palette.POPUP_BG))
    p.setColor(QPalette.ToolTipText, QColor(Palette.TEXT))
    p.setColor(QPalette.Link, QColor(Palette.ACCENT))
    p.setColor(QPalette.Mid, QColor(Palette.BORDER))
    p.setColor(QPalette.Dark, QColor(Palette.BORDER_SOFT))
    p.setColor(QPalette.Shadow, QColor("#000000"))

    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, QColor(Palette.TEXT_DISABLED))
    p.setColor(QPalette.Disabled, QPalette.Base, QColor(Palette.DISABLED_BG))
    p.setColor(QPalette.Disabled, QPalette.Button, QColor(Palette.DISABLED_BG))
    p.setColor(QPalette.Disabled, QPalette.Highlight, QColor(Palette.BORDER_STRONG))
    return p


def configure_high_dpi() -> None:
    """高 DPI 配置（必须在 QApplication 创建**之前**调用）。

    Qt6 默认已开启高 DPI 缩放，这里只把缩放取整策略改成 PassThrough，
    避免 125% / 150% 缩放下 1px 边框被取整成 2px 或消失。
    """
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication

    try:
        QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
    except Exception:  # pragma: no cover - 老版本 Qt 没有该 API
        pass


def apply_theme(app: QApplication | None = None) -> None:
    """把主题应用到整个应用（幂等，可重复调用）。

    刻意装在 QApplication 上而不是 MainWindow 上：
    QDialog / QMessageBox 是独立顶层窗口，只有应用级样式表才覆盖得到。
    """
    if app is None:
        app = QApplication.instance()
    if app is None:
        return

    app.setStyle("Fusion")
    app.setPalette(_build_palette())
    app.setStyleSheet(build_stylesheet())


# ======================================================================
# Windows 11 系统背景（Mica / Acrylic）
# ======================================================================
def _win11_22h2_or_newer() -> bool:
    """Mica 需要 Windows 11 22H2（build 22621）及以上。"""
    import os
    import sys

    if os.name != "nt":
        return False
    try:
        return sys.getwindowsversion().build >= 22621
    except Exception:  # noqa: BLE001
        return False


def enable_backdrop(hwnd: int, kind: str = "mica") -> bool:
    """给窗口启用 DWM 系统背景（mica / acrylic），失败返回 False。

    这不是 PySide6 的能力，需要 Win32 的 DwmSetWindowAttribute
    （38=系统背景类型 / 20=深色 / 33=圆角）。任何失败都返回 False，
    由调用方退回不透明深色 —— 不做视觉上的欺骗。
    """
    if not _win11_22h2_or_newer():
        return False
    backdrop = {"mica": 2, "acrylic": 3}.get(kind)
    if backdrop is None:
        return False
    try:
        import ctypes

        dwm = ctypes.windll.dwmapi
        # 顺序：先声明深色，再设背景类型（反之 DWM 会先按浅色合成且不重算）
        dark = ctypes.c_int(1)
        dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), ctypes.sizeof(dark))
        value = ctypes.c_int(backdrop)
        if dwm.DwmSetWindowAttribute(hwnd, 38, ctypes.byref(value),
                                     ctypes.sizeof(value)) != 0:
            return False
        corner = ctypes.c_int(2)
        dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(corner), ctypes.sizeof(corner))
        return True
    except Exception:  # noqa: BLE001
        return False


def apply_dialog_acrylic(dialog) -> bool:
    """给对话框启用 Acrylic 系统背景（Phase UI-3）。

    「适度使用」的落地方式刻意保守：**只调用 DWM**，不动 QSS、不动窗口属性。
    保留原生边框，因此拖动 / 关闭 / 阴影 / 缩放行为与不做这件事时完全一致；
    对话框本体仍由 ``QDialog { background-color: APP_BG }`` 绘制。

    任何失败（非 Win11 22H2+ / 无 dwmapi / DWM 返回非 0）都返回 False，
    此时什么都【没有】被改过 —— 即需求里的「回退为不透明深色」。

    刻意不使用 ``WA_TranslucentBackground`` 把本体挖空：该属性必须在窗口显示
    之前设置才生效，而 DWM 句柄又必须等显示之后才有，两者顺序天然冲突；
    强行用「构造期先设透明属性」去凑，会让对话框在 Acrylic 不可用的机器上
    也变成无边框无阴影的半透明窗口 —— 那不是回退，是把失败路径做得更糟。

    :return: True 表示 DWM 已接受 Acrylic 设置。
    """
    if dialog is None:
        return False
    try:
        return enable_backdrop(int(dialog.winId()), "acrylic")
    except Exception:  # noqa: BLE001 - 任何失败都退回不透明深色
        return False
