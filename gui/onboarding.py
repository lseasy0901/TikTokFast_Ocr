# -*- coding: utf-8 -*-
"""
首次启动引导 / ROI 框选帮助

本模块提供两个纯 GUI 的说明组件，不含任何业务逻辑：

    OnboardingDialog   5 步使用引导。首次启动由 main.py 触发，
                       之后可从状态栏「使用引导」再次打开。
    RoiHelpDialog      识别区域（ROI）框选说明，含代码绘制的正误对比示意图。

示意图**全部用 QPainter 绘制，不引入任何图片文件**：
    新增打包资源需要改 douyin_viewer.spec 的 datas（打包架构，本阶段禁止改动）。
    仓库现有的画法都是自绘（gui/widgets.py 的 WindowButton、gui/video_widget.py
    的 ROI 框、ocr/roi_selector.py 的选择框），这里沿用同一做法。

配色全部取自 gui/theme.Palette，与 UpdateDialog / ExpiredDialog / ActivationDialog
同一套做法；QDialog 的应用级样式表由 theme.apply_theme() 统一安装，这里不重复设背景。

对话框只负责「报告用户的意图」，自己不碰磁盘：
    写盘由 gui/main_window.py 的 _run_onboarding() 统一完成 —— 见
    OnboardingDialog.should_mark_seen()。
"""

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from gui.theme import Palette

# ======================================================================
# 样式小工具
# ======================================================================
#: 所有内联 QLabel 规则都必须带 background: none，否则 Fusion 会给标签
#: 刷上一层底色。这是三个既有对话框共同遵循的约定。
_LABEL_QSS = "QLabel {{ {rules} background: none; }}"


def _style_label(label: QLabel, size: int, color: str, bold: bool = False) -> QLabel:
    """给标签套上对话框内部使用的字号 / 颜色（不动全局 QSS）。"""
    weight = "font-weight: 600;" if bold else ""
    label.setStyleSheet(_LABEL_QSS.format(rules=f"font-size: {size}px; {weight} color: {color};"))
    return label


def _body_label(text: str, size: int = 13, color: str = Palette.TEXT_DIM) -> QLabel:
    """一段自动换行的正文标签。"""
    label = _style_label(QLabel(text), size, color)
    label.setWordWrap(True)
    label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
    return label


# ======================================================================
# 进度圆点
# ======================================================================
class StepDots(QWidget):
    """向导进度圆点：当前步用强调色，其余用弱化色。

    自绘而不是用「● ○」字符：圆点字符在不同字体下的基线与宽度不一致，
    自绘能保证对齐，且不依赖字体是否包含该字形。
    """

    _DIAMETER = 8
    _SPACING = 16

    def __init__(self, count: int = 5, parent: QWidget | None = None):
        super().__init__(parent)
        self._count = count
        self._current = 0
        self.setFixedSize(count * self._SPACING, self._DIAMETER + 4)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    # -- 对外接口 ------------------------------------------------------
    def set_current(self, index: int) -> None:
        index = max(0, min(index, self._count - 1))
        if index == self._current:
            return
        self._current = index
        self.update()

    def current(self) -> int:
        return self._current

    def count(self) -> int:
        return self._count

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self._count * self._SPACING, self._DIAMETER + 4)

    # -- 绘制 ----------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)

        y = (self.height() - self._DIAMETER) // 2
        for i in range(self._count):
            # 已走过的步骤与当前步骤都算「进度」，统一用强调色
            color = Palette.ACCENT if i <= self._current else Palette.TEXT_MUTED
            painter.setBrush(QColor(color))
            painter.drawEllipse(i * self._SPACING + 2, y, self._DIAMETER, self._DIAMETER)
        painter.end()


