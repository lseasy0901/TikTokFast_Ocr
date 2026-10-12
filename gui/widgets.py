# -*- coding: utf-8 -*-
"""
LiveLens 通用 UI 组件

本模块只放「与业务无关」的界面积木，全部基于 PySide6 原生控件 + theme 的 QSS：

    DockPanel            OBS 风格的停靠面板（标题栏 + 内容区）
    TitleBar             无边框窗口的自定义标题栏
    WindowButton         标题栏的最小化 / 最大化 / 关闭按钮（自绘字形）
    FramelessController  无边框窗口的拖动与边角缩放
    set_tone             状态语气（neutral / ok / warn / error / accent）切换

刻意不引入第三方 UI 框架：以上组件用原生 QWidget + QSS 即可达到目标视觉，
避免为了几个控件把整个 FluentWidgets 变成运行时依赖。
"""

import math

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QRectF, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from gui import theme
from gui.theme import Metrics, Palette

__all__ = [
    "DockPanel",
    "TitleBar",
    "WindowButton",
    "IconButton",
    "InfoPopup",
    "icon_pixmap",
    "FramelessController",
    "OverlayHost",
    "set_property",
    "set_tone",
    "enable_mouse_tracking",
    "h_line",
    "open_external_url",
]


def open_external_url(url: str, parent=None) -> bool:
    """用**系统默认浏览器**打开外部链接（不在软件内嵌网页）。

    走 Qt 的 ``QDesktopServices.openUrl``（Windows 上即 ShellExecute），
    它返回布尔值：仅在系统确认接管时才算成功。**失败时弹出明确提示**并把
    链接原文给出来（便于手动打开），绝不静默假报成功。

    :return: True = 已交给系统浏览器；False = 打开失败（已提示用户）
    """
    if not url:
        return False
    ok = False
    try:
        ok = bool(QDesktopServices.openUrl(QUrl(url)))
    except Exception:  # noqa: BLE001 - 平台异常一律按失败处理并提示
        ok = False
    if not ok:
        QMessageBox.warning(
            parent, "无法打开浏览器",
            "无法调用系统默认浏览器打开购买链接。\n\n"
            "请手动在浏览器中访问：\n%s" % url,
        )
    return ok


# ======================================================================
# 状态语气
# ======================================================================
def set_property(widget: QWidget, name: str, value) -> None:
    """设置动态属性并让样式表重新生效（对应 QSS 里的 ``[name="value"]``）。

    只改动态属性不会自动重绘，必须 unpolish/polish 一次——
    这是 Qt 样式表刷新动态属性的标准做法。
    """
    if widget.property(name) == value:
        return
    widget.setProperty(name, value)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def set_tone(widget: QWidget, tone: str) -> None:
    """切换控件的状态语气（对应 QSS 里的 ``[tone="..."]`` 选择器）。"""
    set_property(widget, "tone", tone)


def chip_html(text: str, tone: str) -> str:
    """状态芯片文案：前置一个状态色圆点 + 文字。"""
    return (
        f'<span style="color:{theme.tone_color(tone)};">●</span>'
        f'&nbsp;{text}'
    )


def h_line() -> QFrame:
    """1px 分隔线。"""
    line = QFrame()
    line.setObjectName("hLine")
    line.setFrameShape(QFrame.NoFrame)
    line.setFixedHeight(1)
    return line


def enable_mouse_tracking(root: QWidget) -> None:
    """递归打开 mouseMove 事件。

    无边框窗口要靠鼠标位置判断是否落在边角热区，而 Qt 默认只在按下按键时
    才派发 mouseMove；这里统一打开，代价可忽略（事件只做一次几何判断）。
    """
    root.setMouseTracking(True)
    for child in root.findChildren(QWidget):
        child.setMouseTracking(True)


