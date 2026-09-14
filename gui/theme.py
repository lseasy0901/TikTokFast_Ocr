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
]


# ======================================================================
# 调色板：深蓝 / 蓝灰、低对比边框、蓝色主强调色
# ======================================================================
class Palette:
    """全局颜色常量（注解表即 QSS 占位符表，不要删注解）。"""

    # ---- 背景层级 ----
    APP_BG: str = "#12171E"          # 窗口底色
    CHROME_BG: str = "#181E27"       # 标题栏 / 状态栏
    DOCK_BG: str = "#1B222B"         # Dock 面板
    DOCK_HEADER_BG: str = "#222B36"  # Dock 面板标题栏
    PANEL_BG: str = "#0D1116"        # 预览区（最暗，让画面成为焦点）
    POPUP_BG: str = "#1F2732"        # 下拉弹层
    INPUT_BG: str = "#0F141A"        # 输入框
    ELEVATED: str = "#242E3A"        # 面板之上的控件
    HOVER: str = "#2C3846"
    PRESSED: str = "#1C242E"
    DISABLED_BG: str = "#1A212A"
    CHIP_BG: str = "#1A212A"

    # ---- 边框：低对比，1px + 顶部微亮边区分层级 ----
    BORDER: str = "#2A3441"
    BORDER_TOP: str = "#35414F"
    BORDER_SOFT: str = "#212932"
    BORDER_STRONG: str = "#3A4757"

    # ---- 文本 ----
    TEXT: str = "#E6ECF3"
    TEXT_DIM: str = "#98A6B5"
    TEXT_MUTED: str = "#6E7C8C"
    TEXT_DISABLED: str = "#4C5764"

    # ---- 强调色（蓝）----
    ACCENT: str = "#3D8BFD"
    ACCENT_HOVER: str = "#5C9EFF"
    ACCENT_PRESSED: str = "#2C6FD8"
    ACCENT_TEXT: str = "#6FB0FF"
    ACCENT_DIM: str = "rgba(61, 139, 253, 0.16)"
    ACCENT_EDGE: str = "rgba(61, 139, 253, 0.55)"

    # ---- 状态色 ----
    OK_TEXT: str = "#56D364"
    OK_BG: str = "rgba(63, 185, 80, 0.12)"
    OK_BORDER: str = "rgba(63, 185, 80, 0.35)"
    WARN_TEXT: str = "#E3B341"
    WARN_BG: str = "rgba(210, 153, 34, 0.12)"
    WARN_BORDER: str = "rgba(210, 153, 34, 0.35)"
    DANGER: str = "#E5534B"
    DANGER_TEXT: str = "#FF7B72"
    DANGER_BG: str = "rgba(229, 83, 75, 0.12)"
    DANGER_BORDER: str = "rgba(229, 83, 75, 0.35)"
    DANGER_HOVER: str = "#F0665E"

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
    DOCK_HEADER_HEIGHT: int = 30
    DOCK_WIDTH_LEFT: int = 268      # 左侧配置 Dock
    DOCK_WIDTH_RIGHT: int = 276     # 右侧 OCR Dock
    DOCK_MIN_WIDTH: int = 224
    DOCK_MAX_WIDTH: int = 380
    SPLITTER_HANDLE: int = 8
    RESIZE_MARGIN: int = 5          # 无边框窗口的边角拖拽热区
    RADIUS: int = 6
    WINDOW_MIN_WIDTH: int = 1100
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
    border: 1px solid {BORDER_STRONG};
    border-bottom: none;
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
    background-color: {CHROME_BG};
    border-bottom: 1px solid {BORDER};
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
    color: #FFFFFF;
    font-weight: 600;
}

QPushButton[variant="primary"]:hover {
    background-color: {ACCENT_HOVER};
    border-color: {ACCENT_HOVER};
}

QPushButton[variant="primary"]:pressed {
    background-color: {ACCENT_PRESSED};
    border-color: {ACCENT_PRESSED};
    color: #DCE9FF;
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

QStatusBar::item {
    border: none;
}

QStatusBar QLabel {
    color: {TEXT_MUTED};
    font-size: {FONT_SIZE_SMALL};
}

/* 状态芯片：等宽字体，读数不跳动 */
#metricChip {
    background-color: {CHIP_BG};
    border: 1px solid {BORDER_SOFT};
    border-radius: 4px;
    padding: 2px 8px;
    color: {TEXT_DIM};
    font-family: {MONO_FAMILY};
    font-size: {FONT_SIZE_SMALL};
}

#metricChip[tone="ok"] {
    color: {OK_TEXT};
    background-color: {OK_BG};
    border-color: {OK_BORDER};
}

#metricChip[tone="warn"] {
    color: {WARN_TEXT};
    background-color: {WARN_BG};
    border-color: {WARN_BORDER};
}

#metricChip[tone="error"] {
    color: {DANGER_TEXT};
    background-color: {DANGER_BG};
    border-color: {DANGER_BORDER};
}

#metricChip[tone="accent"] {
    color: {ACCENT_TEXT};
    background-color: {ACCENT_DIM};
    border-color: {ACCENT_EDGE};
}

/* ==================== 预览区 ==================== */
#previewFrame {
    background-color: {PANEL_BG};
    border: none;
}

#previewOverlay {
    color: {TEXT_MUTED};
    font-size: 14px;
    background: transparent;
    padding: 20px;
}

#copyToast {
    background-color: {OK_BG};
    border: 1px solid {OK_BORDER};
    border-radius: 5px;
    color: {OK_TEXT};
    padding: 8px 18px;
    font-weight: 600;
    font-size: 13px;
}

/* OCR 识别结果 */
#resultBox {
    background-color: {INPUT_BG};
    border: 1px solid {BORDER_SOFT};
    border-radius: 5px;
    color: {TEXT_DIM};
    font-family: {MONO_FAMILY};
    font-size: 14px;
    padding: 8px 10px;
}

#resultBox[filled="true"] {
    color: {ACCENT_TEXT};
    border-color: {ACCENT_EDGE};
    background-color: {ACCENT_DIM};
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
