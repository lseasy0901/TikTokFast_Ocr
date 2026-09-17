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