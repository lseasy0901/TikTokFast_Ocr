# -*- coding: utf-8 -*-
"""
License service implementation
"""

import secrets
import hashlib
import hmac
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy.orm import Session
from models import License, LicenseState, Authorization, AuthorizationState
from schemas import LicenseCreate, LicenseActivate, LicenseValidationResponse


def generate_secure_key() -> str:
    """Generate a cryptographically secure random license key"""
    return secrets.token_urlsafe(32)


def generate_key_hash(license_key: str) -> str:
    """Generate SHA-256 hash of license key"""
    return hashlib.sha256(license_key.encode()).hexdigest()


def constant_time_compare(val1: str, val2: str) -> bool:
    """Compare strings in constant time to prevent timing attacks"""
    return hmac.compare_digest(val1, val2)


class LicenseService:
    """Service for license operations"""

    def __init__(self, db: Session):
        self.db = db

    def find_or_create_authorization(self, device_id: str, duration_days: int) -> Authorization:
        """Find existing authorization for device or create new one"""
        authorization = self.db.query(Authorization).filter(
            Authorization.device_id == device_id
        ).first()

        if not authorization:
            # Create new authorization
            authorization = Authorization(
                device_id=device_id,
                expires_at=datetime.now(timezone.utc) + timedelta(days=duration_days)
            )
            self.db.add(authorization)
            self.db.commit()
            self.db.refresh(authorization)

        return authorization

    def find_license_by_key_hash(self, key_hash: str) -> Optional[License]:
        """Get license by key hash"""
        return self.db.query(License).filter(
            License.key_hash == key_hash
        ).first()

    def find_authorization(self, device_id: str) -> Optional[Authorization]:
        """Find authorization for device"""
        return self.db.query(Authorization).filter(
            Authorization.device_id == device_id
        ).first()

    def create_license(self, license_data: LicenseCreate) -> License:
        """Create a new license key"""
        # Generate secure license key
        license_key = generate_secure_key()
        key_hash = generate_key_hash(license_key)

        # Create license record (NOT linked to authorization yet)
        license = License(
            key_hash=key_hash,
            duration_days=license_data.duration_days,
            state=LicenseState.UNUSED,
            authorization_id=None  # Set when license is activated
        )

        self.db.add(license)
        self.db.commit()
        self.db.refresh(license)

        # Store plaintext key only in memory - NOT persisted in database
        # Access the license object and add license_key as a non-column attribute
        license.license_key = license_key
        return license

    def activate_license(self, activation_data: LicenseActivate) -> tuple[License, dict]:
        """Activate a license key"""
        server_time = datetime.now(timezone.utc)
        key_hash = generate_key_hash(activation_data.license_key)

        # STEP 1: Find License by key_hash (VALIDATE FIRST)
        license = self.find_license_by_key_hash(key_hash)

        if not license:
            raise ValueError("invalid_key")

        # STEP 2: Check License state
        if license.state == LicenseState.REVOKED:
            raise ValueError("revoked")
        if license.state == LicenseState.REDEEMED:
            raise ValueError("already_redeemed")

        # STEP 3: Find or create Authorization for device
        authorization = self.find_or_create_authorization(
            activation_data.device_id,
            license.duration_days
        )

        # STEP 4: Update Authorization based on its current state
        if authorization.state == AuthorizationState.ACTIVE:
            # Extend from current expiry
            authorization.expires_at = authorization.expires_at + timedelta(days=license.duration_days)
        elif authorization.state == AuthorizationState.EXPIRED:
            # Reactivate from server_time
            authorization.state = AuthorizationState.ACTIVE
            authorization.activated_at = server_time
            authorization.expires_at = authorization.calculate_expires_at(server_time)
        elif authorization.state == AuthorizationState.REVOKED:
            raise ValueError("revoked")

        # STEP 5: Mark License as REDEEMED
        license.state = LicenseState.REDEEMED
        license.redeemed_at = server_time
        license.authorization_id = authorization.id

        # STEP 6: Commit atomically
        self.db.commit()
        self.db.refresh(license)

        return license, self._format_activation_response(license, server_time)

    def validate_license(self, validation_data: LicenseValidate) -> LicenseValidationResponse:
        """Validate a license key"""
        server_time = datetime.now(timezone.utc)
        key_hash = generate_key_hash(validation_data.license_key)

        # Find license by key hash
        license = self.find_license_by_key_hash(key_hash)

        if not license:
            return LicenseValidationResponse(
                valid=False,
                state="invalid_key",
                expires_at=None,
                server_time=server_time,
                remaining_seconds=None
            )

        # Check license state
        if license.state == LicenseState.REVOKED:
            return LicenseValidationResponse(
                valid=False,
                state="revoked",
                expires_at=None,
                server_time=server_time,
                remaining_seconds=None
            )

        # Check if authorization exists for the device
        authorization = self.find_authorization(validation_data.device_id)
        if not authorization:
            return LicenseValidationResponse(
                valid=False,
                state="no_authorization",
                expires_at=None,
                server_time=server_time,
                remaining_seconds=None
            )

        # Convert all naive datetime fields from database to UTC-aware
        if license.redeemed_at and license.redeemed_at.tzinfo is None:
            license.redeemed_at = license.redeemed_at.replace(tzinfo=timezone.utc)

        if authorization.expires_at and authorization.expires_at.tzinfo is None:
            authorization.expires_at = authorization.expires_at.replace(tzinfo=timezone.utc)
        if authorization.activated_at and authorization.activated_at.tzinfo is None:
            authorization.activated_at = authorization.activated_at.replace(tzinfo=timezone.utc)

        # Check authorization state
        if authorization.state == AuthorizationState.REVOKED:
            return LicenseValidationResponse(
                valid=False,
                state="revoked",
                expires_at=None,
                server_time=server_time,
                remaining_seconds=None
            )

        if authorization.state == AuthorizationState.EXPIRED:
            return LicenseValidationResponse(
                valid=False,
                state="expired",
                expires_at=authorization.expires_at,
                server_time=server_time,
                remaining_seconds=None
            )

        # Check expiration
        if authorization.expires_at < server_time:
            # License is expired
            return LicenseValidationResponse(
                valid=False,
                state="expired",
                expires_at=authorization.expires_at,
                server_time=server_time,
                remaining_seconds=None
            )

        # License is valid
        remaining = int((authorization.expires_at - server_time).total_seconds())

        return LicenseValidationResponse(
            valid=True,
            state=license.state.value,
            expires_at=authorization.expires_at,
            server_time=server_time,
            remaining_seconds=remaining
        )

    def get_license_by_key(self, license_key: str) -> Optional[License]:
        """Get license by key (for testing)"""
        key_hash = generate_key_hash(license_key)
        return self.db.query(License).filter(License.key_hash == key_hash).first()

    def _format_activation_response(self, license: License, server_time: datetime) -> dict:
        """Format activation response"""
        remaining = None
        if license.authorization and license.authorization.expires_at:
            # SQLAlchemy returns naive datetime from database
            expires_at = license.authorization.expires_at
            # Interpret naive expires_at as UTC (server time is UTC)
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            remaining = int((expires_at - server_time).total_seconds())

        return {
            "id": license.id,
            "license_key": license.license_key if hasattr(license, 'license_key') else None,
            "duration_days": license.duration_days,
            "state": license.state.value,
            "created_at": license.created_at,
            "redeemed_at": license.redeemed_at,
            "authorization_id": license.authorization_id,
            "remaining_seconds": remaining
        }