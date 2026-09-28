# -*- coding: utf-8 -*-
"""
Pydantic schemas for API validation and response
"""

from datetime import date, datetime
from typing import Optional
from pydantic import BaseModel, Field


class LicenseCreate(BaseModel):
    """Schema for creating a new license"""
    duration_days: int = Field(..., gt=0, le=365)


class LicenseResponse(BaseModel):
    """Schema for license creation response"""
    id: int
    license_key: str  # Actual plaintext key (in-memory only, not persisted)
    duration_days: int
    state: str
    created_at: datetime
    redeemed_at: Optional[datetime]
    # Which authorization this license extends/redeems.
    # None while the license is still UNUSED: a license acquires an
    # authorization only when it is redeemed (RedemptionService.redeem_license).
    authorization_id: Optional[int] = None

    class Config:
        from_attributes = True


class LicenseActivate(BaseModel):
    """Schema for license activation"""
    license_key: str = Field(..., min_length=16, max_length=64)
    device_id: str = Field(..., min_length=1, max_length=64)


class LicenseValidate(BaseModel):
    """Schema for license validation"""
    license_key: str = Field(..., min_length=16, max_length=64)
    device_id: str = Field(..., min_length=1, max_length=64)


class LicenseValidationResponse(BaseModel):
    """Schema for license validation response"""
    valid: bool
    state: str
    expires_at: Optional[datetime]
    server_time: datetime
    remaining_seconds: Optional[int]

    class Config:
        from_attributes = True


class LicenseHeartbeat(BaseModel):
    """Schema for a client launch heartbeat (Phase 7.4).

    Carries the device identifier and nothing else. Deliberately no timestamp:
    the client's clock is not trusted for anything, let alone for deciding which
    day a device was active on -- the server stamps that itself.

    ``max_length=64`` matches ``Authorization.device_id`` and the existing
    LicenseActivate/LicenseValidate constraints. Real machine codes from
    susi_helper are exactly 64 lowercase hex characters; SQLite does not enforce
    VARCHAR length, so this bound is the only thing keeping the column honest.
    """
    device_id: str = Field(..., min_length=1, max_length=64)


class HeartbeatResponse(BaseModel):
    """Schema for a heartbeat response."""
    ok: bool
    active_date: date


class ErrorResponse(BaseModel):
    """Schema for error responses"""
    detail: str


# ----------------------------------------------------------------------
# Phase 2 — 自动更新发布登记
# ----------------------------------------------------------------------
class UpdateReleaseCreate(BaseModel):
    """发布一个 stable release。

    ``package_sha1`` / ``package_sha256`` 是**必填**的：Velopack 的
    ``releases.<channel>.json`` 本身就要求携带这两个哈希，客户端下载完 .nupkg
    会用它校验完整性。它们的唯一权威来源是打包工具 ``vpk pack`` 生成的 feed
    （见 scripts/velopack_release.py），服务端不下载 .nupkg，因此无法自算。

    这里**没有**、也不允许有 installer_url / direct_url / CDN URL / 临时 token。
    """

    version: str = Field(..., min_length=5, max_length=32)
    channel: str = Field(default="stable", max_length=16)
    package_filename: str = Field(..., min_length=1, max_length=255)
    #: 永久的小飞机分享链接。临时直链由服务端在下载时实时换取，绝不保存。
    package_share_url: str = Field(..., min_length=1, max_length=512)
    release_notes: Optional[str] = None
    package_sha1: str = Field(..., min_length=40, max_length=40)
    package_sha256: str = Field(..., min_length=64, max_length=64)
    #: 可选：申报大小。给了就会与分享中的实际大小做交叉校验，不一致即拒绝发布。
    package_size: Optional[int] = Field(default=None, gt=0)


class UpdateReleaseResponse(BaseModel):
    """发布结果。

    刻意**不含** direct_url / CDN URL / 临时 token —— 那些根本不存在于库里。
    """

    id: int
    version: str
    channel: str
    package_filename: str
    package_share_url: str
    package_size: int
    release_notes: Optional[str]
    published_at: datetime

    class Config:
        from_attributes = True