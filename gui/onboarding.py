# -*- coding: utf-8 -*-
"""
首次启动引导 / ROI 框选帮助

本模块提供两个纯 GUI 的说明组件，不含任何业务逻辑：

    OnboardingDialog   4 步使用引导的第 4 步。首次启动由 main.py 触发，
                       之后可从标题栏的帮助入口再次打开（回顾模式）。
                       前 3 步由 GuideSpotlight 在主界面上以聚光灯呈现。
    RoiHelpDialog      识别区域（ROI）框选说明，含代码绘制的正误对比示意图。

示意图**全部用 QPainter 绘制，不引入任何图片文件**：
    新增打包资源需要改 douyin_viewer.spec 的 datas（打包架构，本阶段禁止改动）。
    仓库现有的画法都是自绘（gui/widgets.py 的 WindowButton、gui/video_widget.py
    的 ROI 框、ocr/roi_selector.py 的选择框），这里沿用同一做法。

配色全部取自 gui/theme.Palette，与 UpdateDialog / ExpiredDialog / ActivationDialog
同一套做法；QDialog 的应用级样式表由 theme.apply_theme() 统一安装，这里不重复设背景。

对话框只负责「报告用户的意图」，自己不碰磁盘：
    写盘由 gui/main_window.py 统一完成 —— 见 OnboardingDialog 的
    completed() 与 exit_choice()。
"""

from PySide6.QtCore import (
    QEvent,
    QObject,
    QPoint,
    QRect,
    QRectF,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
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
#: 4 步引导文案。全部基于程序的真实行为，不描述任何不存在的功能。
#: 前 3 步（索引 0-2）由聚光灯承担，见 DIALOG_FIRST_STEP。
STEPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "连接直播间",
        ("粘贴直播间地址，点击「连接」，即可开始预览。",),
    ),
    (
        "选择游戏",
        ("选择直播间正在玩的游戏，确保房间码格式匹配。",),
    ),
    (
        "框选房间码",
        (
            "点击「选择识别区域」，拖动框住房间码，松开后按 Enter 确认。",
            "按 Esc 可取消当前框选。",
        ),
    ),
    (
        "开始自动识别",
        ("点击「开始 OCR」。识别到新的房间码后，软件会自动复制到剪贴板。",),
    ),
)

#: 本对话框负责的第一步（STEPS 索引）。
#: 首次引导已全部改由主界面聚光灯承担（GuideSpotlight），对话框现在只服务
#: 「标题栏 ? → 回顾模式」：用户主动打开时应当能完整回看四步，所以从第 1 步
#: 开始渲染。
DIALOG_FIRST_STEP = 0


