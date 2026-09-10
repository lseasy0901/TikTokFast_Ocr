# -*- coding: utf-8 -*-
"""
SQLAlchemy database models
"""

from datetime import datetime, timedelta
from sqlalchemy import Column, Integer, String, DateTime, Boolean, Enum, ForeignKey
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
    max_devices = Column(Integer, default=1, nullable=False)

    # Reverse relationship
    licenses = relationship("License", back_populates="authorization")

    def calculate_expires_at(self, server_time: datetime) -> datetime:
        """Calculate expiration time based on duration"""
        return server_time + timedelta(days=self.expires_at.timestamp() - server_time.timestamp() if isinstance(self.expires_at, (datetime, )) else 0)


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

    # Timestamps
    created_at = Column(DateTime, default=func.now(), nullable=False)
    redeemed_at = Column(DateTime, nullable=True)

    # NO license_key column - only exists as local variable during create_license
    # No device_id - now in Authorization
    # NO database attribute for license_key - stored only in memory

    # Reverse relationship
    authorization = relationship("Authorization", back_populates="licenses")