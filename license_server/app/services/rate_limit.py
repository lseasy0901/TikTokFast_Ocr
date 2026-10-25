# -*- coding: utf-8 -*-
"""轻量内存级速率限制与登录失败锁定(2026-10-25 加固,零新依赖)。

设计边界:
    - 单进程 uvicorn 部署(当前生产形态)下内存计数即准确;将来多 worker
      需换共享存储,届时本模块的调用点不变。
    - 速率限制只挂在"消耗性/探测性"入口(激活兑换),不给只读页加。
    - 登录锁定按 IP+用户名维度,内存态,重启即清(爆破者无损失,守门足够)。
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from threading import Lock

__all__ = ["SlidingWindowLimiter", "LoginThrottle"]


class SlidingWindowLimiter:
    """每 key 滑动窗口计数;超限返回 True(被限)。"""

    def __init__(self, max_events: int, window_seconds: float):
        self._max = max_events
        self._window = window_seconds
        self._events: dict[str, deque] = defaultdict(deque)
        self._lock = Lock()

    def hit(self, key: str) -> bool:
        """记录一次事件;返回是否超过限额(超限时该次仍被计入)。"""
        now = time.monotonic()
        with self._lock:
            q = self._events[key]
            while q and now - q[0] > self._window:
                q.popleft()
            q.append(now)
            return len(q) > self._max


class LoginThrottle:
    """登录失败锁定:窗口内连续失败 lock_threshold 次锁 lock_seconds。

    成功登录清零;锁定期内 is_locked 为 True(即使密码正确也拒绝)。
    """

    def __init__(self, max_failures: int = 5,
                 failure_window: float = 900.0,
                 lock_seconds: float = 600.0):
        self._max = max_failures
        self._window = failure_window
        self._lock_s = lock_seconds
        self._failures: dict[str, deque] = defaultdict(deque)
        self._locked_until: dict[str, float] = {}
        self._lock = Lock()

    def is_locked(self, key: str) -> bool:
        with self._lock:
            until = self._locked_until.get(key, 0.0)
            if until and time.monotonic() < until:
                return True
            if until:
                del self._locked_until[key]      # 锁过期,清掉
            return False

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            q = self._failures[key]
            while q and now - q[0] > self._window:
                q.popleft()
            q.append(now)
            if len(q) >= self._max:
                self._locked_until[key] = now + self._lock_s
                q.clear()

    def record_success(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
            self._locked_until.pop(key, None)