class OnboardingDialog(QDialog):
    """4 步使用引导向导（只渲染第 4 步）。

    :param first_run: True 表示首次启动自动弹出。此时中途关闭会先弹退出确认
        （见 :func:`ask_guide_exit`），走完则由调用方把「不再自动显示」落盘。
        False 表示用户从「使用引导」主动打开的回顾模式：关闭时不打扰、
        也不表态（:meth:`exit_choice` 恒为 None），因此重新打开永远不会
        覆盖用户之前的选择。
    """

    def __init__(self, parent: QWidget | None = None, first_run: bool = True,
                 start_step: int = 0):
        """
        :param start_step: 起始步（STEPS 索引）。第一步「连接直播间」由主界面上的
            聚光灯承担，本对话框只渲染第 2 步及之后，因此首轮引导会带着
            ``start_step=DIALOG_FIRST_STEP`` 被打开（见 gui/main_window.py 的
            衔接逻辑）；传更小的值会被夹到对话框的第一页。
        """
        super().__init__(parent)
        self._first_run = first_run
        self._reached_last_step = False
        #: 只有点「完成」才算走完；「下一步」永远只是翻页
        self._completed = False
        #: 提前关闭时用户在确认框里的选择（None = 没关过 / 选了「返回引导」）
        self._exit_choice: str | None = None
        #: 确认过一次就不再问第二遍（closeEvent 会转调 reject）
        self._exit_confirmed = False

        self.setWindowTitle("使用引导")
        self.setModal(True)
        self.setFixedSize(560, 420)
        self._setup_ui()
        if start_step:
            self._go(start_step)
        # 显式同步一次初始进度。不能指望 setCurrentIndex 触发 currentChanged：
        # 对话框的第一页本来就是下标 0，而 start_step 落到第一页时下标没有变化，
        # 信号不会发出来，圆点就会停在"第 0 步"的位置。
        self._on_step_changed(self._stack.currentIndex())

    # ------------------------------------------------------------------
    # 对外接口（同时是测试的观察面）
    # ------------------------------------------------------------------
    def step_count(self) -> int:
        """整份引导的步数（含第一步聚光灯）。"""
        return len(STEPS)

    def current_step(self) -> int:
        """当前是整份引导里的第几步（STEPS 索引，含第一步聚光灯）。"""
        return DIALOG_FIRST_STEP + self._stack.currentIndex()

    def completed(self) -> bool:
        """是否正常走完（到最后一页并点了「完成」）。"""
        return self._completed

    def exit_choice(self) -> str | None:
        """提前关闭时用户的选择；没提前关过则为 None。"""
        return self._exit_choice

    # ------------------------------------------------------------------
    # 退出确认（✕ / Esc / 「跳过」共用同一套）
    # ------------------------------------------------------------------
    def _confirm_exit(self) -> bool:
        """返回 True 表示可以关闭；False 表示用户选择「返回引导」。"""
        if self._exit_confirmed:
            return True
        # 回顾模式（用户主动点帮助打开）：看一眼而已，直接关，
        # 既不打扰也不碰他的自动提醒设置。
        if not self._first_run:
            return True
        choice = ask_guide_exit(self)
        if choice is None:
            return False
        self._exit_choice = choice
        self._exit_confirmed = True
        return True

    def reject(self) -> None:  # noqa: D102 - 覆盖 QDialog
        if not self._confirm_exit():
            return
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802
        if not self._confirm_exit():
            event.ignore()
            return
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------
    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(14)

        self._stack = QStackedWidget()
        # 只渲染第 2 步及之后：第 1 步由聚光灯在主界面上呈现
        pages = STEPS[DIALOG_FIRST_STEP:]
        last = len(pages) - 1
        for index, (title, paragraphs) in enumerate(pages):
            self._stack.addWidget(
                self._build_page(title, paragraphs, is_last=index == last))
        layout.addWidget(self._stack, 1)

        dots_row = QHBoxLayout()
        dots_row.addStretch(1)
        # 圆点按**整份引导**的步数来画：对话框只渲染第 2 步起，
        # 但用户看到的进度应当是"共 4 步"里的一格，与第一步聚光灯一致。
        self._dots = StepDots(len(STEPS))
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
        self._finish_btn.clicked.connect(self._on_finish_clicked)

        row.addWidget(self._prev_btn)
        row.addWidget(self._skip_btn)
        row.addStretch(1)
        row.addWidget(self._next_btn)
        row.addWidget(self._finish_btn)
        return row

    # ------------------------------------------------------------------
    # 导航
    # ------------------------------------------------------------------
    def _on_finish_clicked(self) -> None:
        """点「完成」才算走完整份引导（需求 4：翻页不算）。"""
        self._completed = True
        self.accept()

    def _go(self, step_index: int) -> None:
        """跳到某一步。参数是 **STEPS 索引**，不是栈下标。"""
        last_page = self._stack.count() - 1
        page = max(0, min(step_index - DIALOG_FIRST_STEP, last_page))
        self._stack.setCurrentIndex(page)

    def _on_step_changed(self, page: int) -> None:
        """栈下标 → STEPS 索引后再刷新进度，避免两套编号错位。"""
        step_index = DIALOG_FIRST_STEP + page
        if step_index == len(STEPS) - 1:
            self._reached_last_step = True
        self._dots.set_current(step_index)
        self._sync_controls()

    def _sync_controls(self) -> None:
        last = self.current_step() == len(STEPS) - 1

        # 「上一步」看的是**对话框自己的第一页**，不是整份引导的第一步：
        # 第 1 步在聚光灯里，对话框没有它的上一页可回。
        self._prev_btn.setEnabled(self._stack.currentIndex() > 0)
        # 用 setVisible 而不是 setHidden：两个按钮互斥地占同一个位置
        self._next_btn.setVisible(not last)
        self._finish_btn.setVisible(last)
        # 回顾模式下没有「跳过」的概念：用户是主动打开来看的，给他这个按钮
        # 反而像是在催他关掉
        self._skip_btn.setVisible(self._first_run and not last)
        # 文案无条件设置：只在最后一页设的话，回顾模式在别的页上会写着「完成」，
        # 直到翻到最后一页才变「关闭」。
        self._finish_btn.setText("完成" if self._first_run else "关闭")


