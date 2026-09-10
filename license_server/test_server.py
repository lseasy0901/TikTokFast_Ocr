# -*- coding: utf-8 -*-
"""
Test runner for the license server
"""

import sys
import os
import uvicorn
from unittest.mock import patch

# Add the app directory to the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'app'))

from database import SessionLocal, Base, engine
from models import License


def setup_test_db():
    """Setup test database"""
    # Create tables
    Base.metadata.create_all(bind=engine)
    print("Test database setup complete")


def cleanup_test_db():
    """Clean up test database"""
    with patch('app.database.SessionLocal') as mock_session:
        db = SessionLocal()
        # Clean up all licenses
        db.query(License).delete()
        db.commit()
        db.close()
    print("Test database cleanup complete")


def test_endpoints():
    """Run basic endpoint tests"""
    import requests

    base_url = "http://127.0.0.1:8000"

    # Test root endpoint
    try:
        response = requests.get(f"{base_url}/")
        assert response.status_code == 200
        print("✓ Root endpoint test passed")
    except Exception as e:
        print(f"✗ Root endpoint test failed: {e}")

    # Test invalid license validation
    try:
        response = requests.post(
            f"{base_url}/api/v1/licenses/validate",
            json={
                "license_key": "invalid-key",
                "device_id": "test-device"
            }
        )
        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is False
        print("✓ Invalid license validation test passed")
    except Exception as e:
        print(f"✗ Invalid license validation test failed: {e}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "setup":
        setup_test_db()
    elif len(sys.argv) > 1 and sys.argv[1] == "cleanup":
        cleanup_test_db()
    elif len(sys.argv) > 1 and sys.argv[1] == "test":
        test_endpoints()
    else:
        print("Usage:")
        print("  python test_server.py setup    - Setup test database")
        print("  python test_server.py cleanup  - Clean up test database")
        print("  python test_server.py test     - Run endpoint tests")
        print("  python test_server.py          - Start the server (default)")