# -*- coding: utf-8 -*-
"""
Pydantic schemas for API validation and response
"""

from datetime import datetime
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


class ErrorResponse(BaseModel):
    """Schema for error responses"""
    detail: str