# ======================================================================
# ROI 示意图（纯绘制）
# ======================================================================
class RoiSchematic(QWidget):
    """三格示意图：正确 / 框得太大 / 框得太小。

    三格的模拟画面内容完全一致，唯一的差别是那个框 —— 这样才构成对照。
    """

    _KINDS = ("ok", "too_big", "too_small")
    _CAPTIONS = {
        "ok": "✓ 正确：正好框住房间号",
        "too_big": "✗ 框得太大：把界面也框进去了",
        "too_small": "✗ 框得太小：切掉了数字",
    }

    #: 与 ocr/roi_selector.py 的选择框同色（绿），让示意图和实际框选对得上。
    _SELECT_COLOR = QColor(0, 255, 128, 220)

    _PANEL_GAP = 12
    _CAPTION_H = 22

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedHeight(172)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    # -- 绘制 ----------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        pad = 2
        panel_w = (self.width() - pad * 2 - self._PANEL_GAP * 2) // 3
        panel_h = self.height() - self._CAPTION_H
        if panel_w <= 20 or panel_h <= 20:
            painter.end()
            return

        for i, kind in enumerate(self._KINDS):
            x = pad + i * (panel_w + self._PANEL_GAP)
            frame = QRect(x, 0, panel_w, panel_h)
            code = self._code_rect(frame)

            self._draw_frame(painter, frame)
            self._draw_mock_content(painter, frame)
            self._draw_code(painter, code)
            self._draw_box(painter, *self._box_for(kind, frame, code))
            self._draw_caption(painter, QRect(x, panel_h, panel_w, self._CAPTION_H), kind)
        painter.end()

    # -- 几何 ----------------------------------------------------------
    @staticmethod
    def _code_rect(frame: QRect) -> QRect:
        """房间号在模拟画面里占据的矩形。所有尺寸都按 frame 的比例算，
        因此在高 DPI / 字体缩放下整体等比变化，不会错位。"""
        w = int(frame.width() * 0.54)
        h = int(frame.height() * 0.20)
        x = frame.x() + (frame.width() - w) // 2
        y = frame.y() + int(frame.height() * 0.52)
        return QRect(x, y, w, h)

    def _box_for(self, kind: str, frame: QRect, code: QRect):
        """返回 (框矩形, 颜色, 是否虚线)。「错」由颜色 + 线型双重编码，不只靠颜色。"""
        if kind == "ok":
            return code.adjusted(-6, -4, 6, 4), self._SELECT_COLOR, False
        if kind == "too_big":
            return frame.adjusted(4, 4, -4, -4), QColor(Palette.DANGER), True
        # too_small：只盖住中间一小段，两侧的数字被切在框外
        w = max(8, code.width() * 2 // 5)
        return (
            QRect(code.center().x() - w // 2, code.top() + 2, w, code.height() - 4),
            QColor(Palette.DANGER),
            True,
        )

    # -- 绘制细节 ------------------------------------------------------
    @staticmethod
    def _draw_frame(painter: QPainter, frame: QRect) -> None:
        """模拟画面底：与预览区同色，让它看起来就是「画面」。"""
        painter.setBrush(QColor(Palette.PANEL_BG))
        painter.setPen(QPen(QColor(Palette.BORDER), 1))
        painter.drawRect(frame)

    @staticmethod
    def _draw_mock_content(painter: QPainter, frame: QRect) -> None:
        """画面里的界面元素（昵称条 / 血条一类）。刻意用低对比色，
        表示「这是背景内容，不是识别目标」。"""
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(Palette.BORDER_STRONG))

        inset = max(4, frame.width() // 14)
        bar_h = max(4, frame.height() // 9)
        # 顶部信息条
        painter.drawRect(
            QRect(frame.x() + inset, frame.y() + inset, frame.width() - inset * 2, bar_h)
        )
        # 底部信息条
        painter.drawRect(
            QRect(
                frame.x() + inset,
                frame.bottom() - inset - bar_h,
                frame.width() - inset * 2,
                bar_h,
            )
        )

    @staticmethod
    def _draw_code(painter: QPainter, code: QRect) -> None:
        font = QFont()
        font.setFamilies(["Consolas", "Cascadia Mono", "monospace"])
        font.setPixelSize(max(9, int(code.height() * 0.7)))
        painter.setFont(font)
        painter.setPen(QColor(Palette.TEXT))
        painter.drawText(code, Qt.AlignmentFlag.AlignCenter, "1234567")

    @staticmethod
    def _draw_box(painter: QPainter, rect: QRect, color: QColor, dashed: bool) -> None:
        pen = QPen(color)
        pen.setWidth(2)
        if dashed:
            pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(rect)

    def _draw_caption(self, painter: QPainter, rect: QRect, kind: str) -> None:
        font = QFont()
        font.setPixelSize(12)
        painter.setFont(font)
        painter.setPen(QColor(Palette.OK_TEXT if kind == "ok" else Palette.DANGER_TEXT))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._CAPTIONS[kind])


# ======================================================================
# 首次启动引导
# ======================================================================
#: 5 步引导文案。全部基于程序的真实行为，不描述任何不存在的功能。
STEPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "连接直播间",
        (
            "从浏览器复制直播间地址（形如 https://live.douyin.com/123456），"
            "粘贴到左下「直播源」的输入框，点「连接」。画面会显示在中间的预览区，"
            "状态栏同时给出 FPS / 延迟 / 分辨率。",
            "注意：首次使用需要先点右下角「激活许可证」完成激活。"
            "未激活时「连接」会被拒绝，状态栏会说明原因。",
        ),
    ),
    (
        "选择游戏类型",
        (
            "在右侧「OCR 控制」的「游戏」下拉框里明确选择当前直播的游戏"
            "（三角洲行动 / 无畏契约）。",
            "必须选择具体游戏：房间号的位数和格式由游戏决定，不选就无法开始识别。"
            "左侧「配置 / 场景」列表与这里是同一个选择项，改哪一个都一样。",
        ),
    ),
    (
        "框选识别区域",
        (
            "连接成功后「选择识别区域」按钮才会变为可用。点击后画面会定格："
            "按住鼠标左键拖动，框住画面里的房间号，按 Enter 确认，按 Esc 取消"
            "（取消会保留上一次的区域）。",
            "只框住房间号本身，不要带上昵称、血条等界面元素。"
            "点「OCR 区域」下方的「说明」可以查看正确与错误选区的对比示例。",
        ),
    ),
    (
        "开始识别",
        (
            "点「开始OCR」，程序约每 300 毫秒读取一次框选的区域。",
            "识别到的房间号会自动复制到剪贴板，直接粘贴即可使用；"
            "识别不出内容时不会覆盖剪贴板里已有的内容。",
        ),
    ),
    (
        "几点说明",
        (
            "框选之后画面上会显示一个红色矩形，它只是显示提示 —— "
            "识别读取的是原始画面像素，不会把红框算进去。",
            "如果游戏选错了，程序会提示「检测到选择错误，请重新选择游戏类型后再进行识别。」，"
            "重新选好游戏再识别即可。程序只输出校验通过的房间号："
            "宁可没有结果，也不会给出错误的号码。",
            "之后可以从状态栏右侧「使用引导」重新打开本引导。",
        ),
    ),
)


