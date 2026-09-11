# -*- coding: utf-8 -*-
"""
License service for admin operations - Phase 7.2-5
Generates license keys and manages license lifecycle
"""

import secrets
import hashlib
import json
from datetime import datetime, timezone
from typing import Optional, List

from sqlalchemy.orm import Session
from models import License, LicenseState, Authorization
from schemas import LicenseCreate


def generate_secure_key() -> str:
    """Generate a cryptographically secure random license key"""
    return secrets.token_urlsafe(32)


def generate_key_hash(license_key: str) -> str:
    """Generate SHA-256 hash of license key"""
    return hashlib.sha256(license_key.encode()).hexdigest()


class LicenseService:
    """Service for admin license operations"""

    def __init__(self, db: Session):
        self.db = db

    def create_license(
        self,
        license_data: LicenseCreate,
        features: Optional[List[str]] = None,
        commit: bool = True,
    ) -> License:
        """Create a new license key (UNUSED state)

        Rules:
        - Generate cryptographically secure license key
        - Hash for database storage
        - Store plaintext key only in memory
        - Initial state: UNUSED

        `commit=False` flushes instead of committing, so a caller can write a
        whole batch inside ONE transaction and commit or roll it back as a unit.
        The default (`commit=True`) is the original behaviour, unchanged.
        """
        license_key = generate_secure_key()
        key_hash = generate_key_hash(license_key)

        # Features stored as JSON string
        features_json = json.dumps(features or []) if features else None

        license = License(
            key_hash=key_hash,
            duration_days=license_data.duration_days,
            state=LicenseState.UNUSED,
            features=features_json
        )

        self.db.add(license)
        if commit:
            self.db.commit()
            self.db.refresh(license)
        else:
            # Flush so the row is written inside the caller's transaction and
            # the object picks up its primary key, without ending that transaction.
            self.db.flush()

        # Store plaintext key in memory only
        license.license_key = license_key
        return license

    def get_license_by_key(self, license_key: str) -> Optional[License]:
        """Get license by key (for testing/admin operations)"""
        key_hash = generate_key_hash(license_key)
        return self.db.query(License).filter(License.key_hash == key_hash).first()

    def revoke_license(self, license_id: int) -> License:
        """Revoke a license (admin operation)"""
        license = self.db.query(License).filter(License.id == license_id).first()
        if not license:
            raise ValueError("license_not_found")

        license.state = LicenseState.REVOKED
        self.db.commit()
        self.db.refresh(license)
        return license

    def get_all_licenses(self) -> List[License]:
        """Get all licenses (admin operation)"""
        return self.db.query(License).order_by(License.created_at.desc()).all()
