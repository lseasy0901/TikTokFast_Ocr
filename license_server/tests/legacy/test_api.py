#!/usr/bin/env python3
"""
Test the license server API endpoints
"""

import requests
import json
import time

BASE_URL = "http://localhost:8000"

def test_server_status():
    """Test if the server is running"""
    try:
        response = requests.get(f"{BASE_URL}/")
        if response.status_code == 200:
            print("[OK] Server is running")
            return True
        else:
            print(f"[ERROR] Server returned status {response.status_code}")
            return False
    except requests.exceptions.ConnectionError:
        print("[ERROR] Server is not running. Please start it first:")
        print("  cd license_server")
        print("  python run_server.py")
        return False

def test_invalid_license():
    """Test validation with invalid license key"""
    print("\nTesting invalid license key...")
    response = requests.post(
        f"{BASE_URL}/api/v1/licenses/validate",
        json={
            "license_key": "invalid-key-123",
            "device_id": "test-device-1"
        }
    )
    print(f"Status: {response.status_code}")
    data = response.json()
    print(f"Response: {json.dumps(data, indent=2)}")
    assert data["valid"] is False
    assert data["state"] == "invalid_key"
    print("[OK] Invalid license test passed")

def test_create_license():
    """Test creating a license (admin only)"""
    print("\nTesting license creation...")
    headers = {"X-API-Key": "test-admin-api-key"}
    response = requests.post(
        f"{BASE_URL}/api/v1/admin/licenses",
        json={"duration_days": 30, "max_devices": 1},
        headers=headers
    )
    print(f"Status: {response.status_code}")
    if response.status_code == 200:
        data = response.json()
        print(f"Response: {json.dumps(data, indent=2)}")
        assert data["duration_days"] == 30
        assert data["state"] == "UNUSED"
        assert "license_key" in data
        print("[OK] License creation test passed")
        return data["license_key"]
    else:
        print(f"[ERROR] Failed to create license: {response.text}")
        return None

def test_activate_license(license_key):
    """Test activating a license"""
    print("\nTesting license activation...")
    response = requests.post(
        f"{BASE_URL}/api/v1/licenses/activate",
        json={
            "license_key": license_key,
            "device_id": "test-device-1"
        }
    )
    print(f"Status: {response.status_code}")
    data = response.json()
    print(f"Response: {json.dumps(data, indent=2)}")
    assert data["state"] == "ACTIVE"
    assert data["device_id"] == "test-device-1"
    assert data["expires_at"] is not None
    print("[OK] License activation test passed")

def test_validate_license(license_key):
    """Test validating an activated license"""
    print("\nTesting license validation...")
    response = requests.post(
        f"{BASE_URL}/api/v1/licenses/validate",
        json={
            "license_key": license_key,
            "device_id": "test-device-1"
        }
    )
    print(f"Status: {response.status_code}")
    data = response.json()
    print(f"Response: {json.dumps(data, indent=2)}")
    assert data["valid"] is True
    assert data["state"] == "ACTIVE"
    assert data["remaining_seconds"] > 0
    print("[OK] License validation test passed")

def main():
    """Run all tests"""
    print("=== License Server API Tests ===\n")

    # Check if server is running
    if not test_server_status():
        return

    # Test invalid license
    test_invalid_license()

    # Test license creation, activation, and validation
    license_key = test_create_license()
    if license_key:
        time.sleep(1)  # Small delay to ensure database consistency
        test_activate_license(license_key)
        time.sleep(1)
        test_validate_license(license_key)

    print("\n=== All tests completed! ===")

if __name__ == "__main__":
    main()