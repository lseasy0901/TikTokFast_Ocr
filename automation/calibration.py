# -*- coding: utf-8 -*-
"""三点校准:用户在游戏里真实点击三次,后台线程轮询捕获并记录。

设计(2026-10-24 评审定稿 + 实机故障修正):
    - **独立线程轮询**(v2 修正):第一版把 15ms QTimer 挂在 GUI 主线程,
      实机直播流+OCR 运行时主线程被视频帧占满,轮询间隔被拉长到远超
      一次点击的持续时间(~100ms),点击全部漏检(2026-10-24 用户实测
      "卡在第 1 步")。现在用专用 QThread 做 5ms 纯 Win32 轮询
      (GetAsyncKeyState/GetCursorPos 线程安全),点击信号发回主线程。
    - **轮询而非钩子**:WH_MOUSE_LL 在 Python 有回调超时约束;线程轮询
      简单可靠。5ms 间隔 + 300ms 冷却(防一次双击被记成两点)。
    - **所见即所得**:用户直接点游戏(真实点击、游戏真实响应),
      第一步点完输入框,粘贴浮层真的弹出,第二步才能校到浮层里的粘贴项。
    - **坐标单轨**:GetCursorPos 是物理像素,与执行时(SendInput)同轨。
    - **落点验证**:点击必须落在游戏窗口客户区内(WindowFromPoint
      取根窗口比对),点偏(点到别处)不记录、提示重试。
"""

from __future__ import annotations

import ctypes
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from PySide6.QtCore import QObject, QThread, Signal

from automation.join_config import JoinConfig
from automation.win32 import WindowInfo, client_geometry, cursor_pos, find_game_window

logger = logging.getLogger("DouyinLowLatencyViewer.automation.calibration")

__all__ = ["CalibrationOverlay"]

_VK_LBUTTON = 0x01
_KEY_PRESSED = 0x8000
_POLL_INTERVAL_S = 0.005     # 5ms:远小于一次点击的按住时长
_CLICK_COOLDOWN_S = 0.30     # 双击/抖动防重

user32 = ctypes.windll.user32
_GA_ROOT = 2

#: 两步的提示文案(2026-10-25 用户截图澄清:粘贴是常驻小按钮,两步即达)
STEP_PROMPTS = (
    "第 1/2 步:点击输入框旁的「粘贴」小按钮(点击即贴入剪贴板内容)",
    "第 2/2 步:点击「加入房间」按钮",
)



class _ClickPollThread(QThread):
    """后台左键点击检测(纯 Win32 只读调用,线程安全)。

    必须独立于 GUI 线程:主线程在直播流+OCR 下被视频帧回调占满,
    QTimer 轮询会漏检点击(2026-10-24 实测故障)。
    """

    #: 检测到一次左键点击(物理像素坐标)
    clicked = Signal(int, int)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._stop = False

    def run(self) -> None:
        was_down = False
        last_click = 0.0
        while not self._stop:
            down = bool(user32.GetAsyncKeyState(_VK_LBUTTON) & _KEY_PRESSED)
            now = time.perf_counter()
            if (down and not was_down
                    and now - last_click >= _CLICK_COOLDOWN_S):
                last_click = now
                x, y = cursor_pos()
                logger.info("[CAL] 检测到点击 px=(%d,%d)", x, y)
                self.clicked.emit(x, y)
            was_down = down
            time.sleep(_POLL_INTERVAL_S)

    def stop(self) -> None:
        self._stop = True
        self.wait(int((_POLL_INTERVAL_S + 0.05) * 1000))


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
        self._poller: Optional[_ClickPollThread] = None
        self._active = False
        self._step = 0
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
        self._active = True
        self._poller = _ClickPollThread(self)
        self._poller.clicked.connect(self._on_click)
        self._poller.start()
        self.stepChanged.emit(0, STEP_PROMPTS[0])
        logger.info("[CAL] 校准开始 target=%s", win)
        return True

    def cancel(self) -> None:
        """用户取消(幂等)。"""
        if self._active:
            self._end(False, "已取消校准", None)

    # ------------------------------------------------------------------
    def _on_click(self, x: int, y: int) -> None:
        """轮询线程报告一次点击(本槽在主线程执行,可安全动 UI/状态)。"""
        if not self._active:
            return
        geo = client_geometry(self._window.hwnd)
        if geo is None:
            self._end(False, "游戏窗口已关闭", None)
            return
        ox, oy, w, h = geo
        if w <= 0 or h <= 0:
            self.invalidClick.emit("游戏窗口尺寸无效,请重试")
            return
        # 落点验证 v2:坐标是否落在游戏客户区矩形内。
        # v1 用 WindowFromPoint 比对根窗口 hwnd,实机翻车:主线程满载时
        # 点击信号延迟数百毫秒才处理,期间游戏在点击处弹出了浮层
        # (独立顶层窗),WindowFromPoint 返回浮层而非游戏根窗口,全部被
        # 误拒(2026-10-24 实测,5 次点击全被拒)。矩形包含判定与"谁盖在
        # 上面"无关,坐标是点击时刻抓的,延迟处理不影响正确性。
        if not (ox <= x < ox + w and oy <= y < oy + h):
            logger.info("[CAL] 点击落点不在游戏客户区 px=(%d,%d) rect=(%d,%d,%d,%d)",
                        x, y, ox, oy, w, h)
            self.invalidClick.emit("点击没落在游戏窗口内,请点游戏界面(要校准的位置)")
            return
        frac = ((x - ox) / w, (y - oy) / h)
        self._points.append(frac)
        logger.info("[CAL] step=%d 点=(%.3f, %.3f) px=(%d,%d)",
                    self._step + 1, frac[0], frac[1], x, y)

        self._step += 1
        if self._step < 2:
            self.stepChanged.emit(self._step, STEP_PROMPTS[self._step])
            return

        # 两点齐:写回配置并保存
        cfg = self._config
        cfg.paste_point, cfg.join_point = (
            self._points[0], self._points[1])
        cfg.calibrated_aspect = w / h
        cfg.calibrated_at = datetime.now(timezone.utc).isoformat(
            timespec="seconds")
        cfg.save()
        self._end(True, "校准完成:三点已记录", cfg)

    def _end(self, ok: bool, message: str, cfg: Optional[JoinConfig]) -> None:
        self._active = False
        if self._poller is not None:
            self._poller.stop()
            self._poller.deleteLater()
            self._poller = None
        self.finished.emit(ok, message, cfg)
