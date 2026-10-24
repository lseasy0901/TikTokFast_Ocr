# -*- coding: utf-8 -*-
"""三点校准:用户在游戏里真实点击三次,LiveLens 轮询捕获并记录。

设计(2026-10-24 评审定稿,取代最初的"透明覆盖层"方案):
    - **轮询而非钩子**:15ms QTimer 轮询 GetAsyncKeyState(VK_LBUTTON)
      上升沿 + GetCursorPos。低级鼠标钩子在 Python 里有回调超时约束,
      轮询零依赖、零风险。
    - **所见即所得**:用户直接点游戏(真实点击、游戏真实响应),
      第一步点完输入框,粘贴浮层真的弹出,第二步才能校到浮层里的粘贴项。
      不存在"覆盖层吃掉点击导致浮层弹不出"的问题。
    - **坐标单轨**:GetCursorPos 是物理像素,与执行时(SendInput)同轨,
      DPI 缩放屏上不会出现 1.5 倍偏差。
    - **落点验证**:点击必须落在游戏窗口客户区内(WindowFromPoint
      取根窗口比对),点偏(点到别处)不记录、提示重试。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal

from automation.join_config import JoinConfig
from automation.win32 import WindowInfo, client_geometry, cursor_pos, find_game_window

logger = logging.getLogger("DouyinLowLatencyViewer.automation.calibration")

__all__ = ["CalibrationOverlay"]

_VK_LBUTTON = 0x01
_KEY_PRESSED = 0x8000
_POLL_MS = 15

import ctypes

user32 = ctypes.windll.user32
_GA_ROOT = 2

#: 三步的提示文案;第 N 步(N=1..3)
STEP_PROMPTS = (
    "第 1/3 步:切到游戏,点击「请输入房间码」输入框",
    "第 2/3 步:点击浮层里的「粘贴」项(把剪贴板内容贴入的那一项)",
    "第 3/3 步:点击「加入房间」按钮",
)


def _root_window_at(x: int, y: int) -> int:
    """物理坐标处的顶层窗口(WindowFromPoint 可能给子窗口,取根)。"""
    import ctypes.wintypes as wt

    pt = wt.POINT(x, y)
    hwnd = user32.WindowFromPoint(pt)
    if not hwnd:
        return 0
    root = user32.GetAncestor(hwnd, _GA_ROOT)
    return root or hwnd


class CalibrationOverlay(QObject):
    """校准会话(名字保留 Overlay 是历史叫法;现在没有任何覆盖窗口)。

    用法:
        cal = CalibrationOverlay(self)
        cal.stepChanged.connect(...)    # 更新引导 UI
        cal.invalidClick.connect(...)   # 提示"点偏了,请点游戏内"
        cal.finished.connect(...)       # (ok, message, JoinConfig|None)
        cal.start(config)
    """

    #: (step_index 0..2, prompt_text)
    stepChanged = Signal(int, str)
    #: 点击落点不在游戏窗口内(文本可直接展示)
    invalidClick = Signal(str)
    #: (ok, message, config)  config 仅成功时给(已保存)
    finished = Signal(bool, str, object)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.setInterval(_POLL_MS)
        self._timer.timeout.connect(self._poll)
        self._active = False
        self._step = 0
        self._was_down = False
        self._window: Optional[WindowInfo] = None
        self._points: list[tuple[float, float]] = []
        self._config: Optional[JoinConfig] = None

    # ------------------------------------------------------------------
    @property
    def active(self) -> bool:
        return self._active

    def start(self, config: JoinConfig) -> bool:
        """开始校准。前置:能找到游戏窗口(找不到立即 failed)。"""
        if self._active:
            return True
        win = find_game_window(config.window_class, config.window_title)
        if win is None:
            self.finished.emit(
                False,
                f"未找到游戏窗口({config.window_title or config.window_class})。"
                "请先打开游戏并进入「自定义房间」页面", None)
            return False
        self._window = win
        self._config = config
        self._step = 0
        self._points = []
        self._was_down = False
        self._active = True
        self.stepChanged.emit(0, STEP_PROMPTS[0])
        self._timer.start()
        logger.info("[CAL] 校准开始 target=%s", win)
        return True

    def cancel(self) -> None:
        """用户取消(幂等)。"""
        if self._active:
            self._active = False
            self._timer.stop()
            self.finished.emit(False, "已取消校准", None)

    # ------------------------------------------------------------------
    def _poll(self) -> None:
        """15ms 轮询:左键上升沿 + 光标位置 + 落点验证。"""
        if not self._active:
            return
        down = bool(user32.GetAsyncKeyState(_VK_LBUTTON) & _KEY_PRESSED)
        clicked = down and not self._was_down
        self._was_down = down
        if not clicked:
            return

        x, y = cursor_pos()
        if _root_window_at(x, y) != self._window.hwnd:
            self.invalidClick.emit("点击没落在游戏窗口内,请点游戏界面(要校准的位置)")
            return

        geo = client_geometry(self._window.hwnd)
        if geo is None:
            self._end(False, "游戏窗口已关闭", None)
            return
        ox, oy, w, h = geo
        if w <= 0 or h <= 0:
            self.invalidClick.emit("游戏窗口尺寸无效,请重试")
            return
        frac = ((x - ox) / w, (y - oy) / h)
        self._points.append(frac)
        logger.info("[CAL] step=%d 点=(%.3f, %.3f) px=(%d,%d)",
                    self._step + 1, frac[0], frac[1], x, y)

        self._step += 1
        if self._step < 3:
            self.stepChanged.emit(self._step, STEP_PROMPTS[self._step])
            return

        # 三点齐:写回配置并保存
        cfg = self._config
        cfg.input_point, cfg.paste_point, cfg.join_point = (
            self._points[0], self._points[1], self._points[2])
        cfg.calibrated_aspect = w / h
        cfg.calibrated_at = datetime.now(timezone.utc).isoformat(
            timespec="seconds")
        cfg.save()
        self._end(True, "校准完成:三点已记录", cfg)

    def _end(self, ok: bool, message: str, cfg: Optional[JoinConfig]) -> None:
        self._active = False
        self._timer.stop()
        self.finished.emit(ok, message, cfg)
