# -*- coding: utf-8 -*-
"""加入序列执行器:定位窗口 → 激活 → 三步点击(逐步验证,可中止)。

执行模型:QTimer 链(每步之间留 UI 响应延时),不阻塞 GUI 线程。
安全护栏(全部来自 2026-10-24 设计评审):
    1. 每步点击前验证 GetForegroundWindow == 游戏窗口,不是立即中止
       —— 绝不在错误窗口上乱点;
    2. 激活失败(验证超时)不进入点击序列;
    3. 执行期间忽略重入(再按热键无效);
    4. 客户区宽高比与校准值偏差 >5% 时先警告(事件里带 warning,
       由 UI 决定 toast 内容;序列继续,用户在现场可判断)。
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal

from automation.join_config import JoinConfig
from automation.win32 import (
    activate_window,
    client_geometry,
    click_point,
    find_game_window,
    is_foreground,
)

logger = logging.getLogger("DouyinLowLatencyViewer.automation.join_controller")

__all__ = ["JoinController", "JoinResult"]

_ASPECT_TOLERANCE = 0.05


class JoinResult:
    """一次执行的终态(给 UI 做 toast 文案,不在这里弹窗)。"""

    __slots__ = ("ok", "reason", "warning")

    def __init__(self, ok: bool, reason: str = "", warning: str = ""):
        self.ok = ok
        self.reason = reason
        self.warning = warning

    def __repr__(self) -> str:  # pragma: no cover
        return f"JoinResult(ok={self.ok}, reason={self.reason!r})"


def frac_to_pixel(frac: tuple[float, float],
                  geometry: tuple[int, int, int, int]) -> tuple[int, int]:
    """比例坐标 + 客户区几何 → 屏幕物理像素(纯函数,单测覆盖)。"""
    ox, oy, w, h = geometry
    fx, fy = frac
    return int(round(ox + fx * w)), int(round(oy + fy * h))


class JoinController(QObject):
    """单例执行器(主窗口持有);一次 join 从头走到尾,期间不可重入。"""

    #: 序列结束(成功或失败);JoinResult
    finished = Signal(object)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._running = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._next_step)
        self._steps: list[tuple[int, callable]] = []
        self._warning = ""

    # ------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._running

    def join(self, config: JoinConfig) -> bool:
        """启动一次加入序列。返回 False = 前置检查未过(未开始执行)。

        前置检查(不满足就直接 finished,不动鼠标):
            已校准 / 找得到窗口 / 能激活到前台。
        """
        if self._running:
            logger.info("序列执行中,忽略重复触发")
            return True
        if not config.is_calibrated:
            self.finished.emit(JoinResult(False, "尚未校准,请到设置·自动加入校准"))
            return False

        win = find_game_window(config.window_class, config.window_title)
        if win is None:
            self.finished.emit(JoinResult(
                False, f"未找到游戏窗口({config.window_title or config.window_class})"))
            return False
        geo = client_geometry(win.hwnd)
        if geo is None:
            self.finished.emit(JoinResult(False, "游戏窗口无效"))
            return False

        # 宽高比漂移检测(布局变了比例点可能不准;警告不阻断)
        ox, oy, w, h = geo
        aspect = w / h if h else 0.0
        if config.calibrated_aspect and abs(
                aspect - config.calibrated_aspect) / config.calibrated_aspect > _ASPECT_TOLERANCE:
            self._warning = (
                f"游戏窗口比例与校准时不同({aspect:.2f} vs "
                f"{config.calibrated_aspect:.2f}),点击可能偏移")

        # 激活是硬门槛:失败绝不点击(会点进当时的前台应用)
        if not activate_window(win.hwnd):
            self.finished.emit(JoinResult(False, "无法把游戏切到前台(点击已中止)"))
            return False

        self._hwnd = win.hwnd
        self._geo = geo
        self._config = config
        steps: list[tuple[int, callable]] = [
            (config.delay_activate_ms, self._click_input),
            (config.delay_click_ms, self._click_paste),
        ]
        if config.auto_confirm and config.join_point:
            steps.append((config.delay_click_ms, self._click_join))
        self._steps = steps
        self._running = True
        logger.info("[JOIN] 序列开始 hwnd=%s geo=%s steps=%d%s",
                    win.hwnd, geo, len(steps),
                    f" warning={self._warning}" if self._warning else "")
        self._timer.start(self._steps[0][0] if self._steps else 0)
        return True

    # ------------------------------------------------------------------
    def _next_step(self) -> None:
        """执行当前步 → 验证 → 排下一步;每步前都重新验证前台与几何。"""
        if not self._steps:
            self._finish(True, "已完成")
            return
        _delay, action = self._steps.pop(0)
        # 前台验证(安全关键):被切走就中止
        if not is_foreground(self._hwnd):
            self._finish(False, "游戏窗口已不在前台,序列中止")
            return
        # 窗口可能被移动/缩放:每步重取几何,比例点实时换算
        geo = client_geometry(self._hwnd)
        if geo is None:
            self._finish(False, "游戏窗口已关闭")
            return
        self._geo = geo
        try:
            ok, stop_reason = action()
        except Exception as e:  # noqa: BLE001 - 任何异常都收序列
            logger.exception("[JOIN] 步骤异常")
            self._finish(False, f"执行异常:{e}")
            return
        if not ok:
            self._finish(False, stop_reason or "点击失败")
            return
        if self._steps:
            self._timer.start(self._steps[0][0])
        else:
            # 最后一步之后留一点时间让 UI 响应,再报完成
            QTimer.singleShot(self._config.delay_click_ms,
                              lambda: self._finish(True, "已执行加入"))

    def _guard_click(self, frac: tuple[float, float], label: str):
        """比例点换算 → 点击。返回 (ok, stop_reason)。"""
        x, y = frac_to_pixel(frac, self._geo)
        if not click_point(x, y):
            return False, f"点击{label}失败(输入系统拒绝)"
        return True, ""

    def _click_input(self):
        return self._guard_click(self._config.input_point, "输入框")

    def _click_paste(self):
        return self._guard_click(self._config.paste_point, "粘贴项")

    def _click_join(self):
        return self._guard_click(self._config.join_point, "加入按钮")

    def _finish(self, ok: bool, reason: str) -> None:
        self._running = False
        self._timer.stop()
        logger.info("[JOIN] 结束 ok=%s reason=%s", ok, reason)
        self.finished.emit(JoinResult(ok, reason, self._warning))
        self._warning = ""
