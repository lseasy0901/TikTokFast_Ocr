# -*- coding: utf-8 -*-
"""未激活状态下的动态 LiveLens 水印（Phase 5）。

设计约束（对应任务书 Phase 5 与 WATERMARK_SPEC.md）：

* **独立 UI 覆盖层**：注册到 ``OverlayHost``，不参与 ``VideoWidget`` 的绘制。
* **不碰视频链路**：不读帧、不改帧、不进 OCR/ROI 输入、不做缩放或拷贝。
* **不承载授权逻辑**：本模块**不导入任何许可模块**，只消费一个布尔量
  "当前是否处于未激活状态"。状态的权威来源仍是既有许可系统。
* **鼠标穿透**：由 ``OverlayHost.add_overlay(..., interactive=False)``
  统一设置 ``WA_TransparentForMouseEvents``，预览区交互不受影响。
* **动画与帧率解耦**：自己的 ``QTimer`` 驱动，隐藏即停表，不空转。

运动算法抽成纯函数 :func:`bounce_step`，便于确定性测试。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QPoint, QRect, QRectF, QSize, QTimer, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QWidget

from gui.theme import Palette

logger = logging.getLogger("DouyinLowLatencyViewer.gui.watermark")


def bounce_step(x: float, y: float, vx: float, vy: float,
                w: float, h: float, W: float, H: float
                ) -> tuple[float, float, float, float]:
    """把水印推进一帧并处理边界反弹（纯函数）。

    合法范围（左上角坐标）::

        0 <= x <= W - w
        0 <= y <= H - h

    规则：

    * 越过左右边界 → 修正位置到边界并反转水平速度；
    * 越过上下边界 → 修正位置到边界并反转垂直速度；
    * **两轴独立处理**，因此角落处两个方向同时反转；
    * 速度用 ``abs()`` 强制方向，避免连续越界后符号错乱（抖动/卡死）；
    * ``W - w <= 0``（水印比预览区还宽）时**冻结该轴**：
      此时"反弹"在数学上无意义，任何积分都会立刻越界并被钳回 0，
      逐帧钳位会表现为抖动，因此直接置 0 并停速。

    :returns: ``(x, y, vx, vy)``
    """
    # --- 水平轴 ---
    if W - w <= 0:
        x, vx = 0.0, 0.0
    else:
        x += vx
        if x < 0.0:
            x, vx = 0.0, abs(vx)
        elif x > W - w:
            x, vx = float(W - w), -abs(vx)

    # --- 垂直轴 ---
    if H - h <= 0:
        y, vy = 0.0, 0.0
    else:
        y += vy
        if y < 0.0:
            y, vy = 0.0, abs(vy)
        elif y > H - h:
            y, vy = float(H - h), -abs(vy)

    return x, y, vx, vy


class WatermarkOverlay(QWidget):
    """在预览区内部弹跳的 ``LiveLens`` 水印（DVD 屏保式）。

    只做三件事：知道自己该不该显示、按纯函数推进位置、把文字画在自己身上。
    """

    TEXT = "LiveLens"
    #: 刷新间隔。实测：每帧真实成本约 449 µs（含 Qt 重绘），
    #: 40ms/25fps → 约 1.1% 单核；33ms/30fps → 约 1.4%。
    #: 运动是约 60 px/s 的缓慢漂移，25fps 与 30fps 肉眼无差别，
    #: 因此取 40ms（任务书要求"先评估刷新间隔，避免不必要的高频重绘"）。
    TICK_MS = 40
    #: 水印与"可视画面矩形"之间保留的安全距离
    MARGIN = 12
    #: 每 tick 位移（px）。刻意取小值：明显但不干扰观看。
    SPEED = 2.4
    #: 内边距
    PAD_X = 12
    PAD_Y = 6

    def __init__(self, video_widget: QWidget | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._video_widget = video_widget
        self._denied = False
        self._x = 0.0
        self._y = 0.0
        self._vx = self.SPEED
        self._vy = self.SPEED * 0.72      # 斜向运动，避免纯水平/垂直的呆板感
        #: 测试注入用：非 None 时优先于自动计算
        self._bounds_override: QRect | None = None
        #: 水印尺寸缓存（字体运行期不变，见 watermark_size()）
        self._size_cache: tuple[int, int] | None = None

        self.setObjectName("watermarkOverlay")
        # 本浮层是**子控件**，父容器（OverlayHost）是不透明的：
        # 子控件只要不填自己的背景，父内容自然透出来，因此**不需要**
        # WA_TranslucentBackground / WA_NoSystemBackground ——
        # 这两个属性是给顶层窗口用的。若设了 WA_NoSystemBackground，
        # Qt 不会擦除本控件区域，水印移动后旧位置会留下残影。
        self.setAutoFillBackground(False)
        # 兜底：即使调用方忘了用 add_overlay(interactive=False)，也不抢鼠标
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.NoFocus)

        self._font = QFont()
        self._font.setPixelSize(16)
        self._font.setBold(True)
        if Palette.FONT_FAMILY:
            # 复用全局字体栈（含中文字体回退），但去掉 QSS 的引号写法
            families = [f.strip().strip('"')
                        for f in Palette.FONT_FAMILY.split(",")]
            self._font.setFamilies([f for f in families if f])

        self._timer = QTimer(self)
        self._timer.setInterval(self.TICK_MS)
        self._timer.timeout.connect(self._on_tick)

    # ------------------------------------------------------------------
    # 尺寸
    # ------------------------------------------------------------------
    def sizeHint(self) -> QSize:  # noqa: N802
        w, h = self.watermark_size()
        return QSize(w, h)

    def watermark_size(self) -> tuple[int, int]:
        """水印实际尺寸 (w, h)。

        由字体度量推导。**带缓存**：``step()`` 每帧都要取它，而
        ``QFontMetrics`` 的构造与测量在每帧路径上是纯浪费。
        字体在运行期不变，因此只需算一次。
        """
        if self._size_cache is None:
            fm = QFontMetrics(self._font)
            w = fm.horizontalAdvance(self.TEXT) + self.PAD_X * 2
            h = fm.height() + self.PAD_Y * 2
            self._size_cache = (int(w), int(h))
        return self._size_cache

    # ------------------------------------------------------------------
    # 状态（只消费一个布尔量，不做任何授权判断）
    # ------------------------------------------------------------------
    def set_denied(self, denied: bool) -> None:
        """由主窗口在授权状态变化时调用。

        :param denied: True = 当前未激活 → 显示并运动；False = 已激活 → 立即
            隐藏并停止计时器。
        """
        denied = bool(denied)
        if denied == self._denied:
            # 状态没变也要保证显示/计时与状态一致（例如首次调用时尚未显示）
            self._sync_visibility()
            return
        self._denied = denied
        self._sync_visibility()

    def is_denied(self) -> bool:
        return self._denied

    def _sync_visibility(self) -> None:
        if not self._denied:
            # 已激活：立即隐藏 + 停表
            self.hide()
            return
        bounds = self._bounds()
        if bounds.width() <= 0 or bounds.height() <= 0:
            # 预览区尚未布局（尺寸为 0）：不启动，等 resize 再试
            self.hide()
            return
        if not self.isVisible():
            self.show()
        # 必须显式 raise_()。
        #
        # 不能依赖"注册顺序"决定层级：未激活遮罩在 _sync_activation_overlay()
        # 里每次都调用 raise_() 把自己提到最上层，因此本浮层即使后注册也会被
        # 它盖住（实测：遮罩可见时水印区峰值 66，raise_() 后 285）。
        # 主窗口的调用顺序是 _sync_activation_overlay() → _sync_watermark()，
        # 所以在这里 raise_() 恰好把水印放到遮罩之上。
        #
        # 层级在水印之上的只有它自己；遮罩上的「立即激活」按钮依然可点，
        # 因为本浮层是 mouse-transparent（WA_TransparentForMouseEvents）。
        self.raise_()
        self._clamp_into(bounds)
        self._start()

    # ------------------------------------------------------------------
    # 计时器
    # ------------------------------------------------------------------
    def _start(self) -> None:
        if self._denied and not self._timer.isActive():
            self._timer.start()

    def _stop(self) -> None:
        if self._timer.isActive():
            self._timer.stop()

    def is_animating(self) -> bool:
        """测试/验收用：动画是否在跑。"""
        return self._timer.isActive()

    def _on_tick(self) -> None:
        self.step()

    def step(self) -> None:
        """推进一帧并重绘。测试可直接调用（不依赖计时器）。"""
        bounds = self._bounds()
        w, h = self.watermark_size()
        if bounds.width() <= 0 or bounds.height() <= 0:
            return
        old = self.watermark_rect()
        self._x, self._y, self._vx, self._vy = bounce_step(
            self._x, self._y, self._vx, self._vy, w, h,
            bounds.width(), bounds.height(),
        )
        # 只重绘"旧位置 ∪ 新位置"：既不整层重绘（成本），
        # 也不会把旧位置的像素留在屏幕上（残影）。
        self.update(old.united(self.watermark_rect()))

    # ------------------------------------------------------------------
    # 边界
    # ------------------------------------------------------------------
    def set_bounds_override(self, rect: QRect | None) -> None:
        """测试注入：强制使用给定边界（控件坐标系）。"""
        self._bounds_override = rect
        if rect is not None:
            self._clamp_into(rect)

    def _bounds(self) -> QRect:
        """水印允许活动的矩形（控件坐标系）。

        优先取**视频可视矩形**（已映射到本控件坐标），并内缩 MARGIN，
        这样水印不会压到上下/左右黑边上；没有帧时就是整个容器。
        """
        if self._bounds_override is not None:
            return self._bounds_override

        base = self.rect()
        vw = self._video_widget
        if vw is not None and vw.isVisible():
            try:
                offset = vw.mapTo(self, QPoint(0, 0))
                visible = vw.visible_video_rect().translated(offset)
                base = visible.intersected(self.rect())
            except Exception:  # noqa: BLE001 - 几何查询失败时退回整容器
                base = self.rect()
        if base.width() <= 0 or base.height() <= 0:
            return base
        inset = self.MARGIN
        shrunk = base.adjusted(inset, inset, -inset, -inset)
        # 容器本身比两倍 MARGIN 还小：不内缩，否则合法范围会变负
        return shrunk if shrunk.width() > 0 and shrunk.height() > 0 else base

    def _clamp_into(self, bounds: QRect) -> None:
        """把当前位置限制在合法范围内（**不**反转速度 —— 这是位置修正）。"""
        w, h = self.watermark_size()
        max_x = max(0, bounds.width() - w)
        max_y = max(0, bounds.height() - h)
        self._x = min(max(self._x, 0.0), float(max_x))
        self._y = min(max(self._y, 0.0), float(max_y))

    def watermark_rect(self) -> QRect:
        """水印当前所在的矩形（控件坐标系）。"""
        bounds = self._bounds()
        w, h = self.watermark_size()
        return QRect(int(bounds.x() + self._x), int(bounds.y() + self._y), w, h)

    # ------------------------------------------------------------------
    # Qt 事件
    # ------------------------------------------------------------------
    def resizeEvent(self, event) -> None:  # noqa: N802
        """预览区尺寸变化：重算合法范围并把位置钳回去。"""
        super().resizeEvent(event)
        if self._denied:
            self._sync_visibility()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if self._denied:
            self._start()

    def hideEvent(self, event) -> None:  # noqa: N802
        # 隐藏即停表：隐藏状态下不得继续空转（任务书 Phase 5 §5.3）
        super().hideEvent(event)
        self._stop()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._stop()
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # 绘制
    # ------------------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802
        # 水印在**控件坐标系**中的当前位置。paintEvent 必须用它做平移，
        # 否则会画在控件左上角（本控件被 OverlayHost 铺满整个预览容器，
        # 左上角并不等于水印该在的位置）。
        target = self.watermark_rect()
        w, h = target.width(), target.height()

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.translate(target.x(), target.y())

        rect = QRectF(0.5, 0.5, w - 1.0, h - 1.0)

        # 玻璃感底：极淡的深色填充 + 细描边 + 顶部高光
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(9, 11, 16, 90))
        painter.drawRoundedRect(rect, 6, 6)

        pen = QPen(QColor(255, 255, 255, 52))
        pen.setWidthF(1.0)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(rect, 6, 6)

        # 顶部高光：一条 1px 的浅色线，营造"玻璃边缘"的观感
        painter.setPen(QPen(QColor(255, 255, 255, 28)))
        painter.drawLine(6, 1, int(rect.right()) - 6, 1)

        # 文字：半透明银白，既能看清又不喧宾夺主
        painter.setPen(QPen(QColor(233, 240, 255, 92)))
        painter.setFont(self._font)
        painter.drawText(QRectF(0, 0, w, h), Qt.AlignCenter, self.TEXT)
        painter.end()
