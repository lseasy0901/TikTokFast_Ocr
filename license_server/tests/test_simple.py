# -*- coding: utf-8 -*-
"""
Simple integration tests for the license server
"""

import pytest
from fastapi.testclient import TestClient
from main import app  # noqa: 与生产 --app-dir app 的顶层模块布局一致
from database import SessionLocal, get_db
from models import License, LicenseState
from services.license_service import generate_secure_key, generate_key_hash

# Override get_db to use test database
def override_get_db():
    try:
        db = SessionLocal()
        yield db
    finally:
        db.close()

app.dependency_overrides[get_db] = override_get_db

client = TestClient(app)


def test_generate_secure_key():
    """Test license key generation"""
    key = generate_secure_key()
    assert len(key) == 43  # 32 bytes urlsafe encoded
    assert isinstance(key, str)


def test_key_hash_generation():
    """Test key hash generation"""
    key = "test-license-key"
    hash1 = generate_key_hash(key)
    hash2 = generate_key_hash(key)
    assert hash1 == hash2
    assert len(hash1) == 64  # SHA-256 hex length


def test_root_endpoint():
    """Test root endpoint"""
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["message"] == "License Server API"


def test_invalid_key_validation():
    """Test validation with invalid license key"""
    response = client.post(
        "/api/v1/licenses/validate",
        json={
            "license_key": "invalid-key-123456",  # ≥16 字符（min_length 加固后短 Key 由 422 拒绝）
            "device_id": "test-device-1"
        }
    )
    assert response.status_code == 200
    data = response.json()
    assert data["valid"] is False
    assert data["state"] == "invalid_key"


def test_admin_endpoint_no_key(monkeypatch):
    """Test admin endpoint without API key"""
    import config
    monkeypatch.setattr(config.settings, "ADMIN_API_KEY", "test-key-for-simple")
    response = client.post(
        "/api/v1/admin/licenses",
        json={"duration_days": 30}
    )
    assert response.status_code == 401


def test_admin_endpoint_invalid_key(monkeypatch):
    """Test admin endpoint with invalid API key"""
    import config
    monkeypatch.setattr(config.settings, "ADMIN_API_KEY", "test-key-for-simple")
    response = client.post(
        "/api/v1/admin/licenses",
        json={"duration_days": 30},
        headers={"X-API-Key": "invalid-key"}
    )
    assert response.status_code == 401