# ======================================================================
# Dock 面板
# ======================================================================
class DockPanel(QFrame):
    """OBS 风格停靠面板：一条标题栏 + 一块内容区。

    内容区通过 :attr:`body` 布局填充；标题栏右侧可用
    :meth:`add_header_widget` 挂状态芯片等小控件。
    """

    def __init__(self, title: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("dockPanel")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFrameShape(QFrame.NoFrame)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ---- 标题栏 ----
        header = QWidget()
        header.setObjectName("dockHeader")
        header.setAttribute(Qt.WA_StyledBackground, True)
        header.setFixedHeight(Metrics.DOCK_HEADER_HEIGHT)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(10, 0, 8, 0)
        header_layout.setSpacing(7)

        accent = QLabel()
        accent.setObjectName("dockTitleBar")
        accent.setFixedSize(3, 12)

        self._title = QLabel(title)
        self._title.setObjectName("dockTitle")

        header_layout.addWidget(accent)
        header_layout.addWidget(self._title)
        header_layout.addStretch(1)
        self._header_layout = header_layout

        outer.addWidget(header)

        # ---- 内容区 ----
        self._body = QWidget()
        self._body.setObjectName("dockBody")
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(10, 10, 10, 10)
        self._body_layout.setSpacing(9)
        outer.addWidget(self._body, 1)

    # -- 对外接口 ------------------------------------------------------
    @property
    def body(self) -> QVBoxLayout:
        """内容区布局。"""
        return self._body_layout

    def set_body_margins(self, left: int, top: int, right: int, bottom: int) -> None:
        self._body_layout.setContentsMargins(left, top, right, bottom)

    def set_body_spacing(self, spacing: int) -> None:
        self._body_layout.setSpacing(spacing)

    def add_header_widget(self, widget: QWidget) -> None:
        """把控件挂到标题栏右侧（在 stretch 之后）。"""
        self._header_layout.addWidget(widget)

    def set_title(self, title: str) -> None:
        self._title.setText(title)


# ======================================================================
# 浮层容器
# ======================================================================
class OverlayHost(QFrame):
    """承载浮层的容器：自动把注册过的浮层铺满自身。

    浮层不参与布局（不能挤压主内容），因此尺寸不会自己跟着容器走，
    必须由容器在 resize 时同步——否则窗口一变大，浮层就停在初始的 0×0。
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._overlays: list[QWidget] = []

    def add_overlay(self, widget: QWidget, interactive: bool = False) -> None:
        """登记一个「铺满容器」的浮层。

        :param interactive: 浮层是否接收鼠标事件。默认 False —— 纯展示浮层
            （连接提示、复制反馈）必须让点击穿透到底下的视频区。
            需要点击的浮层（未激活遮罩）必须传 True：``WA_TransparentForMouseEvents``
            不只作用于浮层自身，Qt 官方语义是「连同它的子控件一起」不再接收事件，
            因此设了它就等于把浮层里的按钮也一并作废。
        """
        widget.setParent(self)
        widget.setAttribute(Qt.WA_TransparentForMouseEvents, not interactive)
        self._overlays.append(widget)
        widget.setGeometry(self.rect())

    def _sync_overlays(self) -> None:
        rect = self.rect()
        for widget in self._overlays:
            widget.setGeometry(rect)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._sync_overlays()


# ======================================================================
# 标题栏按钮
# ======================================================================
class WindowButton(QPushButton):
    """标题栏窗口按钮：QSS 负责背景 / hover / pressed，本类只自绘字形。

    自绘字形而不是用 ``— □ ✕`` 字符，是为了避免不同系统字体下
    字形粗细、基线不一致（DPI 变化时尤其明显）。
    """

    MIN, MAX, RESTORE, CLOSE = "min", "max", "restore", "close"

    def __init__(self, kind: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._kind = kind
        self.setObjectName("winBtnClose" if kind == self.CLOSE else "winBtn")
        self.setFlat(True)
        self.setFixedSize(Metrics.WINDOW_BUTTON_WIDTH, Metrics.TITLEBAR_HEIGHT)
        self.setFocusPolicy(Qt.NoFocus)
        self.setCursor(Qt.ArrowCursor)
        self.setAttribute(Qt.WA_Hover, True)

        tips = {
            self.MIN: "最小化",
            self.MAX: "最大化",
            self.RESTORE: "还原",
            self.CLOSE: "关闭",
        }
        self.setToolTip(tips.get(kind, ""))

    def set_kind(self, kind: str) -> None:
        self._kind = kind
        self.setToolTip("还原" if kind == self.RESTORE else "最大化")
        self.update()

    # -- 绘制 ----------------------------------------------------------
    def _glyph_color(self) -> str:
        if self._kind == self.CLOSE and (self.underMouse() or self.isDown()):
            return "#FFFFFF"
        if self.underMouse():
            return Palette.TEXT
        return Palette.TEXT_DIM

    def paintEvent(self, event) -> None:  # noqa: N802
        # 先让样式表把背景 / hover / pressed 画完，再叠字形
        super().paintEvent(event)

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        pen = QPen(QColor(self._glyph_color()))
        pen.setWidthF(1.1)
        pen.setCapStyle(Qt.FlatCap)
        pen.setJoinStyle(Qt.MiterJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)

        cx = self.width() / 2.0
        cy = self.height() / 2.0
        s = 5.0

        if self._kind == self.MIN:
            painter.drawLine(QPointF(cx - s, cy), QPointF(cx + s, cy))
        elif self._kind == self.MAX:
            painter.drawRect(QRectF(cx - s, cy - s, 2 * s, 2 * s))
        elif self._kind == self.RESTORE:
            painter.drawRect(QRectF(cx - s, cy - s + 2, 2 * s - 2, 2 * s - 2))
            painter.drawRect(QRectF(cx - s + 2, cy - s, 2 * s - 2, 2 * s - 2))
        elif self._kind == self.CLOSE:
            painter.drawLine(QPointF(cx - s, cy - s), QPointF(cx + s, cy + s))
            painter.drawLine(QPointF(cx - s, cy + s), QPointF(cx + s, cy - s))
        painter.end()


def _draw_icon_glyph(painter, kind: str, cx: float, cy: float,
                     s: float) -> None:
    """把一个图标种类画在 (cx, cy)，`s` 是半尺寸基准。

    抽成模块函数是为了让"图标 + 文字"的普通按钮也能复用同一套画法
    （见 :func:`icon_pixmap`），而不是各画一份、慢慢画歪。
    """
    if kind == IconButton.HELP:
        # 问号用字形而不是画弧：小尺寸下自绘的弧线会被读成 "C"，
        # 而 "?" 是 ASCII，任何系统字体都有，且一眼可辨。
        side = s * 28.0 / 6.0        # 还原成 IconButton 控件边长
        font = painter.font()
        font.setPixelSize(int(side * 0.62))
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(QRectF(cx - side / 2, cy - side / 2, side, side),
                         Qt.AlignCenter, "?")
    elif kind == IconButton.LICENSE:
        # 钥匙：左侧细圆环 + 右侧长杆与两枚齿（杆要够长才不会被读成圆点）
        painter.drawEllipse(QRectF(cx - s, cy - 2.6, 5.2, 5.2))
        painter.drawLine(QPointF(cx - s + 5.2, cy), QPointF(cx + s, cy))
        painter.drawLine(QPointF(cx + s - 1.5, cy), QPointF(cx + s - 1.5, cy + 3.2))
        painter.drawLine(QPointF(cx + s - 4.5, cy), QPointF(cx + s - 4.5, cy + 3.2))
    elif kind == IconButton.CONTACT:
        # message-circle：圆角对话气泡 + 左下小尾巴
        half_w = s * 0.86
        half_h = s * 0.66
        bubble = QRectF(cx - half_w, cy - half_h - 0.5,
                        half_w * 2, half_h * 2)
        painter.drawRoundedRect(bubble, s * 0.40, s * 0.40)
        tail_x = cx - half_w * 0.42
        painter.drawLine(QPointF(tail_x, bubble.bottom()),
                         QPointF(tail_x - s * 0.12, bubble.bottom() + s * 0.40))
        painter.drawLine(QPointF(tail_x - s * 0.12, bubble.bottom() + s * 0.40),
                         QPointF(tail_x + s * 0.44, bubble.bottom()))
    elif kind == IconButton.SETTINGS:
        # 标准齿轮：一圈齿 + 齿圈 + 中心孔
        teeth = s * 0.86      # 齿顶半径
        ring = s * 0.52       # 齿根半径（也是齿圈半径）
        for i in range(8):
            angle = math.radians(i * 45.0)
            dx, dy = math.cos(angle), math.sin(angle)
            painter.drawLine(QPointF(cx + dx * ring, cy + dy * ring),
                             QPointF(cx + dx * teeth, cy + dy * teeth))
        painter.drawEllipse(QRectF(cx - ring, cy - ring, ring * 2, ring * 2))
        hole = s * 0.22
        painter.drawEllipse(QRectF(cx - hole, cy - hole, hole * 2, hole * 2))
    elif kind in (IconButton.FULLSCREEN, IconButton.FULLSCREEN_EXIT):
        # 四个直角括号；退出全屏时改画向内收拢的括号
        d = 2.5 if kind == IconButton.FULLSCREEN else 4.5
        a = s - 1.0
        for sx, sy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
            painter.drawLine(QPointF(cx + sx * a, cy + sy * a),
                             QPointF(cx + sx * a - sx * d, cy + sy * a))
            painter.drawLine(QPointF(cx + sx * a, cy + sy * a),
                             QPointF(cx + sx * a, cy + sy * a - sy * d))


def icon_pixmap(kind: str, size: int = 14,
                color: str = Palette.TEXT_DIM) -> QPixmap:
    """把某个图标渲染成 QPixmap，供"图标 + 文字"的普通 QPushButton 使用。

    尺寸按 IconButton 的比例（28px 对应 s=6）等比缩放，保证两处图标
    看起来是同一种粗细与大小。
    """
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)

    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(QColor(color))
    pen.setWidthF(1.2)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    _draw_icon_glyph(painter, kind, size / 2.0, size / 2.0, size * 6.0 / 28.0)
    painter.end()
    return pix


class IconButton(QPushButton):
    """标题栏操作区的紧凑图标按钮（帮助 / 授权 / 联系作者 / 设置 / 全屏）。

    与 :class:`WindowButton` 同一个理由自绘：图标是几何线条而不是字体字形，
    因此 DPI / 字体变化下不会出现粗细与基线漂移，也不需要引入图标资源。
    QSS 只负责背景、hover 与按下反馈，字形一律由 :func:`_draw_icon_glyph` 画。

    ``variant="primary"`` 时字形用强调色，用于「授权」这个唯一被强调的入口。
    """

    HELP, LICENSE, CONTACT, SETTINGS, FULLSCREEN, FULLSCREEN_EXIT = (
        "help", "license", "contact", "settings", "fullscreen",
        "fullscreenExit")

    def __init__(self, kind: str, tooltip: str = "",
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._kind = kind
        self.setObjectName("titleIconBtn")
        self.setFlat(True)
        self.setFixedSize(Metrics.ICON_BUTTON_SIZE, Metrics.ICON_BUTTON_SIZE)
        self.setFocusPolicy(Qt.NoFocus)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover, True)
        if tooltip:
            self.setToolTip(tooltip)

    def set_kind(self, kind: str) -> None:
        """切换图标（全屏 ↔ 还原），同时刷新 tooltip。"""
        self._kind = kind
        self.setToolTip("退出全屏工作台" if kind == self.FULLSCREEN_EXIT
                        else "全屏工作台")
        self.update()

    # -- 绘制 ----------------------------------------------------------
    def _glyph_color(self) -> str:
        if self.property("variant") == "primary":
            return Palette.TEXT
        if self.underMouse():
            return Palette.TEXT
        return Palette.TEXT_DIM

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)   # QSS 先画背景 / hover / pressed

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        pen = QPen(QColor(self._glyph_color()))
        pen.setWidthF(1.2)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)

        cx = self.width() / 2.0
        cy = self.height() / 2.0
        s = 6.0

        _draw_icon_glyph(painter, self._kind, cx, cy, s)
        painter.end()


# ======================================================================
# 就近弹出的小卡片（标题栏按钮下方）
# ======================================================================
_POPUP_QSS = f"""
#infoPopup {{
    background-color: {Palette.POPUP_BG};
    border: 1px solid {Palette.BORDER_STRONG};
    border-radius: 10px;
}}
#infoPopup QLabel {{ background: none; }}
#popupTitle {{ font-size: 13px; font-weight: 600; color: {Palette.TEXT}; }}
#popupSubtitle {{ font-size: 11px; color: {Palette.TEXT_MUTED}; }}
#popupLabel {{ font-size: 12px; color: {Palette.TEXT_DIM}; }}
#popupValue {{ font-size: 12px; color: {Palette.TEXT};
               font-family: {Palette.MONO_FAMILY}; }}
