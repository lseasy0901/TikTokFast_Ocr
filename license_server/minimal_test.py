#!/usr/bin/env python3
"""
Minimal test to identify the issue
"""

import sys
import os

# Add the app directory to Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'app'))

try:
    from config import settings
    print("[OK] config imported")

    from database import engine, Base, SessionLocal
    print("[OK] database imported")

    from models import License
    print("[OK] models imported")

    from services.license_service import generate_secure_key
    key = generate_secure_key()
    print(f"[OK] Generated key: {key}")
    print(f"[OK] Key length: {len(str(key))}")

    # Test database
    Base.metadata.create_all(bind=engine)
    print("[OK] Database created")

except Exception as e:
    print(f"[ERROR] {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)