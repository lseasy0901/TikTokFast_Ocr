# -*- coding: utf-8 -*-
"""速率限制与登录锁定的纯逻辑回归(2026-10-25 加固)。"""

import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from services.rate_limit import LoginThrottle, SlidingWindowLimiter


def test_sliding_window_allows_within_limit():
    lim = SlidingWindowLimiter(max_events=3, window_seconds=60)
    assert not lim.hit("a")
    assert not lim.hit("a")
    assert not lim.hit("a")


def test_sliding_window_blocks_over_limit():
    lim = SlidingWindowLimiter(max_events=3, window_seconds=60)
    for _ in range(3):
        lim.hit("a")
    assert lim.hit("a") is True          # 第 4 次超限
    assert not lim.hit("b")              # 其它 key 不受累


def test_login_throttle_locks_after_failures():
    t = LoginThrottle(max_failures=3, failure_window=900, lock_seconds=600)
    assert not t.is_locked("ip1")
    t.record_failure("ip1")
    t.record_failure("ip1")
    assert not t.is_locked("ip1")        # 2 次未锁
    t.record_failure("ip1")
    assert t.is_locked("ip1")            # 第 3 次锁定


def test_login_throttle_success_resets():
    t = LoginThrottle(max_failures=3, failure_window=900, lock_seconds=600)
    t.record_failure("ip1")
    t.record_failure("ip1")
    t.record_success("ip1")              # 成功清零
    t.record_failure("ip1")
    assert not t.is_locked("ip1")        # 清零后重新计数