class OnboardingDialog(QDialog):
    """5 步使用引导向导。

    :param first_run: True 表示首次启动自动弹出。此时跳过 / 关闭会被视为
        「以后不再自动显示」，最后一页的复选框可以改回显示。
        False 表示用户从「使用引导」主动打开的回顾模式：不显示复选框，
        并且不表态（:meth:`should_mark_seen` 恒为 False），
        因此重新打开永远不会覆盖用户之前的选择。
    """

    def __init__(self, parent: QWidget | None = None, first_run: bool = True):
        super().__init__(parent)
        self._first_run = first_run
        self._reached_last_step = False

        self.setWindowTitle("使用引导")
        self.setModal(True)
        self.setFixedSize(560, 420)
        self._setup_ui()
        self._sync_controls()

    # ------------------------------------------------------------------
    # 对外接口（同时是测试的观察面）
    # ------------------------------------------------------------------
    def step_count(self) -> int:
        return self._stack.count()

    def current_step(self) -> int:
        return self._stack.currentIndex()

    def should_mark_seen(self) -> bool:
        """关闭后是否应记录「以后不再自动显示」。

        回顾模式（用户主动打开）恒为 False —— 看一眼帮助不该改掉之前的设置。
        首次启动时：没走到最后一页就关了（点「跳过」、按 Esc、点右上角 X）
        视为「别再打扰我」，走到最后一页则以复选框为准。
        """
        if not self._first_run:
            return False
        if not self._reached_last_step:
            return True
        return self._dont_show.isChecked()

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------
    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(14)

        self._stack = QStackedWidget()
        last = len(STEPS) - 1
        for index, (title, paragraphs) in enumerate(STEPS):
            self._stack.addWidget(self._build_page(title, paragraphs, is_last=index == last))
        layout.addWidget(self._stack, 1)

        dots_row = QHBoxLayout()
        dots_row.addStretch(1)
        self._dots = StepDots(self._stack.count())
        dots_row.addWidget(self._dots)
        dots_row.addStretch(1)
        layout.addLayout(dots_row)

        layout.addLayout(self._build_buttons())
        self._stack.currentChanged.connect(self._on_step_changed)

    def _build_page(self, title: str, paragraphs: tuple[str, ...], is_last: bool) -> QWidget:
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(10)

        page_layout.addWidget(_style_label(QLabel(title), 15, Palette.ACCENT_TEXT, bold=True))
        for text in paragraphs:
            page_layout.addWidget(_body_label(text))
        page_layout.addStretch(1)

        # 复选框只出现在最后一页：它属于「本次引导结束后怎么办」这个问题。
        # 回顾模式下不显示 —— 用户主动打开看一遍，不该顺手改掉之前的设置。
        if is_last and self._first_run:
            self._dont_show = QCheckBox("不再自动显示")
            self._dont_show.setChecked(True)
            self._dont_show.setStyleSheet(
                f"QCheckBox {{ color: {Palette.TEXT_DIM}; font-size: 12px;"
                f" background: none; spacing: 8px; }}"
                f"QCheckBox::indicator {{ width: 14px; height: 14px;"
                f" border: 1px solid {Palette.BORDER}; border-radius: 3px;"
                f" background: {Palette.INPUT_BG}; }}"
                f"QCheckBox::indicator:checked {{ background: {Palette.ACCENT};"
                f" border-color: {Palette.ACCENT}; }}"
            )
            page_layout.addWidget(self._dont_show)

        return page

    def _build_buttons(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)

        # 每个按钮都显式以 self 为父：不加入布局的按钮若没有父，就是顶层窗口，
        # setVisible(False) 之后仍可能以独立窗口的身份出现。
        self._prev_btn = QPushButton("上一步", self)
        self._prev_btn.setFixedHeight(34)
        self._prev_btn.setMinimumWidth(88)
        self._prev_btn.clicked.connect(lambda: self._go(self.current_step() - 1))

        # 「跳过」始终创建并加入布局，回顾模式下由 _sync_controls 隐藏
        self._skip_btn = QPushButton("跳过", self)
        self._skip_btn.setFixedHeight(34)
        self._skip_btn.setMinimumWidth(72)
        self._skip_btn.clicked.connect(self.reject)

        self._next_btn = QPushButton("下一步", self)
        self._next_btn.setFixedHeight(34)
        self._next_btn.setMinimumWidth(96)
        self._next_btn.setProperty("variant", "primary")
        self._next_btn.clicked.connect(lambda: self._go(self.current_step() + 1))

        self._finish_btn = QPushButton("完成", self)
        self._finish_btn.setFixedHeight(34)
        self._finish_btn.setMinimumWidth(96)
        self._finish_btn.setProperty("variant", "primary")
        self._finish_btn.clicked.connect(self.accept)

        row.addWidget(self._prev_btn)
        row.addWidget(self._skip_btn)
        row.addStretch(1)
        row.addWidget(self._next_btn)
        row.addWidget(self._finish_btn)
        return row

    # ------------------------------------------------------------------
    # 导航
    # ------------------------------------------------------------------
    def _go(self, index: int) -> None:
        self._stack.setCurrentIndex(max(0, min(index, self._stack.count() - 1)))

    def _on_step_changed(self, index: int) -> None:
        if index == self._stack.count() - 1:
            self._reached_last_step = True
        self._dots.set_current(index)
        self._sync_controls()

    def _sync_controls(self) -> None:
        index = self.current_step()
        last = index == self._stack.count() - 1

        self._prev_btn.setEnabled(index > 0)
        # 用 setVisible 而不是 setHidden：两个按钮互斥地占同一个位置
        self._next_btn.setVisible(not last)
        self._finish_btn.setVisible(last)
        # 回顾模式下没有「跳过」的概念：用户是主动打开来看的，给他这个按钮
        # 反而像是在催他关掉
        self._skip_btn.setVisible(self._first_run and not last)
        if last:
            self._finish_btn.setText("完成" if self._first_run else "关闭")


