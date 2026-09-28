# -*- coding: utf-8 -*-
"""
License Server Tests

Phase 3（2026-09-29）更新：本文件自 7.1 基线（66de0a8, 2026-09-10）后未跟进
7.2-5/6 的接口演进（DI 容器移除、max_devices 字段移除、license_key min_length=16
加固、激活响应改为 SignedLicense 结构）。本次按**当前正式授权 API 契约**更新
断言；覆盖意图不变，未降低任何安全校验（见 PHASE3 报告「任务一」）。
"""

import pytest
from fastapi.testclient import TestClient

import config
from main import app  # noqa: 与生产 --app-dir app 的顶层模块布局一致
from database import get_db, SessionLocal
from models import Authorization, DeviceDailyActive, License

ADMIN_KEY = "test-admin-key"

# Override get_db to use test database
def override_get_db():
    try:
        db = SessionLocal()
        yield db
    finally:
        db.close()

app.dependency_overrides[get_db] = override_get_db

client = TestClient(app)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """每个测试：干净业务表 + 已配置的管理员 API Key。"""
    monkeypatch.setattr(config.settings, "ADMIN_API_KEY", ADMIN_KEY)
    db = SessionLocal()
    try:
        for t in (DeviceDailyActive, License, Authorization):
            db.query(t).delete()
        db.commit()
    finally:
        db.close()
    yield
    db = SessionLocal()
    try:
        for t in (DeviceDailyActive, License, Authorization):
            db.query(t).delete()
        db.commit()
        db.close()
    except Exception:
        db.close()


def _create_license(duration_days: int = 30) -> str:
    """走正式管理 API 创建卡密，返回明文。"""
    response = client.post(
        "/api/v1/admin/licenses",
        json={"duration_days": duration_days},
        headers={"X-API-Key": ADMIN_KEY},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["state"] == "UNUSED"
    assert "license_key" in data
    return data["license_key"]


class TestLicenseServer:
    """Test suite for license server operations"""

    def test_root_endpoint(self):
        """Test root endpoint"""
        response = client.get("/")
        assert response.status_code == 200
        assert response.json()["message"] == "License Server API"

    def test_create_license(self):
        """Test license creation (admin API + X-API-Key)"""
        response = client.post(
            "/api/v1/admin/licenses",
            json={"duration_days": 30},
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["duration_days"] == 30
        assert data["state"] == "UNUSED"
        assert "license_key" in data

    def test_admin_endpoint_requires_key(self):
        """无/错 API Key 一律 401（fail closed）"""
        assert client.post("/api/v1/admin/licenses",
                           json={"duration_days": 30}).status_code == 401
        assert client.post("/api/v1/admin/licenses", json={"duration_days": 30},
                           headers={"X-API-Key": "wrong"}).status_code == 401

    def test_activate_unused_key(self):
        """Test activating an unused license key（当前契约：REDEEMED + SignedLicense）"""
        license_key = _create_license(30)
        response = client.post(
            "/api/v1/licenses/activate",
            json={"license_key": license_key, "device_id": "test-device-1"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["state"] == "REDEEMED"
        assert data["remaining_seconds"] > 0
        assert data.get("signed_license"), "激活必须返回 SignedLicense"
        # 授权状态经由 validate 体现
        val = client.post("/api/v1/licenses/validate",
                          json={"license_key": license_key, "device_id": "test-device-1"})
        assert val.json()["valid"] is True

    def test_validate_activated_key(self):
        """Test validating an activated license key"""
        license_key = _create_license(30)
        client.post("/api/v1/licenses/activate",
                    json={"license_key": license_key, "device_id": "test-device-1"})
        response = client.post(
            "/api/v1/licenses/validate",
            json={"license_key": license_key, "device_id": "test-device-1"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is True
        assert data["state"] == "REDEEMED"  # License.state.value（当前口径）
        assert data["remaining_seconds"] > 0

    def test_same_device_validation(self):
        """Test that the same device can validate repeatedly"""
        license_key = _create_license(30)
        client.post("/api/v1/licenses/activate",
                    json={"license_key": license_key, "device_id": "test-device-1"})
        for _ in range(3):
            response = client.post(
                "/api/v1/licenses/validate",
                json={"license_key": license_key, "device_id": "test-device-1"},
            )
            assert response.status_code == 200
            assert response.json()["valid"] is True

    def test_different_device_rejection(self):
        """另一设备无授权 → valid=False, state=no_authorization（设备绑定语义）"""
        license_key = _create_license(30)
        client.post("/api/v1/licenses/activate",
                    json={"license_key": license_key, "device_id": "test-device-1"})
        response = client.post(
            "/api/v1/licenses/validate",
            json={"license_key": license_key, "device_id": "test-device-2"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is False
        assert data["state"] == "no_authorization"

    def test_duplicate_redemption_rejection(self):
        """Test that duplicate redemption is rejected"""
        license_key = _create_license(30)
        client.post("/api/v1/licenses/activate",
                    json={"license_key": license_key, "device_id": "test-device-1"})
        response = client.post(
            "/api/v1/licenses/activate",
            json={"license_key": license_key, "device_id": "test-device-1"},
        )
        assert response.status_code == 400
        assert response.json()["detail"] == "already_redeemed"

    def test_invalid_key(self):
        """无效 Key（满足 min_length=16 的输入才进入业务校验）→ invalid_key"""
        response = client.post(
            "/api/v1/licenses/validate",
            json={"license_key": "invalid-key-0123456789", "device_id": "test-device-1"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is False
        assert data["state"] == "invalid_key"

    def test_short_key_rejected_by_validation(self):
        """短于 16 字符的 Key 在 schema 层拒绝（422）——安全加固生效"""
        response = client.post(
            "/api/v1/licenses/validate",
            json={"license_key": "invalid-key", "device_id": "test-device-1"},
        )
        assert response.status_code == 422

    def test_duration_handling(self):
        """Test different duration options（均在 LicenseCreate gt=0 le=365 白名单内）"""
        for duration in [7, 30, 90, 365]:
            license_key = _create_license(duration)
            response = client.post(
                "/api/v1/licenses/activate",
                json={"license_key": license_key, "device_id": "test-device-1"},
            )
            assert response.status_code == 200
            data = response.json()
            assert data["duration_days"] == duration
            assert data["remaining_seconds"] > duration * 86400 - 86400  # 余量≈时长
