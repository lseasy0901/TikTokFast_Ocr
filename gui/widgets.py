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

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
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
    "FramelessController",
    "OverlayHost",
    "set_property",
    "set_tone",
    "enable_mouse_tracking",
    "h_line",
]


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

    def add_overlay(self, widget: QWidget) -> None:
        """登记一个「铺满容器」的浮层。"""
        widget.setParent(self)
        widget.setAttribute(Qt.WA_TransparentForMouseEvents, True)
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
        self._manual_edge = Qt.Edges()
        self._manual_start_geo: QRect | None = None
        self._manual_start_pos: QPoint | None = None
        self._last_cursor: Qt.CursorShape = Qt.ArrowCursor
        self._app: QApplication | None = QApplication.instance()
        if self._app is not None:
            self._app.installEventFilter(self)
        window.destroyed.connect(self._detach)

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