# ======================================================================
# ROI 框选帮助
# ======================================================================
#: 要点列表。全部是可操作的具体建议，不重复示意图已经表达的内容。
_ROI_TIPS: tuple[str, ...] = (
    "只框住房间号本身，不要带上玩家昵称、血条、弹幕等界面元素。",
    "四周留一点空隙，让数字完整落在框内 —— 略微大一点没关系，切掉数字才会识别失败。",
    "框选时：拖动鼠标框选 · Enter 确认 · Esc 取消（取消会保留原来的区域）。",
    "识别区域只有一个，重新框选会替换掉原来的区域。",
    "区域是画面上的固定位置：房间号在画面里的位置变了，就需要重新框选。",
    "画面上的红色矩形只是显示提示，识别读取的是原始画面像素。",
)


class RoiHelpDialog(QDialog):
    """「选择识别区域」的说明与示例。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("识别区域选择说明")
        self.setModal(True)
        self.setFixedSize(600, 520)
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(14)

        layout.addWidget(
            _style_label(QLabel("怎样框选识别区域"), 15, Palette.ACCENT_TEXT, bold=True)
        )
        layout.addWidget(
            _body_label(
                "识别区域要正好框住画面里的房间号。下面是三种典型情况的对比。",
                size=12,
                color=Palette.TEXT,
            )
        )

        # 内容整体放进滚动区：固定尺寸的对话框不会因为字体缩放而截断文字
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(10)

        self._schematic = RoiSchematic()
        container_layout.addWidget(self._schematic)

        # 必须点明「示意图的绿框」和「实际画面上的红框」不是一回事，
        # 否则用户会去画面里找绿框。
        caption = _body_label(
            "（示意图里的绿框表示框选动作；确认后画面上显示的是红色矩形。）",
            size=12,
            color=Palette.TEXT_MUTED,
        )
        caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        container_layout.addWidget(caption)

        for tip in _ROI_TIPS:
            container_layout.addWidget(_body_label(f"• {tip}", size=12))
        container_layout.addStretch(1)

        scroll.setWidget(container)
        layout.addWidget(scroll, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        close_btn = QPushButton("我知道了")
        close_btn.setFixedHeight(36)
        close_btn.setMinimumWidth(110)
        close_btn.setProperty("variant", "primary")
        close_btn.clicked.connect(self.accept)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)