#popupHint {{ font-size: 11px; color: {Palette.OK_TEXT}; }}
#infoPopup QPushButton {{ font-size: 11px; }}
"""


class InfoPopup(QFrame):
    """挂在某个按钮下方的深色信息卡片：标题 + 副标题 + 若干「标签/值/复制」行。

    用 ``Qt.Popup`` 而不是普通顶层窗口：它天然满足"点外面就关掉"，
    不需要自己装应用级事件过滤器去判断点在哪儿 —— 那种过滤器在窗口销毁
    之后还活着的话很容易踩到悬空指针。

    Popup 不是模态对话框：它不阻塞事件循环，正在跑的直播解码与 OCR 照常工作，
    只是鼠标点别处会先被用来关掉本卡片（与菜单一致）。
    """

    WIDTH = 300

    def __init__(self, title: str, subtitle: str, rows, parent=None):
        """
        :param rows: [(标签, 值, 复制成功后的提示文案), ...]
        """
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint)
        self.setObjectName("infoPopup")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(_POPUP_QSS)
        self.setFixedWidth(self.WIDTH)

        col = QVBoxLayout(self)
        col.setContentsMargins(16, 14, 16, 14)
        col.setSpacing(8)

        title_label = QLabel(title)
        title_label.setObjectName("popupTitle")
        col.addWidget(title_label)

        subtitle_label = QLabel(subtitle)
        subtitle_label.setObjectName("popupSubtitle")
        col.addWidget(subtitle_label)

        for label, value, copied_text in rows:
            col.addLayout(self._row(label, value, copied_text))

    @staticmethod
    def _row(label: str, value: str, copied_text: str) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)

        name = QLabel(label)
        name.setObjectName("popupLabel")
        name.setFixedWidth(38)          # 两行标签对齐
        row.addWidget(name)

        text = QLabel(value)
        text.setObjectName("popupValue")
        # 值完整展示、可选中，不省略
        text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        row.addWidget(text)

        hint = QLabel("")
        hint.setObjectName("popupHint")
        row.addWidget(hint)
        row.addStretch(1)

        btn = QPushButton("复制")
        btn.setFixedHeight(24)
        btn.setMinimumWidth(48)
        btn.setCursor(Qt.PointingHandCursor)

        def _copy():
            clipboard = QApplication.clipboard()
            if clipboard is not None:
                clipboard.setText(value)
                hint.setText(copied_text)

        btn.clicked.connect(_copy)
        row.addWidget(btn)
        return row

    # ------------------------------------------------------------------
    def show_below(self, anchor: QWidget) -> None:
        """贴在 anchor 下方弹出，并夹回屏幕可用范围内。"""
        self.adjustSize()
        btn_rect = QRect(anchor.mapToGlobal(QPoint(0, 0)), anchor.size())

        screen = QApplication.screenAt(btn_rect.center()) or QApplication.primaryScreen()
        area = screen.availableGeometry() if screen is not None else btn_rect

        # 水平方向尽量与按钮中心对齐；越界就夹回屏内
        x = btn_rect.center().x() - self.width() // 2
        x = max(area.left() + 8, min(x, area.right() - self.width() - 8))
        y = btn_rect.bottom() + 6
        if y + self.height() > area.bottom() - 8:
            # 下面放不下就翻到按钮上方
            y = max(area.top() + 8, btn_rect.top() - self.height() - 6)

        self.move(x, y)
        self.show()


# ======================================================================
# 自定义标题栏
# ======================================================================
class TitleBar(QFrame):
    """无边框主窗口的自定义标题栏。

    可拖动、双击最大化/还原；窗口按钮由 :class:`FramelessController` 接线。
    """

    def __init__(self, title: str, subtitle: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("titleBar")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedHeight(Metrics.TITLEBAR_HEIGHT)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 0, 0)
        layout.setSpacing(8)

        logo = QLabel()
        logo.setObjectName("appLogo")
        logo.setFixedSize(14, 14)
        logo.setAttribute(Qt.WA_TransparentForMouseEvents)

        title_label = QLabel(title)
        title_label.setObjectName("appTitle")
        title_label.setAttribute(Qt.WA_TransparentForMouseEvents)

        layout.addWidget(logo)
        layout.addWidget(title_label)

        if subtitle:
            separator = QLabel("·")
            separator.setObjectName("appSubtitle")
            separator.setAttribute(Qt.WA_TransparentForMouseEvents)
            subtitle_label = QLabel(subtitle)
            subtitle_label.setObjectName("appSubtitle")
            subtitle_label.setAttribute(Qt.WA_TransparentForMouseEvents)
            layout.addWidget(separator)
            layout.addWidget(subtitle_label)

        layout.addStretch(1)

        # 自定义操作区（帮助 / 授权 / 设置 …）：位于窗口按钮**左侧**的空位。
        # 默认没有内容、不占可视宽度，既有调用方的标题栏外观与行为不变。
        # 放在这里而不是让调用方自己拼布局：窗口按钮的贴角布局是标题栏的职责。
        self._actions = QWidget()
        self._actions.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self._actions_layout = QHBoxLayout(self._actions)
        self._actions_layout.setContentsMargins(0, 0, 6, 0)
        self._actions_layout.setSpacing(2)
        layout.addWidget(self._actions)

        # 窗口按钮单独一行，按钮之间不留间距（贴住窗口右上角）
        buttons = QWidget()
        buttons_layout = QHBoxLayout(buttons)
        buttons_layout.setContentsMargins(0, 0, 0, 0)
        buttons_layout.setSpacing(0)

        self.min_button = WindowButton(WindowButton.MIN, self)
        self.max_button = WindowButton(WindowButton.MAX, self)
        self.close_button = WindowButton(WindowButton.CLOSE, self)
        for button in (self.min_button, self.max_button, self.close_button):
            buttons_layout.addWidget(button)

        layout.addWidget(buttons)

        # 拖动状态（仅在没有系统级拖动能力时使用）
        self._drag_offset: QPoint | None = None

    # -- 自定义操作区 --------------------------------------------------
    def add_action_button(self, text: str, tooltip: str = "",
                          object_name: str = "") -> QPushButton:
        """在窗口按钮左侧加一个操作按钮（帮助 / 授权 / 设置…）并返回它。

        只负责摆放；按钮的语义、样式变体与信号接线由调用方决定。
        """
        button = QPushButton(text, self)
        if object_name:
            button.setObjectName(object_name)
        if tooltip:
            button.setToolTip(tooltip)
        button.setFixedHeight(26)
        button.setCursor(Qt.PointingHandCursor)
        self._actions_layout.addWidget(button)
        return button

    def add_icon_button(self, kind: str, tooltip: str = "") -> "IconButton":
        """在操作区加一个紧凑图标按钮并返回它。

        与 :meth:`add_action_button` 同一个位置，但用自绘图标取代文字，
        避免标题栏被三个中文按钮撑满（Phase UI-3.1）。
        """
        button = IconButton(kind, tooltip, self)
        self._actions_layout.addWidget(button)
        return button

    def add_action_widget(self, widget: QWidget) -> None:
        """往操作区插入任意控件（例如一个状态标签）。"""
        self._actions_layout.addWidget(widget)

    # -- 拖动 ----------------------------------------------------------
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return

        window = self.window()
        handle = window.windowHandle()
        if handle is not None:
            try:
                # 交给系统：保留 Aero Snap、多显示器 DPI 切换等原生行为
                if handle.startSystemMove():
                    event.accept()
                    return
            except (AttributeError, RuntimeError):
                pass

        # 退回手动拖动
        self._drag_offset = (
            event.globalPosition().toPoint() - window.frameGeometry().topLeft()
        )
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_offset is not None and (event.buttons() & Qt.LeftButton):
            self.window().move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            window = self.window()
            if window.isMaximized():
                window.showNormal()
            else:
                window.showMaximized()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


# ======================================================================
# 无边框窗口：移动 / 缩放
# ======================================================================
class FramelessController(QObject):
    """给无边框窗口补上边角缩放能力。

    事件过滤器装在 QApplication 上：窗口边缘的鼠标事件往往先落在
    子控件（Dock、预览区）上，装在窗口自身会漏掉这些位置。

    Windows 上优先用 ``QWindow.startSystemResize``，失败时退回手动几何计算，
    保证任何环境下窗口都一定能缩放。
    """

    def __init__(self, window: QWidget):
        super().__init__(window)
        self._window = window
        #: 窗口是否处于全屏（由 MainWindow 通过 set_fullscreen 同步进来）
        self._fullscreen = False
        self._manual_edge = Qt.Edges()
        self._manual_start_geo: QRect | None = None
        self._manual_start_pos: QPoint | None = None
        self._last_cursor: Qt.CursorShape = Qt.ArrowCursor
        self._app: QApplication | None = QApplication.instance()
        if self._app is not None:
            self._app.installEventFilter(self)
        window.destroyed.connect(self._detach)

    def set_fullscreen(self, enabled: bool) -> None:
        """由窗口在状态变化时同步：全屏期间停用边角缩放。

        不用查询窗口的方式实现，是为了在事件的每个分支上都不去解引用
        ``self._window`` —— 见 :meth:`eventFilter` 里的说明。
        """
        self._fullscreen = bool(enabled)

    def _detach(self, *_args) -> None:
        """窗口销毁时摘掉应用级过滤器，避免留下悬空引用。"""
        if self._app is not None:
            try:
                self._app.removeEventFilter(self)
            except RuntimeError:
                pass
            self._app = None

    # -- 边角判定 ------------------------------------------------------
    def _edges_at(self, pos: QPoint) -> Qt.Edges:
        margin = Metrics.RESIZE_MARGIN
        rect = self._window.rect()
        if rect.width() <= margin * 3 or rect.height() <= margin * 3:
            return Qt.Edges()

        edges = Qt.Edges()
        if pos.x() <= margin:
            edges |= Qt.LeftEdge
        elif pos.x() >= rect.width() - margin:
            edges |= Qt.RightEdge
        if pos.y() <= margin:
            edges |= Qt.TopEdge
        elif pos.y() >= rect.height() - margin:
            edges |= Qt.BottomEdge
        return edges

    @staticmethod
    def _cursor_for(edges: Qt.Edges) -> Qt.CursorShape:
        left = bool(edges & Qt.LeftEdge)
        right = bool(edges & Qt.RightEdge)
        top = bool(edges & Qt.TopEdge)
        bottom = bool(edges & Qt.BottomEdge)
        if (left and top) or (right and bottom):
            return Qt.SizeFDiagCursor
        if (right and top) or (left and bottom):
            return Qt.SizeBDiagCursor
        if left or right:
            return Qt.SizeHorCursor
        if top or bottom:
            return Qt.SizeVerCursor
        return Qt.ArrowCursor

    # -- 手动缩放（系统调用不可用时的兜底）------------------------------
    def _start_manual_resize(self, edges: Qt.Edges, global_pos: QPoint) -> None:
        self._manual_edge = edges
        self._manual_start_geo = QRect(self._window.geometry())
        self._manual_start_pos = global_pos

    def _apply_manual_resize(self, global_pos: QPoint) -> None:
        if self._manual_start_geo is None or self._manual_start_pos is None:
            return
        delta = global_pos - self._manual_start_pos
        geo = QRect(self._manual_start_geo)
        minimum = self._window.minimumSize()

        if self._manual_edge & Qt.LeftEdge:
            geo.setLeft(min(geo.left() + delta.x(), geo.right() - minimum.width()))
        elif self._manual_edge & Qt.RightEdge:
            geo.setRight(max(geo.right() + delta.x(), geo.left() + minimum.width()))
        if self._manual_edge & Qt.TopEdge:
            geo.setTop(min(geo.top() + delta.y(), geo.bottom() - minimum.height()))
        elif self._manual_edge & Qt.BottomEdge:
            geo.setBottom(max(geo.bottom() + delta.y(), geo.top() + minimum.height()))

        self._window.setGeometry(geo)

    # -- 事件过滤 ------------------------------------------------------
    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        event_type = event.type()
        if event_type not in (
            QEvent.Type.MouseMove,
            QEvent.Type.MouseButtonPress,
            QEvent.Type.MouseButtonRelease,
        ):
            return False

        # 全屏时不参与边角缩放。全屏窗口本来就不可缩放，而顶部 5px 的缩放
        # 热区（RESIZE_MARGIN）会整个盖住 4px 高的"鼠标到顶滑出标题栏"热区：
        # 鼠标移到屏幕顶端想唤出标题栏，却变成按住窗口上边缘拖拽。
        #
        # 刻意读自己的标志位而不是 self._window.isFullScreen()：窗口销毁后
        # 访问它会**直接段错误**（不是 RuntimeError，try/except 兜不住）。
        # 原实现对 MouseButtonRelease 完全不碰 _window，多这一次解引用就足以
        # 让「反复创建/销毁主窗口」的用例崩溃。
        if self._fullscreen:
            return False

        try:
            if not isinstance(obj, QWidget) or obj.window() is not self._window:
                return False
        except RuntimeError:
            return False

        if event_type == QEvent.Type.MouseButtonPress:
            if event.button() != Qt.LeftButton:
                return False
            global_pos = event.globalPosition().toPoint()
            edges = self._edges_at(event.position().toPoint())
            if not edges:
                return False
            handle = self._window.windowHandle()
            if handle is not None:
                try:
                    if handle.startSystemResize(edges):
                        return True
                except (AttributeError, RuntimeError):
                    pass
            self._start_manual_resize(edges, global_pos)
            return True

        if event_type == QEvent.Type.MouseButtonRelease:
            self._manual_start_geo = None
            self._manual_start_pos = None
            self._manual_edge = Qt.Edges()
            return False

        # MouseMove
        if self._manual_start_pos is not None and (event.buttons() & Qt.LeftButton):
            self._apply_manual_resize(event.globalPosition().toPoint())
            return True

        edges = self._edges_at(event.position().toPoint())
        cursor = self._cursor_for(edges)
        if cursor != self._last_cursor:
            self._last_cursor = cursor
            if cursor == Qt.ArrowCursor:
                self._window.unsetCursor()
            else:
                self._window.setCursor(cursor)
        return False
