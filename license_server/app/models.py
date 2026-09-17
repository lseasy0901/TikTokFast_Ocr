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