# ======================================================================
# UI-4.1：第一步「连接直播间」的主界面聚光灯
# ======================================================================
#: 聚光灯覆盖的步骤（STEPS 索引）：全部四步。
#: 首次引导至此完全由主界面聚光灯承担，旧版对话框只剩"标题栏 ? 回顾模式"在用。
SPOTLIGHT_STEPS: tuple[int, ...] = (0, 1, 2, 3)

#: 需要交给旧版对话框的步骤：没有。保留这个映射是为了让"聚光灯结束后往哪走"
#: 仍然是一处声明式配置，将来若要再加对话框步骤，改这里即可。
DIALOG_START_FOR: dict[int, int] = {}

#: 高亮框相对目标控件向外扩出的像素。太小会让描边压住控件本身的边框。
_HOLE_PADDING = 4

#: 卡片与高亮区之间的间距（上下方向）。
_GAP_BELOW = 14
#: 卡片与高亮区之间的间距（左右方向）。比上下略大：横向留白更显眼，
#: 且要求卡片右边缘与目标控件保持 16–24px。
_GAP_SIDE = 20
#: 卡片距窗口边缘的最小留白；所有位置候选都必须满足它。
_MARGIN = 12

#: 卡片相对高亮区的候选方位。按顺序试，第一个完整放得下的就用。
#: 第一步的目标在顶部地址栏，往下放最自然且不会盖住任何东西（保持原观感）；
#: 第二步的目标在右侧面板底部，往左放到视频区才不会盖住面板。
_CARD_SIDES_BELOW = ("below", "above", "left", "right")
_CARD_SIDES_LEFT = ("left", "below", "above", "right")


# ======================================================================
# 引导退出：三种结果 + 统一的确认弹窗
# ======================================================================
#: 走完了这一步，继续后面的引导（连接成功、或点「下一步」）。
GUIDE_ADVANCE = "advance"
#: 用户提前关闭，选择「下次继续提醒」。
GUIDE_ALWAYS = "always"
#: 用户提前关闭，选择「不再自动显示」。
GUIDE_NEVER = "never"
#: 回顾模式（标题栏 ?）被关闭。它既不代表"完成"，也不代表"别再提醒我"——
#: 调用方据此只做清理，绝不写任何首次引导状态。
GUIDE_REVIEW_CLOSED = "reviewClosed"


