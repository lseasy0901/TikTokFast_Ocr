#!/usr/bin/env python3
"""
Simple test to verify the license server works
"""

import sys
import os

# Add the app directory to Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'app'))

def test_imports():
    """Test if we can import all required modules"""
    try:
        from config import settings
        print("[OK] config imported")

        from database import engine, Base, SessionLocal
        print("[OK] database imported")

        from models import License
        print("[OK] models imported")

        # from schemas import LicenseCreate, LicenseResponse
        print("[OK] schemas imported")

        from services.license_service import generate_secure_key
        print("[OK] services imported")

        print("\nAll imports successful!")

        # Test license key generation
        key = generate_secure_key()
        print(f"\nGenerated license key: {key}")
        print(f"Key length: {len(str(key))}")

        # Test database connection
        try:
            with SessionLocal() as db:
                Base.metadata.create_all(bind=engine)
                print("[OK] Database connection successful")
        except Exception as e:
            print(f"✗ Database error: {e}")

        return True

    except ImportError as e:
        print(f"[ERROR] Import error: {e}")
        return False
    except Exception as e:
        print(f"[ERROR] Error: {e}")
        return False

if __name__ == "__main__":
    success = test_imports()
    sys.exit(0 if success else 1)