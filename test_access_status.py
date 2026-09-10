#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Test access status functionality
"""

import sys
import time
from datetime import datetime, timedelta

# Add project root to path
sys.path.insert(0, '.')

from utils.access_status import AccessStatus, AccessState


def test_access_status():
    """Test access status states"""
    print("=== Testing Access Status ===")

    # Create access status
    access = AccessStatus()
    access.start_trial(1)  # 1 day trial

    print(f"Initial state: {access.get_state()}")
    print(f"Status text: {access.get_status_text()}")

    # Test activation
    print("\n--- Testing Activation ---")
    access.activate(7)  # 7 days activation
    print(f"After activation: {access.get_state()}")
    print(f"Status text: {access.get_status_text()}")

    # Test expiration
    print("\n--- Testing Expiration ---")
    # Set expiry time to 2 hours ago
    access._expiry_time = datetime.now() - timedelta(hours=2)
    print(f"Expired state: {access.get_state()}")
    print(f"Status text: {access.get_status_text()}")

    # Test expiring soon
    print("\n--- Testing Expiring Soon ---")
    access._expiry_time = datetime.now() + timedelta(hours=12)
    print(f"Expiring state: {access.get_state()}")
    print(f"Status text: {access.get_status_text()}")

    print("\n=== Test Complete ===")


if __name__ == "__main__":
    test_access_status()