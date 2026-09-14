# -*- coding: utf-8 -*-
"""
访问状态管理器

管理授权状态。
支持的状态：
- 已激活
- 即将到期
- 已过期 / 未激活

功能：
- 本地状态管理（由已通过本地验证的许可证驱动）
- 倒计时更新
- 状态转换
- 通知回调

注意：7 天免费试用已移除（业务策略变更）。
本地没有有效授权时状态即为 EXPIRED，受保护功能一律拒绝，
用户必须先激活有效许可证才能正常使用。
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Optional


class AccessState(Enum):
    """访问状态枚举（不再有免费试用态）"""
    ACTIVE = "active"        # 已激活
    EXPIRING = "expiring"    # 即将到期
    EXPIRED = "expired"      # 已过期 / 未激活


#: 本地没有有效授权时的默认状态文案
NOT_ACTIVATED_TEXT = "未激活"


class AccessStatus:
    """访问状态管理器

    授权模型：状态完全由「已通过本地验证的签名凭据」驱动。
    免试用期、无时长兜底 —— 没有有效授权就没有任何可用时间。
    """

    #: 访问状态重新评估周期（毫秒）。
    #: 只用于发现「应用运行中到期」，因此刻意取低频值（30-60s）。
    CHECK_INTERVAL_MS = 60000

    def __init__(self):
        self._warning_hours = 24  # 到期前24小时显示警告
        self._update_timer: Optional[object] = None  # QTimer会通过设置注入
        self._on_status_changed: Optional[Callable] = None
        self._on_expired: Optional[Callable] = None

        # 授权状态（Phase 7.2-6.5）
        # 只有通过本地验证的 SignedLicense 才会让状态变为 ACTIVE；
        # 有效期直接使用服务器签名的 expires 字段，客户端不做任何时长计算。
        # 初始即为「未激活」：不再有试用期兜底。
        self._license_active: bool = False
        self._license_expires_at: Optional[datetime] = None
        self._license_note: Optional[str] = NOT_ACTIVATED_TEXT

    def set_update_timer(self, timer):
        """设置更新定时器（由外部注入）"""
        self._update_timer = timer

    def set_callbacks(self, on_status_changed: Callable, on_expired: Callable):
        """设置回调函数"""
        self._on_status_changed = on_status_changed
        self._on_expired = on_expired

    # ------------------------------------------------------------------
    # 授权状态（Phase 7.2-6.5）
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
        """标记为授权不可用（无效/已过期/不属于本机/激活失败）。

        :param note: 直接展示给用户的状态文案。
        """
        self._license_active = False
        self._license_expires_at = None
        self._license_note = note
        self._notify_status_changed()

    def clear_license_state(self) -> None:
        """回到「本地没有可用授权」的状态（未激活 → 拒绝使用）。"""
        self._license_active = False
        self._license_expires_at = None
        self._license_note = NOT_ACTIVATED_TEXT
        self._notify_status_changed()

    def get_state(self) -> AccessState:
        """获取当前状态。

        没有已通过本地验证的授权时一律为 EXPIRED（未激活同样受保护功能拒绝）。
        """
        if not self._license_active:
            return AccessState.EXPIRED

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

    def get_status_text(self) -> str:
        """获取状态文本"""
        if not self._license_active:
            return self._license_note or NOT_ACTIVATED_TEXT

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

    def is_expired(self) -> bool:
        """是否不可用（已过期 / 无效 / 未激活）。

        这是受保护功能的【唯一访问判定入口】：
        只要有效状态为 EXPIRED，调用方都应拒绝发起受保护动作。

        本方法只读取既有状态，不改变任何状态机行为。
        """
        return self.get_state() == AccessState.EXPIRED

    def _notify_status_changed(self):
        """通知状态变化"""
        if self._on_status_changed:
            self._on_status_changed()

        # 只有「曾经激活过的授权在运行中掉到 EXPIRED」才弹过期对话框。
        # 未激活 / 无效 / 不属于本机的状态由顶部文案表达，
        # 不应弹出与场景不符的提示。
        if self._license_active and self.get_state() == AccessState.EXPIRED and self._on_expired:
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
