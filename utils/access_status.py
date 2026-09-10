# -*- coding: utf-8 -*-
"""
访问状态管理器

管理试用/激活状态和倒计时
支持的状态：
- 免费试用
- 已激活
- 即将到期
- 已过期

功能：
- 本地状态管理（暂时用于演示）
- 倒计时更新
- 状态转换
- 通知回调
"""

import time
from datetime import datetime, timedelta
from enum import Enum
from typing import Callable, Optional


class AccessState(Enum):
    """访问状态枚举"""
    TRIAL = "trial"          # 免费试用
    ACTIVE = "active"        # 已激活
    EXPIRING = "expiring"    # 即将到期
    EXPIRED = "expired"       # 已过期


class AccessStatus:
    """访问状态管理器"""

    def __init__(self):
        self._state = AccessState.TRIAL
        self._expiry_time: Optional[datetime] = None
        self._trial_start: Optional[datetime] = None
        self._trial_days = 7  # 默认7天试用
        self._warning_hours = 24  # 到期前24小时显示警告
        self._update_timer: Optional[object] = None  # QTimer会通过设置注入
        self._on_status_changed: Optional[Callable] = None
        self._on_expired: Optional[Callable] = None

        # 初始化试用期开始时间
        self._trial_start = datetime.now()
        self._expiry_time = self._trial_start + timedelta(days=self._trial_days)

    def set_update_timer(self, timer):
        """设置更新定时器（由外部注入）"""
        self._update_timer = timer

    def set_callbacks(self, on_status_changed: Callable, on_expired: Callable):
        """设置回调函数"""
        self._on_status_changed = on_status_changed
        self._on_expired = on_expired

    def get_state(self) -> AccessState:
        """获取当前状态"""
        now = datetime.now()

        if self._state == AccessState.EXPIRED:
            return AccessState.EXPIRED

        if now >= self._expiry_time:
            return AccessState.EXPIRED

        time_until_expiry = self._expiry_time - now

        if time_until_expiry.total_seconds() <= 0:
            return AccessState.EXPIRED
        elif time_until_expiry.total_seconds() <= self._warning_hours * 3600:
            return AccessState.EXPIRING
        elif self._state == AccessState.ACTIVE:
            return AccessState.ACTIVE
        else:
            return AccessState.TRIAL

    def get_status_text(self) -> str:
        """获取状态文本"""
        state = self.get_state()
        now = datetime.now()

        if state == AccessState.EXPIRED:
            return "使用期限已到期"

        elif state == AccessState.ACTIVE:
            time_until_expiry = self._expiry_time - now
            days = int(time_until_expiry.total_seconds() // 86400)
            return f"已激活 · 剩余 {days}天"

        elif state == AccessState.EXPIRING:
            time_until_expiry = self._expiry_time - now
            hours = int(time_until_expiry.total_seconds() // 3600)
            return f"即将到期 · 剩余 {hours}小时"

        else:  # TRIAL
            time_until_expiry = self._expiry_time - now
            days = int(time_until_expiry.total_seconds() // 86400)
            hours = int((time_until_expiry.total_seconds() % 86400) // 3600)
            return f"免费试用 · 剩余 {days}天{hours}小时"

    def activate(self, days: int):
        """激活许可证"""
        self._state = AccessState.ACTIVE
        now = datetime.now()
        self._expiry_time = now + timedelta(days=days)
        self._notify_status_changed()

    def start_trial(self, days: int = 7):
        """开始试用"""
        self._state = AccessState.TRIAL
        now = datetime.now()
        self._trial_start = now
        self._expiry_time = now + timedelta(days=days)
        self._notify_status_changed()

    def reset_trial(self):
        """重置试用期"""
        self.start_trial(self._trial_days)

    def is_expired(self) -> bool:
        """是否已过期"""
        return self.get_state() == AccessState.EXPIRED

    def get_remaining_time(self) -> timedelta:
        """获取剩余时间"""
        now = datetime.now()
        return self._expiry_time - now

    def _notify_status_changed(self):
        """通知状态变化"""
        if self._on_status_changed:
            self._on_status_changed()

        # 如果过期，触发过期回调
        if self.get_state() == AccessState.EXPIRED and self._on_expired:
            self._on_expired()

    def start_countdown(self):
        """开始倒计时更新"""
        if self._update_timer:
            # 立即更新一次
            self._notify_status_changed()
            # 然后每分钟更新一次
            self._update_timer.start(60000)  # 60秒

    def stop_countdown(self):
        """停止倒计时"""
        if self._update_timer:
            self._update_timer.stop()