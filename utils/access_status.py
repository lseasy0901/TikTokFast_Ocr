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
from datetime import datetime, timedelta, timezone
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

    #: 访问状态重新评估周期（毫秒）。
    #: 只用于发现「应用运行中到期」，因此刻意取低频值（30-60s）。
    CHECK_INTERVAL_MS = 60000

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

        # 许可证状态（Phase 7.2-6.5）
        # 一旦由已通过本地验证的 SignedLicense 驱动，就覆盖试用期显示。
        # 有效期直接使用服务器签名的 expires 字段，客户端不做任何时长计算。
        self._license_active: bool = False
        self._license_expires_at: Optional[datetime] = None
        self._license_note: Optional[str] = None

    def set_update_timer(self, timer):
        """设置更新定时器（由外部注入）"""
        self._update_timer = timer

    def set_callbacks(self, on_status_changed: Callable, on_expired: Callable):
        """设置回调函数"""
        self._on_status_changed = on_status_changed
        self._on_expired = on_expired

    # ------------------------------------------------------------------
    # 许可证状态（Phase 7.2-6.5）
    # ------------------------------------------------------------------
    def set_license_active(self, expires_at: Optional[datetime]) -> None:
        """标记为已激活。

        :param expires_at: 服务器签名凭据里的 expires（UTC aware）；
                           None 表示永久授权。客户端不计算时长。
        """
        self._license_active = True
        self._license_expires_at = expires_at
        self._license_note = None
        self._notify_status_changed()

    def set_license_inactive(self, note: str) -> None:
        """标记为许可证不可用（未激活/无效/已过期/不属于本机）。

        :param note: 直接展示给用户的状态文案。
        """
        self._license_active = False
        self._license_expires_at = None
        self._license_note = note
        self._notify_status_changed()

    def clear_license_state(self) -> None:
        """回到未被许可证驱动的状态（仍按原有试用逻辑显示）。"""
        self._license_active = False
        self._license_expires_at = None
        self._license_note = None
        self._notify_status_changed()

    @property
    def is_license_controlled(self) -> bool:
        """当前显示是否由许可证状态决定。"""
        return self._license_active or self._license_note is not None

    def get_state(self) -> AccessState:
        """获取当前状态"""
        # 许可证优先：一旦有已激活凭据或明确的不激活原因，就覆盖试用逻辑
        if self._license_note is not None:
            return AccessState.EXPIRED

        if self._license_active:
            if self._license_expires_at is None:
                return AccessState.ACTIVE  # 永久授权
            remaining = (
                self._license_expires_at - datetime.now(timezone.utc)
            ).total_seconds()
            if remaining <= 0:
                return AccessState.EXPIRED
            if remaining <= self._warning_hours * 3600:
                return AccessState.EXPIRING
            return AccessState.ACTIVE

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
        # 许可证状态优先
        if self._license_note is not None:
            return self._license_note

        if self._license_active:
            if self._license_expires_at is None:
                return "已激活"
            remaining = (
                self._license_expires_at - datetime.now(timezone.utc)
            ).total_seconds()
            if remaining <= 0:
                return "许可证已过期"
            if remaining <= self._warning_hours * 3600:
                return f"即将到期 · 剩余 {int(remaining // 3600)}小时"
            return f"已激活 · 剩余 {int(remaining // 86400)}天"

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
        """是否已过期。

        这是受保护功能的【唯一访问判定入口】（Phase 7.2-6.6）：
        只要有效状态为 EXPIRED —— 无论它来自许可证（已过期 / 无效 /
        不属于本机）还是原有的试用倒计时 —— 调用方都应拒绝发起受保护动作。

        本方法只读取既有状态，不改变任何状态机行为。
        """
        return self.get_state() == AccessState.EXPIRED

    def get_remaining_time(self) -> timedelta:
        """获取剩余时间"""
        now = datetime.now()
        return self._expiry_time - now

    def _notify_status_changed(self):
        """通知状态变化"""
        if self._on_status_changed:
            self._on_status_changed()

        # 仅试用期到期才弹过期对话框。
        # 许可证自身的状态（无效/已过期/不属于本机）由顶部文案表达，
        # 不应弹出「免费试用已结束，请充值」这种与场景不符的提示。
        if self._license_note is None and self.get_state() == AccessState.EXPIRED and self._on_expired:
            self._on_expired()

    def start_countdown(self):
        """开始倒计时更新"""
        if self._update_timer:
            # 立即更新一次
            self._notify_status_changed()
            # 然后按 CHECK_INTERVAL_MS 周期重新评估
            self._update_timer.start(self.CHECK_INTERVAL_MS)

    def stop_countdown(self):
        """停止倒计时"""
        if self._update_timer:
            self._update_timer.stop()

    def refresh(self) -> None:
        """周期性重新评估（由专用定时器低频调用）。

        只通知展示层，用于发现「会话运行中到期」。

        刻意不触发过期弹窗：弹窗仍只由 _notify_status_changed 在状态
        真正变化时负责，避免每 60 秒重复弹出。
        """
        if self._on_status_changed:
            self._on_status_changed()