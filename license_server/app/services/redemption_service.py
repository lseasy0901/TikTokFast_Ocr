# -*- coding: utf-8 -*-
"""
Redemption service for license redemption operations - Phase 7.2-5
Handles atomic license redemption with feature union semantics
"""

import secrets
import hashlib
import json
from datetime import datetime, timezone, timedelta
from typing import Optional, Set, List

from sqlalchemy.orm import Session
from models import License, LicenseState, Authorization, AuthorizationState
from schemas import LicenseActivate, LicenseValidationResponse, LicenseValidate
from services.susi_security_service import SusiSecurityService


def generate_secure_key() -> str:
    """Generate a cryptographically secure random license key"""
    return secrets.token_urlsafe(32)


def generate_key_hash(license_key: str) -> str:
    """Generate SHA-256 hash of license key"""
    return hashlib.sha256(license_key.encode()).hexdigest()


def parse_features(features_json: Optional[str]) -> Set[str]:
    """Parse features from JSON string"""
    if not features_json:
        return set()
    try:
        return set(json.loads(features_json))
    except:
        return set()


class RedemptionService:
    """Service for license redemption operations with atomic transactions"""

    def __init__(self, db: Session, susi_security_service: SusiSecurityService):
        self.db = db
        self.susi_security = susi_security_service

    def find_or_create_authorization(
        self, device_id: str, duration_days: int
    ) -> tuple[Authorization, bool]:
        """Find existing authorization for device or create new one

        Rules:
        - No Authorization: expires_at = server_time + duration_days
        - ACTIVE Authorization: extends expiry (accumulation)
        - EXPIRED Authorization: renews from server_time
        - REVOKED Authorization: cannot activate

        Returns:
            ``(authorization, created)``. ``created`` is True when this call
            inserted the row. A freshly created authorization already carries
            its full duration, so the caller must not extend it again.
        """
        server_time = datetime.now(timezone.utc)
        authorization = self.db.query(Authorization).filter(
            Authorization.device_id == device_id
        ).first()

        if not authorization:
            # Create new authorization with time-based expiry
            authorization = Authorization(
                device_id=device_id,
                expires_at=server_time + timedelta(days=duration_days)
            )
            self.db.add(authorization)
            self.db.commit()
            self.db.refresh(authorization)
            return authorization, True

        if authorization.state == AuthorizationState.REVOKED:
            raise ValueError("revoked")

        return authorization, False

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

    def redeem_license(self, activation_data: LicenseActivate) -> tuple[License, dict]:
        """Redeem a license key with atomic transaction

        CRITICAL RULES (LOCKED):
        1. LicenseKey is globally one-time:
           - UNUSED -> REDEEMED on successful redemption
           - REDEEMED keys cannot be redeemed again
           - Repeated redemption returns 'already_redeemed'

        2. A repeated redemption MUST NOT mutate Authorization:
           - Check license state FIRST
           - If already REDEEMED, return error immediately
           - Do not proceed to Authorization update

        3. Authorization states:
           - No Authorization: expires_at = server_time + duration
           - ACTIVE: expiry += duration (accumulation)
           - EXPIRED: expires_at = server_time + duration (restart)
           - REVOKED: Cannot activate

        4. Atomic transaction:
           - License state check
           - Authorization mutation
           - License state update to REDEEMED
           - ALL in ONE database transaction

        5. Features are unioned across all authorized licenses

        Returns:
            tuple[License, dict]: Updated license and activation response
        """
        server_time = datetime.now(timezone.utc)
        key_hash = generate_key_hash(activation_data.license_key)

        # STEP 1: Find License by key_hash (VALIDATE FIRST)
        license = self.find_license_by_key_hash(key_hash)

        if not license:
            raise ValueError("invalid_key")

        # STEP 2: Check License state (REDEEMED check BEFORE authorization mutation)
        if license.state == LicenseState.REVOKED:
            raise ValueError("revoked")
        if license.state == LicenseState.REDEEMED:
            # CRITICAL: Return error WITHOUT mutating Authorization
            raise ValueError("already_redeemed")

        # STEP 3: Find or create Authorization for device
        authorization, created = self.find_or_create_authorization(
            activation_data.device_id,
            license.duration_days
        )

        # STEP 4: Update Authorization based on its current state
        if created:
            # Brand new authorization: it already expires at exactly
            # server_time + duration. Authorization.state defaults to ACTIVE,
            # so without this guard the accumulation branch below would add the
            # duration a second time and grant new devices 2x their key.
            pass
        elif authorization.state == AuthorizationState.ACTIVE:
            # Extend from current expiry (accumulation)
            authorization.expires_at = authorization.expires_at + timedelta(days=license.duration_days)
        elif authorization.state == AuthorizationState.EXPIRED:
            # Reactivate from server_time (restart)
            authorization.state = AuthorizationState.ACTIVE
            authorization.activated_at = server_time
            # Calculate new expiration from server_time + duration
            authorization.expires_at = server_time + timedelta(days=license.duration_days)
        # REVOKED handled in find_or_create_authorization()

        # STEP 5: Mark License as REDEEMED
        license.state = LicenseState.REDEEMED
        license.redeemed_at = server_time
        license.authorization_id = authorization.id

        # STEP 6: Commit atomically
        self.db.commit()
        self.db.refresh(license)

        # STEP 7: Create SignedLicense for client using Susi security service
        # This is where Susi security layer integrates
        # CRITICAL: SignedLicense creation is REQUIRED for successful activation
        # If this fails, the entire redemption must fail to preserve business consistency
        signed_license_response = None
        try:
            signed_license_data = self.susi_security.create_signed_license(
                {
                    "id": license.id,
                    # The plaintext key is deliberately never persisted (see
                    # models.License: there is no license_key column), so it is
                    # unavailable whenever creation and redemption happen in
                    # different requests -- i.e. always, over HTTP.
                    # The key hash is the canonical non-secret identifier for
                    # this license and is always present.
                    "license_key": license.key_hash,
                    "created_at": license.created_at,
                    "features": parse_features(license.features),
                    "authorization": {
                        "expires_at": license.authorization.expires_at,
                        "activated_at": license.authorization.activated_at
                    }
                },
                # The Authorization's own device_id, not activation_data.device_id:
                # find_or_create_authorization() selected or created this row by
                # that value, so the two are equal by construction -- and taking it
                # off the row guarantees the signed machine_codes binding can never
                # drift from the Authorization it was issued for.
                authorization.device_id,
                license.duration_days
            )
            # If susi_security returned signed_license, include it in response
            if signed_license_data.get('signed_license'):
                signed_license_response = signed_license_data['signed_license']
            else:
                raise RuntimeError("SignedLicense creation returned no signed_license data")
        except Exception as e:
            # CRITICAL: If SignedLicense creation fails, the entire redemption must fail
            # This preserves the business rule that activation requires successful security integration
            raise RuntimeError(f"Failed to create SignedLicense: {e}")

        return license, self._format_activation_response(license, server_time, signed_license=signed_license_response)

    def validate_license(self, validation_data: LicenseValidate) -> LicenseValidationResponse:
        """Validate a license key and check authorization status

        Rules:
        - Check license state (not revoked)
        - Check authorization exists
        - Check authorization state (not revoked/expired)
        - Check actual expiration time
        """
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

        # Check actual expiration
        if authorization.expires_at < server_time:
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

    def _format_activation_response(self, license: License, server_time: datetime, signed_license: Optional[str] = None) -> dict:
        """Format activation response

        Args:
            license: The redeemed License object
            server_time: Current server time for calculating remaining seconds
            signed_license: Optional SignedLicense string from Susi security layer

        Returns:
            Formatted response dict
        """
        remaining = None
        if license.authorization and license.authorization.expires_at:
            expires_at = license.authorization.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            remaining = int((expires_at - server_time).total_seconds())

        response = {
            "id": license.id,
            "license_key": license.license_key if hasattr(license, 'license_key') else None,
            "duration_days": license.duration_days,
            "state": license.state.value,
            "features": parse_features(license.features),
            "created_at": license.created_at,
            "redeemed_at": license.redeemed_at,
            "authorization_id": license.authorization_id,
            "remaining_seconds": remaining
        }

        # Include signed_license if provided (Phase 7.2-6 integration)
        if signed_license:
            response["signed_license"] = signed_license

        return response

    def aggregate_features(self, device_id: str) -> tuple[Set[str], datetime]:
        """Aggregate features from all authorized licenses

        Rule: Features are unioned from all licenses that have redeemed for this device
        """
        # Get all active authorizations for this device
        authorizations = self.db.query(Authorization).filter(
            Authorization.device_id == device_id,
            Authorization.state == AuthorizationState.ACTIVE
        ).all()

        features = set()
        max_expires_at = None

        for auth in authorizations:
            # Get all licenses for this authorization
            licenses = self.db.query(License).filter(
                License.authorization_id == auth.id,
                License.state == LicenseState.REDEEMED
            ).all()

            for license in licenses:
                # Union features from all licenses
                features.update(parse_features(license.features))

            # Track the maximum expiration time
            if auth.expires_at and (max_expires_at is None or auth.expires_at > max_expires_at):
                max_expires_at = auth.expires_at

        return features, max_expires_at