class _GuideExitDialog(QDialog):
    """提前结束引导时的确认：下次启动还要不要继续显示。

    刻意提供「返回引导」这个第三选项 —— 提前关闭多半是误触或想先看一眼，
    只给"是/否"会把"其实不想关"的用户逼成一个会改配置的选择。
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.choice: str | None = None

        self.setWindowTitle("结束引导")
        self.setModal(True)
        self.setFixedWidth(430)

        col = QVBoxLayout(self)
        col.setContentsMargins(22, 20, 22, 18)
        col.setSpacing(10)

        col.addWidget(_style_label(QLabel("要结束使用引导吗？"), 15,
                                   Palette.TEXT, bold=True))
        col.addWidget(_body_label(
            "下次启动软件时，是否继续显示本引导？", 12, Palette.TEXT_DIM))

        row = QHBoxLayout()
        row.setSpacing(10)
        row.addStretch(1)

        never_btn = QPushButton("不再自动显示")
        never_btn.setFixedHeight(34)
        never_btn.setMinimumWidth(112)
        never_btn.clicked.connect(lambda: self._choose(GUIDE_NEVER))
        row.addWidget(never_btn)

        always_btn = QPushButton("下次继续提醒")
        always_btn.setFixedHeight(34)
        always_btn.setMinimumWidth(112)
        always_btn.setProperty("variant", "primary")
        always_btn.clicked.connect(lambda: self._choose(GUIDE_ALWAYS))
        row.addWidget(always_btn)
        col.addLayout(row)

        back_row = QHBoxLayout()
        back_row.addStretch(1)
        back_btn = QPushButton("返回引导")
        back_btn.setFixedHeight(32)
        back_btn.setMinimumWidth(96)
        back_btn.clicked.connect(self.reject)
        back_row.addWidget(back_btn)
        col.addLayout(back_row)

    def _choose(self, choice: str) -> None:
        self.choice = choice
        self.accept()


def ask_guide_exit(parent: QWidget | None = None) -> str | None:
    """弹出退出确认。

    :return: :data:`GUIDE_ALWAYS` / :data:`GUIDE_NEVER`；
             用户选「返回引导」或直接关掉确认框时返回 None（不改变配置）。
    """
    dialog = _GuideExitDialog(parent)
    dialog.exec()
    return dialog.choice


class _SpotlightMask(QWidget):
    """半透明暗色遮罩，在高亮矩形处挖空；鼠标事件完全穿透。

    关键约束：本控件设了 ``WA_TransparentForMouseEvents``，而 Qt 的鼠标穿透
    是**连同子控件**一起生效的（见 gui/widgets.py OverlayHost 的同类说明）。
    所以提示卡片绝对不能挂在本控件下面 —— 它是兄弟控件，见 GuideSpotlight。
    """

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("spotlightMask")
        # 局部清掉背景：遮罩只画"挖空后的那一块"，其余区域必须透出真实界面
        self.setStyleSheet("background: transparent;")
        self.setAttribute(Qt.WA_StyledBackground, False)
        self.setAutoFillBackground(False)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.NoFocus)
        self._holes: list[QRect] = []

    def set_holes(self, holes) -> None:
        self._holes = list(holes)
        self.update()

    def hole_at(self, pos: QPoint) -> bool:
        """坐标是否落在某个高亮孔里（供测试与点击穿透核对使用）。"""
        return any(hole.contains(pos) for hole in self._holes)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        path = QPainterPath()
        path.addRect(QRectF(self.rect()))
        for hole in self._holes:
            cut = QPainterPath()
            cut.addRoundedRect(QRectF(hole), 8.0, 8.0)
            path = path.subtracted(cut)

        scrim = QColor(Palette.WELL)
        scrim.setAlpha(196)
        painter.fillPath(path, scrim)

        pen = QPen(QColor(Palette.ACCENT_EDGE))
        pen.setWidthF(1.2)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        for hole in self._holes:
            painter.drawRoundedRect(QRectF(hole), 8.0, 8.0)
        painter.end()


_CARD_QSS = f"""
#spotlightCard {{
    background-color: {Palette.POPUP_BG};
    border: 1px solid {Palette.BORDER_STRONG};
    border-radius: 10px;
}}
#spotlightCard QLabel {{ background: none; }}
#spotlightTitle {{ font-size: 14px; font-weight: 600; color: {Palette.TEXT}; }}
#spotlightBody  {{ font-size: 12px; color: {Palette.TEXT_DIM}; }}
#spotlightStep  {{ font-size: 11px; color: {Palette.TEXT_MUTED}; }}
#spotlightClose {{
    background: transparent; border: none; color: {Palette.TEXT_MUTED};
    font-size: 13px; padding: 0px; min-height: 0px; min-width: 0px;
}}
#spotlightClose:hover {{ color: {Palette.TEXT}; }}
#spotlightToast {{
    background-color: {Palette.POPUP_BG};
    border: 1px solid {Palette.OK_BORDER};
    border-radius: 10px;
    color: {Palette.OK_TEXT};
    font-size: 13px;
    font-weight: 600;
}}
#spotlightHint {{
    background-color: {Palette.POPUP_BG};
    border: 1px solid {Palette.BORDER_STRONG};
    border-radius: 8px;
    color: {Palette.TEXT_DIM};
    font-size: 12px;
}}
"""


def _card_origin(side: str, anchor: QRect, cw: int, ch: int):
    """某个方位下卡片的左上角坐标（尚未做窗口边界夹取）。"""
    if side == "left":
        return anchor.left() - _GAP_SIDE - cw, anchor.top()
    if side == "right":
        return anchor.right() + _GAP_SIDE, anchor.top()
    if side == "above":
        return anchor.left(), anchor.top() - _GAP_BELOW - ch
    return anchor.left(), anchor.bottom() + _GAP_BELOW      # below


class _RoiHint(QLabel):
    """框选过程中浮在画面上的轻量提示：「Enter 确认 · Esc 取消」。

    必须是**独立顶层窗口**：框选发生在一个最大化的 ROISelector 里，主窗口的
    浮层全被它盖住，挂在主窗口下等于没显示。
    用 ``WindowTransparentForInput`` 保证它不吃鼠标，Enter / Esc 仍然由
    ROISelector 收到。
    """

    TEXT = "Enter 确认 · Esc 取消"

    def __init__(self):
        super().__init__(None)
        self.setObjectName("spotlightHint")
        self.setStyleSheet(_CARD_QSS)
        self.setWindowFlags(
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
            | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setText(self.TEXT)
        self.setContentsMargins(14, 8, 14, 8)
        self.hide()

    def show_avoiding(self, avoid: QRect) -> None:
        """显示在屏幕底部居中；与选区重叠时改放到选区上方。"""
        self.adjustSize()
        screen = QApplication.primaryScreen()
        area = screen.availableGeometry() if screen is not None else QRect(0, 0, 1920, 1080)

        x = area.center().x() - self.width() // 2
        y = area.bottom() - self.height() - 24
        rect = QRect(x, y, self.width(), self.height())

        if avoid.isValid() and rect.intersects(avoid):
            # 往上让，让到选区上方；上面也放不下就贴着选区左侧
            y = avoid.top() - self.height() - 12
            if y < area.top() + 12:
                y = area.bottom() - self.height() - 24
                x = avoid.left() - self.width() - 12
                if x < area.left() + 12:
                    x = avoid.right() + 12
            rect = QRect(x, y, self.width(), self.height())

        self.move(max(area.left() + 8, min(rect.x(), area.right() - self.width() - 8)),
                  max(area.top() + 8, min(rect.y(), area.bottom() - self.height() - 8)))
        self.show()
        self.raise_()


class GuideToast(QLabel):
    """引导完成的轻量提示：居中浮在主窗口上，约 2 秒后自行消失。

    做成独立顶层窗口的理由与 :class:`_RoiHint` 相同 —— 不参与主窗口布局，
    也就不会因为父控件的重排被挪走或裁掉。
    """

    def __init__(self, text: str = "✓ 引导完成，开始使用吧"):
        super().__init__(None)
        self.setObjectName("spotlightToast")
        self.setStyleSheet(_CARD_QSS)
        self.setWindowFlags(
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
            | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        self.setText(text)
        self.setContentsMargins(18, 10, 18, 10)
        self.hide()

    def show_over(self, window: QWidget, duration_ms: int = 2000) -> None:
        """显示在窗口正中间，`duration_ms` 之后自动隐藏。"""
        from PySide6.QtCore import QTimer

        self.adjustSize()
        center = window.mapToGlobal(window.rect().center())
        self.move(center.x() - self.width() // 2,
                  center.y() - self.height() // 2)
        self.show()
        self.raise_()
        QTimer.singleShot(duration_ms, self.hide)


class _ClickShield(QWidget):
    """盖在高亮区上的透明挡板：吞掉点击，界面其余部分不受影响。

    回顾模式只做"指示"，高亮的那颗按钮不该被真的按下去（按下去就是
    连接直播 / 启动 OCR 这类业务操作了）。挡板只覆盖高亮矩形本身，
    主窗口其它地方照常可用。
    """

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setCursor(Qt.ArrowCursor)
        self.hide()

    def mousePressEvent(self, event):  # noqa: N802
        event.accept()

    def mouseReleaseEvent(self, event):  # noqa: N802
        event.accept()

    def mouseDoubleClickEvent(self, event):  # noqa: N802
        event.accept()


class _SpotlightCard(QFrame):
    """聚光灯旁的深色提示卡片：标题 / 正文 / 进度 / 下一步 / 关闭。"""

    next_clicked = Signal()
    close_clicked = Signal()
    back_clicked = Signal()

    WIDTH = 340

    def __init__(self, title: str, body: str, step_index: int, step_count: int,
                 parent: QWidget, show_prev: bool = False,
                 next_text: str = "下一步"):
        super().__init__(parent)
        self.setObjectName("spotlightCard")
        self.setStyleSheet(_CARD_QSS)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedWidth(self.WIDTH)

        col = QVBoxLayout(self)
        col.setContentsMargins(16, 14, 16, 14)
        col.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(8)
        # 存成属性：测试与截图脚本要核对当前是哪一步
        self.title_label = QLabel(title)
        self.title_label.setObjectName("spotlightTitle")
        head.addWidget(self.title_label)
        head.addStretch(1)
        self.close_btn = QPushButton("✕")
        self.close_btn.setObjectName("spotlightClose")
        self.close_btn.setFixedSize(18, 18)
        self.close_btn.setCursor(Qt.PointingHandCursor)
        self.close_btn.setFocusPolicy(Qt.NoFocus)
        self.close_btn.setToolTip("关闭引导")
        self.close_btn.clicked.connect(self.close_clicked.emit)
        head.addWidget(self.close_btn)
        col.addLayout(head)

        self.body_label = QLabel(body)
        self.body_label.setObjectName("spotlightBody")
        self.body_label.setWordWrap(True)
        col.addWidget(self.body_label)

        foot = QHBoxLayout()
        foot.setSpacing(8)
        # 「上一步」只有能回到上一步的聚光灯才有（第一步没有）
        self.back_btn = QPushButton("上一步", self)
        self.back_btn.setFixedHeight(30)
        self.back_btn.setMinimumWidth(76)
        self.back_btn.setCursor(Qt.PointingHandCursor)
        self.back_btn.clicked.connect(self.back_clicked.emit)
        self.back_btn.setVisible(show_prev)
        foot.addWidget(self.back_btn)
        self.dots = StepDots(step_count)
        self.dots.set_current(step_index)
        foot.addWidget(self.dots)
        self.step_label = QLabel(f"第 {step_index + 1} 步 / 共 {step_count} 步")
        self.step_label.setObjectName("spotlightStep")
        foot.addWidget(self.step_label)
        foot.addStretch(1)
        self.next_btn = QPushButton(next_text)
        self.next_btn.setFixedHeight(30)
        self.next_btn.setMinimumWidth(92)
        self.next_btn.setCursor(Qt.PointingHandCursor)
        self.next_btn.clicked.connect(self.next_clicked.emit)
        foot.addWidget(self.next_btn)
        col.addLayout(foot)


class GuideSpotlight(QObject):
    """主界面上的交互式聚光灯引导：暗色遮罩 + 提示卡片。

    非模态 —— 遮罩不接收鼠标事件，用户可以直接操作被高亮的真实控件
    （第一步的地址框与「连接」，第二步的游戏下拉框）。这正是本步的目的：
    让用户上手，而不是看说明。

    前两步各用一个实例；「上一步」由调用方销毁当前实例、再建上一步的实例，
    因此本类不需要自己维护步骤栈。

    :param window:     主窗口；遮罩与卡片都是它的子控件
    :param targets:    需要保持高亮的真实控件
    :param title/body: 卡片文案
    :param step_index: 步骤号（STEPS 索引）
    :param show_prev:  是否显示「上一步」

    信号 :attr:`finished` 携带三种结果之一（与 :data:`GUIDE_ADVANCE` /
    :data:`GUIDE_ALWAYS` / :data:`GUIDE_NEVER` 对应）：
        ``GUIDE_ADVANCE`` 走完这一步（真实操作完成或点「下一步」），继续后续引导；
        ``GUIDE_ALWAYS``  提前关闭并选择「下次继续提醒」；
        ``GUIDE_NEVER``   提前关闭并选择「不再自动显示」。

    信号 :attr:`back_requested`：用户点了「上一步」。它**不**走 finished ——
    回退不是"结束引导"，不该按退出结果写盘。
    """

    finished = Signal(str)
    back_requested = Signal()

    def __init__(self, window: QWidget, targets, title: str, body: str,
                 step_index: int, step_count: int = len(STEPS),
                 show_prev: bool = False, prefers_left: bool = False,
                 next_text: str = "下一步", review_mode: bool = False):
        super().__init__(window)
        self._window = window
        self._targets = [w for w in targets if w is not None]
        self._finished = False
        self.step_index = step_index
        #: True = 卡片优先放高亮区左侧（目标在右侧面板时的正确选择）
        self.prefers_left = prefers_left
        #: 框选期间遮罩/卡片被临时收起（见 suspend / resume）
        self._suspended = False
        #: 框选画面的轻量提示（独立顶层窗口，见 _RoiHint）
        self._hint = _RoiHint()
        #: 回顾模式：只读的操作说明，不参与任何首次引导状态与自动推进
        self.review_mode = review_mode
        #: 高亮区的点击挡板（仅回顾模式创建）
        self._shields: list[_ClickShield] = (
            [_ClickShield(window) for _ in self._targets]
            if review_mode else [])

        # 顺序要紧：遮罩先建、卡片后建，卡片才在遮罩之上；
        # 两者是**兄弟**关系，卡片因此能正常收到鼠标事件。
        self.mask = _SpotlightMask(window)
        self.card = _SpotlightCard(
            title, body, step_index, step_count, window,
            show_prev=show_prev, next_text=next_text)
        self.card.next_clicked.connect(lambda: self.finish(GUIDE_ADVANCE))
        self.card.back_clicked.connect(self._on_back_requested)
        # ✕ 与旧版向导的 ✕ / Esc 走同一个确认逻辑
        self.card.close_clicked.connect(self._on_close_requested)

        self.mask.hide()
        self.card.hide()
        window.installEventFilter(self)

    # ------------------------------------------------------------------
    # 框选期间：临时收起 / 恢复 / 轻量提示
    # ------------------------------------------------------------------
    def suspend(self) -> None:
        """临时收起遮罩与卡片（进入 ROI 框选时用）。

        只是不可见，实例仍在：用户按 Esc 取消框选后要原样恢复，
        所以这里不做 _teardown。
        """
        if self._finished:
            return
        self._suspended = True
        self.mask.hide()
        self.card.hide()
        for shield in self._shields:
            shield.hide()

    def resume(self) -> None:
        """恢复被 :meth:`suspend` 收起的遮罩与卡片。"""
        if self._finished or not self._suspended:
            return
        self._suspended = False
        self.mask.show()
        self.mask.raise_()
        self.card.show()
        self.card.raise_()
        self.reposition()   # 里面会把挡板按当前高亮孔重新摆好

    def show_roi_hint(self, avoid: QRect) -> None:
        """在框选画面上显示「Enter 确认 · Esc 取消」，避开选区。"""
        if self._finished:
            return
        self._hint.show_avoiding(avoid)

    def hide_roi_hint(self) -> None:
        self._hint.hide()

    # ------------------------------------------------------------------
    def start(self) -> None:
        """显示遮罩与卡片，并把键盘焦点交给第一个高亮控件。"""
        self.mask.setGeometry(self._window.rect())
        self.mask.show()
        self.mask.raise_()
        self.card.show()
        self.card.raise_()
        self.reposition()
        if self._targets:
            # 直接落焦点：用户接下来要做的就是操作这个控件
            # （第一步是粘贴地址，第二步是选游戏）
            self._targets[0].setFocus()

    def reposition(self) -> None:
        """按高亮控件的**当前**位置重算遮罩孔与卡片位置。

        窗口缩放 / 最大化时主窗口会发 Resize（见 eventFilter），控件在窗口内
        的位置随之改变，这里跟着重算，不缓存任何坐标。
        """
        if self._suspended:
            return          # 框选期间浮层是收起的，不必重算
        self.mask.setGeometry(self._window.rect())

        holes: list[QRect] = []
        for target in self._targets:
            if not target.isVisible():
                continue
            top_left = target.mapTo(self._window, QPoint(0, 0))
            holes.append(
                QRect(top_left, target.size()).adjusted(
                    -_HOLE_PADDING, -_HOLE_PADDING, _HOLE_PADDING, _HOLE_PADDING))
        self.mask.set_holes(holes)

        # 挡板与高亮孔一一对应（回顾模式才存在），卡片始终压在它们之上
        for shield, hole in zip(self._shields, holes):
            shield.setGeometry(hole)
            shield.show()
            shield.raise_()
        if self._shields:
            self.card.raise_()

        self.card.adjustSize()
        self._place_card(holes)

    def _place_card(self, holes) -> None:
        """把卡片放到高亮区旁边，按 prefers 的顺序挑第一个放得下的方位。

        所有候选都必须完整落在窗口留白之内；一个都放不下时，退化为对首选
        方位做夹取 —— 宁可贴边，也不要让卡片跑出窗口。
        """
        card = self.card
        if not holes:
            card.move(_MARGIN, _MARGIN)
            return

        anchor = holes[0]
        right_limit = self._window.width() - card.width() - _MARGIN
        bottom_limit = self._window.height() - card.height() - _MARGIN
        sides = _CARD_SIDES_LEFT if self.prefers_left else _CARD_SIDES_BELOW

        # 每个方位只看**决定它的那一条轴**够不够放：
        #   上下方位看纵向，横向只是"对齐目标左边缘"，差几像素夹一下就好；
        #   左右方位看横向，纵向同理夹取。
        # 不能对两条轴一起判——第一步的卡片横向本就是贴着目标左缘（会差
        # 一点 padding），一起判会把它判成放不下，卡片就跳到右边去了。
        chosen = None
        for side in sides:
            x, y = _card_origin(side, anchor, card.width(), card.height())
            if side in ("below", "above"):
                fits = _MARGIN <= y <= bottom_limit
            else:
                fits = _MARGIN <= x <= right_limit
            if fits:
                chosen = (x, y)
                break
        if chosen is None:
            chosen = _card_origin(sides[0], anchor, card.width(), card.height())

        x, y = chosen
        card.move(max(_MARGIN, min(x, right_limit)),
                  max(_MARGIN, min(y, bottom_limit)))

    def eventFilter(self, obj, event):  # noqa: N802
        if obj is self._window:
            if event.type() in (QEvent.Type.Resize, QEvent.Type.Move):
                self.reposition()
            elif (event.type() == QEvent.Type.KeyPress
                    and event.key() == Qt.Key_Escape
                    and self.review_mode):
                # 只有回顾模式用 Esc 直接关；首次引导关闭走 ✕ 的三选项确认，
                # 这里不动它的既有行为。
                self.finish(GUIDE_REVIEW_CLOSED)
                return True
        return False

    # ------------------------------------------------------------------
    def notify_completed(self) -> None:
        """用户完成了本步要求的**真实操作**（连接成功 / 选好了游戏）。

        由主窗口在对应的业务回调里调用；调用方负责判断"当前是不是这一步"。

        **回顾模式下这里直接吞掉**：回顾是只读说明，用户看帮助时顺手连了个
        直播、换了个游戏，不该把引导往前推。把隔离放在这一个点上，主窗口
        那几个业务回调就不用各自再判一次模式，也就不会漏。
        """
        if self._finished or self.review_mode:
            return
        self.finish(GUIDE_ADVANCE)

    def _on_close_requested(self) -> None:
        """✕：回顾模式直接关；首次引导才问"下次还要不要提醒"。"""
        if self.review_mode:
            self.finish(GUIDE_REVIEW_CLOSED)
            return
        choice = ask_guide_exit(self._window)
        if choice is None:
            return          # 返回引导：什么都不做，聚光灯继续留在屏幕上
        self.finish(choice)

    def set_body(self, text: str) -> None:
        """改卡片正文（第四步用它补一句"当前缺少什么前置条件"）。"""
        self.card.body_label.setText(text)
        self.reposition()

    def _on_back_requested(self) -> None:
        """「上一步」：只拆掉本步，**不**发 finished。

        回退不是"结束引导"，走 finished 会被主窗口当成提前关闭去写
        «不再自动显示»，用户点一下上一步就被记成放弃引导了。
        """
        if self._finished:
            return
        self._teardown()
        self.back_requested.emit()

    def finish(self, outcome: str) -> None:
        """结束本步并对外报告结果。"""
        if self._finished:
            return
        self._teardown()
        self.finished.emit(outcome)

    def _teardown(self) -> None:
        """摘掉过滤器、彻底移除遮罩与卡片。

        先 hide 再 deleteLater：deleteLater 要等回到事件循环才真正析构，
        期间若还留着可见的浮层，用户会看到一层撤不掉的灰。
        """
        self._finished = True
        self._window.removeEventFilter(self)
        # 提示条是独立顶层窗口，不跟着主窗口销毁，必须显式收掉
        self._hint.hide()
        self._hint.deleteLater()
        for layer in (self.mask, self.card, *self._shields):
            layer.hide()
            # 先摘父子关系再 deleteLater：DeferredDelete 要等事件循环回到顶层
            # 才处理，只 hide() 的话控件会以"已隐藏但仍挂在窗口上"的状态多留
            # 一会儿。setParent(None) 让"移除"当场生效，恢复交互不依赖回收时机。
            layer.setParent(None)
            layer.deleteLater()

    @property
    def is_finished(self) -> bool:
        return self._finished


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
