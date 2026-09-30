# -*- coding: utf-8 -*-
"""
SQLAlchemy database models
"""

from datetime import datetime, timedelta
from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import hashlib
import enum

import database
from database import Base


class AuthorizationState(str, enum.Enum):
    """Authorization state enumeration"""
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


class UpdateRelease(Base):
    """一次已发布的自动更新（Phase 2）。

    ``package_share_url`` 保存**永久**公开 URL，两种后端（v2.1.0 起支持对象存储）：
        - OSS（阿里云对象存储，公共读桶）：值 = 永久公开直链。永久有效，入库
          不违反脱敏原则 —— 脱敏针对的是**有时效**的临时 direct_url。
        - Feijipan（遗留）：值 = 永久分享链接；临时 direct_url 仍由 resolver 在
          每次下载时实时换取，绝不入库。
    下载端点按 URL host 分流（见 main._redirect_package）。

    ``(channel, version)`` 唯一：同一渠道同一版本不允许存在多个 release。

    ``package_sha1`` / ``package_sha256`` / ``package_size`` 是**必须**的，
    因为 Velopack 的 ``releases.<channel>.json`` 本身就要带这些字段，客户端下载
    后会用它校验包的完整性。它们的权威来源只有一个：打包工具 ``vpk pack`` 的输出
    （见 scripts/velopack_release.py 生成的 feed）。服务端不下载 .nupkg，因此无法
    自行计算 —— 这也是发布接口要求调用方提供它们的原因。
    """

    __tablename__ = "update_releases"
    __table_args__ = (
        UniqueConstraint("channel", "version", name="uq_update_release_channel_version"),
    )

    id = Column(Integer, primary_key=True, index=True)
    #: MAJOR.MINOR.PATCH，与 utils/version.py 的约定一致（纯数字三段式，可数值比较）。
    version = Column(String(32), nullable=False, index=True)
    #: 渠道。本阶段只支持 stable。
    channel = Column(String(16), nullable=False, index=True, default="stable")
    #: Velopack 包文件名，同时就是客户端会请求的那个文件名。
    package_filename = Column(String(255), nullable=False)
    #: 永久分享链接（唯一被保存的 URL）。禁止保存 direct_url / CDN URL / 临时 token。
    package_share_url = Column(String(512), nullable=False)
    package_sha1 = Column(String(40), nullable=False)
    package_sha256 = Column(String(64), nullable=False)
    package_size = Column(Integer, nullable=False)
    release_notes = Column(Text, nullable=True)
    published_at = Column(DateTime, default=func.now(), nullable=False)

    # ---- 增量包（v2.1.0 起可选）----
    # 一个版本可以有 Full（必须）+ Delta（可选）两个包。Delta 只服务
    # 「从上一版本升级」这一条路径；其余场景（老版本、delta 应用失败）
    # 由 Full 兜底 —— 这就是清单必须同时登记两种类型的原因。
    # Delta 的 URL 语义与 package_share_url 相同：OSS 永久直链或飞盘分享。
    delta_filename = Column(String(255), nullable=True)
    delta_share_url = Column(String(512), nullable=True)
    delta_sha1 = Column(String(40), nullable=True)
    delta_sha256 = Column(String(64), nullable=True)
    delta_size = Column(Integer, nullable=True)


class Authorization(Base):
    """Device's current authorization"""
    __tablename__ = "authorizations"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(String(64), nullable=False, unique=True, index=True)
    expires_at = Column(DateTime, nullable=False)
    state = Column(Enum(AuthorizationState), default=AuthorizationState.ACTIVE, nullable=False)

    # Timestamps
    created_at = Column(DateTime, default=func.now(), nullable=False)
    activated_at = Column(DateTime, nullable=True)

    # Reverse relationship
    licenses = relationship("License", back_populates="authorization")

    def calculate_expires_at(self, server_time: datetime) -> datetime:
        """Calculate expiration time based on duration"""
        # Calculate the difference in days between expires_at and server_time
        days = (self.expires_at.timestamp() - server_time.timestamp()) / (24 * 3600) if isinstance(self.expires_at, datetime) else 0
        return server_time + timedelta(days=days)


class LicenseState(str, enum.Enum):
    """License state enumeration"""
    UNUSED = "UNUSED"
    REDEEMED = "REDEEMED"
    REVOKED = "REVOKED"


