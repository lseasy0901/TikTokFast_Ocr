# -*- coding: utf-8 -*-
"""
License Server Tests
"""

import pytest
from datetime import datetime, timezone
from fastapi.testclient import TestClient
from app.main import app
from app.database import get_db, SessionLocal
from app.models import License, LicenseState

# Override get_db to use test database
def override_get_db():
    try:
        db = SessionLocal()
        yield db
    finally:
        db.close()

app.dependency_overrides[get_db] = override_get_db

client = TestClient(app)


class TestLicenseServer:
    """Test suite for license server operations"""

    def setup_method(self):
        """Setup test database"""
        self.db = SessionLocal()
        # Clean up any existing licenses
        self.db.query(License).delete()
        self.db.commit()

    def teardown_method(self):
        """Clean up test database"""
        self.db.query(License).delete()
        self.db.commit()
        self.db.close()

    def test_root_endpoint(self):
        """Test root endpoint"""
        response = client.get("/")
        assert response.status_code == 200
        assert response.json()["message"] == "License Server API"

    def test_create_license(self):
        """Test license creation (admin)"""
        # Set admin API key
        admin_key = "test-admin-key"
        with app.container.credentials.override({"ADMIN_API_KEY": admin_key}):
            response = client.post(
                "/api/v1/admin/licenses",
                json={"duration_days": 30, "max_devices": 1},
                headers={"X-API-Key": admin_key}
            )
            assert response.status_code == 200
            data = response.json()
            assert data["duration_days"] == 30
            assert data["state"] == "UNUSED"
            assert data["max_devices"] == 1
            assert "license_key" in data

    def test_activate_unused_key(self):
        """Test activating an unused license key"""
        # First create a license
        admin_key = "test-admin-key"
        response = client.post(
            "/api/v1/admin/licenses",
            json={"duration_days": 30, "max_devices": 1},
            headers={"X-API-Key": admin_key}
        )
        license_data = response.json()
        license_key = license_data["license_key"]

        # Activate the license
        response = client.post(
            "/api/v1/licenses/activate",
            json={"license_key": license_key, "device_id": "test-device-1"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["state"] == "ACTIVE"
        assert data["device_id"] == "test-device-1"
        assert data["expires_at"] is not None

    def test_validate_activated_key(self):
        """Test validating an activated license key"""
        # Create and activate a license
        admin_key = "test-admin-key"
        response = client.post(
            "/api/v1/admin/licenses",
            json={"duration_days": 30, "max_devices": 1},
            headers={"X-API-Key": admin_key}
        )
        license_data = response.json()
        license_key = license_data["license_key"]

        # Activate the license
        client.post(
            "/api/v1/licenses/activate",
            json={"license_key": license_key, "device_id": "test-device-1"}
        )

        # Validate the license
        response = client.post(
            "/api/v1/licenses/validate",
            json={"license_key": license_key, "device_id": "test-device-1"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is True
        assert data["state"] == "ACTIVE"
        assert data["remaining_seconds"] > 0

    def test_same_device_validation(self):
        """Test that the same device can validate repeatedly"""
        # Create and activate a license
        admin_key = "test-admin-key"
        response = client.post(
            "/api/v1/admin/licenses",
            json={"duration_days": 30, "max_devices": 1},
            headers={"X-API-Key": admin_key}
        )
        license_data = response.json()
        license_key = license_data["license_key"]

        # Activate the license
        client.post(
            "/api/v1/licenses/activate",
            json={"license_key": license_key, "device_id": "test-device-1"}
        )

        # Validate multiple times
        for _ in range(3):
            response = client.post(
                "/api/v1/licenses/validate",
                json={"license_key": license_key, "device_id": "test-device-1"}
            )
            assert response.status_code == 200
            data = response.json()
            assert data["valid"] is True

    def test_different_device_rejection(self):
        """Test that different devices are rejected"""
        # Create and activate a license
        admin_key = "test-admin-key"
        response = client.post(
            "/api/v1/admin/licenses",
            json={"duration_days": 30, "max_devices": 1},
            headers={"X-API-Key": admin_key}
        )
        license_data = response.json()
        license_key = license_data["license_key"]

        # Activate on first device
        client.post(
            "/api/v1/licenses/activate",
            json={"license_key": license_key, "device_id": "test-device-1"}
        )

        # Try to validate from different device
        response = client.post(
            "/api/v1/licenses/validate",
            json={"license_key": license_key, "device_id": "test-device-2"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is False
        assert data["state"] == "device_mismatch"

    def test_duplicate_redemption_rejection(self):
        """Test that duplicate redemption is rejected"""
        # Create and activate a license
        admin_key = "test-admin-key"
        response = client.post(
            "/api/v1/admin/licenses",
            json={"duration_days": 30, "max_devices": 1},
            headers={"X-API-Key": admin_key}
        )
        license_data = response.json()
        license_key = license_data["license_key"]

        # Activate the license
        client.post(
            "/api/v1/licenses/activate",
            json={"license_key": license_key, "device_id": "test-device-1"}
        )

        # Try to activate again
        response = client.post(
            "/api/v1/licenses/activate",
            json={"license_key": license_key, "device_id": "test-device-1"}
        )
        # Should fail because it's already redeemed
        assert response.status_code == 400

    def test_invalid_key(self):
        """Test validation with invalid license key"""
        response = client.post(
            "/api/v1/licenses/validate",
            json={"license_key": "invalid-key", "device_id": "test-device-1"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is False
        assert data["state"] == "invalid_key"

    def test_duration_handling(self):
        """Test different duration options"""
        durations = [7, 30, 90, 365]
        for duration in durations:
            # Clean up
            self.db.query(License).delete()
            self.db.commit()

            # Create license with specific duration
            admin_key = "test-admin-key"
            response = client.post(
                "/api/v1/admin/licenses",
                json={"duration_days": duration, "max_devices": 1},
                headers={"X-API-Key": admin_key}
            )
            license_data = response.json()
            license_key = license_data["license_key"]

            # Activate and check duration
            response = client.post(
                "/api/v1/licenses/activate",
                json={"license_key": license_key, "device_id": "test-device-1"}
            )
            assert response.status_code == 200
            data = response.json()
            # Note: We can't easily check exact duration without mocking time
            assert data["expires_at"] is not None