class License(Base):
    """License key - can be redeemed exactly once"""
    __tablename__ = "licenses"

    id = Column(Integer, primary_key=True, index=True)
    authorization_id = Column(Integer, ForeignKey("authorizations.id"), nullable=True, index=True)
    key_hash = Column(String(64), unique=True, index=True, nullable=False)
    duration_days = Column(Integer, nullable=False)
    state = Column(Enum(LicenseState), default=LicenseState.UNUSED, nullable=False)

    # Features - stored as JSON string, unioned across all authorizations for a device
    features = Column(Text, nullable=True)

    # Timestamps
    created_at = Column(DateTime, default=func.now(), nullable=False)
    redeemed_at = Column(DateTime, nullable=True)

    # NO license_key column - only exists as local variable during create_license
    # No device_id - now in Authorization
    # NO database attribute for license_key - stored only in memory

    # Reverse relationship
    authorization = relationship("Authorization", back_populates="licenses")


class DeviceDailyActive(Base):
    """One device's activity on one business-zone calendar day (Phase 7.4).

    This is the DAU fact table. Exactly one row per ``(device_id, active_date)``
    -- the composite unique constraint is what makes the daily de-duplication
    happen at write time instead of at query time, so DAU stays a plain
    ``COUNT(*)`` over a table whose size is (devices x days), not (requests).

    Deliberately not linked to ``Authorization`` by a foreign key: a heartbeat
    records that the app STARTED, which is independent of whether the device
    holds a valid authorization. An expired or revoked device still launched,
    and must still be counted.
    """

    __tablename__ = "device_daily_active"
    __table_args__ = (
        UniqueConstraint("device_id", "active_date", name="uq_device_daily_active"),
    )

    id = Column(Integer, primary_key=True, index=True)
    #: The client's machine code -- the same identifier the activation flow
    #: stores on Authorization.device_id, so the two are joinable.
    device_id = Column(String(64), nullable=False, index=True)
    #: Business-zone calendar day, derived server-side from the server clock.
    #: Never supplied by the client -- see services/heartbeat_service.bucket_date.
    active_date = Column(Date, nullable=False, index=True)
    #: Timestamps follow the project-wide naive-UTC convention. first_seen_at is
    #: written once on insert and never updated; last_seen_at is refreshed on
    #: every subsequent heartbeat for the same day.
    first_seen_at = Column(DateTime, nullable=False)
    last_seen_at = Column(DateTime, nullable=False)
    #: How many times the app reported in on this day (startup + periodic pings).
    launch_count = Column(Integer, nullable=False, default=1)

class RedemptionEvent(Base):
    """Immutable audit record of one successful redemption (Phase 3).

    Written in the SAME database transaction as the License state mutation and
    the Authorization update (see services/redemption_service.py), so the event
    log can never show a redemption that did not happen, or miss one that did.

    ``license_id`` is UNIQUE: a License can be redeemed exactly once, therefore
    it can produce at most one event. Writers use SQLite
    ``ON CONFLICT DO NOTHING`` so a retried or concurrent write can never
    duplicate an event or abort the redemption itself.

    ``result`` semantics (live writes):
        - ``new``    : Authorization created by this redemption.
        - ``extend`` : ACTIVE authorization, expiry accumulated.
        - ``reset``  : EXPIRED authorization, restarted from redemption time.
    Backfilled rows (recorded_via='backfill') use ``new``/``unknown``: the
    pre-redemption ``expires_at`` of historical renewals was never recorded and
    is deliberately NOT guessed.
    """

    __tablename__ = "redemption_events"

    id = Column(Integer, primary_key=True, index=True)
    #: Exactly one event per License (UNIQUE = the de-duplication guarantee).
    license_id = Column(Integer, nullable=False, unique=True, index=True)
    authorization_id = Column(Integer, nullable=True, index=True)
    #: Denormalized from Authorization for query convenience; the machine code.
    device_id = Column(String(64), nullable=True, index=True)
    #: 'first' | 'renewal' -- first = this redemption created the Authorization.
    event_type = Column(String(16), nullable=False)
    duration_days = Column(Integer, nullable=False)
    #: naive UTC, mirrors licenses.redeemed_at.
    redeemed_at = Column(DateTime, nullable=False)
    auth_created_at = Column(DateTime, nullable=True)
    #: Expiry BEFORE the mutation (NULL for new authorizations and backfilled
    #: renewals -- history cannot be reconstructed).
    auth_expires_before = Column(DateTime, nullable=True)
    #: Expiry AFTER the mutation (NULL on backfill).
    auth_expires_after = Column(DateTime, nullable=True)
    #: 'new' | 'extend' | 'reset' | 'unknown'
    result = Column(String(16), nullable=False)
    #: 'live' | 'backfill'
    recorded_via = Column(String(16), nullable=False, default="live")
    recorded_at = Column(DateTime, default=func.now(), nullable=False)
