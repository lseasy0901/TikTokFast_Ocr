#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Test access status functionality

授权策略：7 天免费试用已移除。
本地没有有效授权 → EXPIRED（受保护功能拒绝）；
已激活且未到期 → ACTIVE / EXPIRING；已过期 → EXPIRED。
"""

import os
import sys
from datetime import datetime, timedelta, timezone

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils.access_status import AccessStatus, AccessState, NOT_ACTIVATED_TEXT

_PASSED = 0
_FAILED = 0


def check(condition, message: str) -> bool:
    global _PASSED, _FAILED
    if condition:
        _PASSED += 1
        print(f"    [ok]   {message}")
    else:
        _FAILED += 1
        print(f"    [FAIL] {message}")
    return bool(condition)


def test_access_status():
    """Test access status states"""
    print("=== Testing Access Status ===")

    print("\n--- Unlicensed: no free trial anymore ---")
    access = AccessStatus()
    check(access.get_state() is AccessState.EXPIRED,
          "fresh instance reports EXPIRED (no trial window)")
    check(access.is_expired() is True,
          "fresh instance is treated as expired/blocked")
    check(access.get_status_text() == NOT_ACTIVATED_TEXT,
          f"status text is the not-activated text (actual: {access.get_status_text()})")
    check(not hasattr(access, "start_trial") and not hasattr(access, "reset_trial"),
          "trial API (start_trial/reset_trial) no longer exists")
    check(not hasattr(access, "activate"),
          "local days-based activate() no longer exists")
    check(not hasattr(AccessState, "TRIAL"),
          "AccessState.TRIAL no longer exists")

    print("\n--- Activated (30 days) ---")
    access.set_license_active(datetime.now(timezone.utc) + timedelta(days=30))
    check(access.get_state() is AccessState.ACTIVE, "activated → ACTIVE")
    check(access.is_expired() is False, "activated → not blocked")
    print(f"Status text: {access.get_status_text()}")

    print("\n--- Expiring soon (12 hours) ---")
    access.set_license_active(datetime.now(timezone.utc) + timedelta(hours=12))
    check(access.get_state() is AccessState.EXPIRING, "near expiry → EXPIRING")
    check(access.is_expired() is False, "near expiry → still allowed")

    print("\n--- Expired ---")
    access.set_license_active(datetime.now(timezone.utc) - timedelta(hours=2))
    check(access.get_state() is AccessState.EXPIRED, "lapsed license → EXPIRED")
    check(access.is_expired() is True, "lapsed license → blocked")

    print("\n--- Permanent license ---")
    access.set_license_active(None)
    check(access.get_state() is AccessState.ACTIVE, "no expiry → permanent ACTIVE")
    check(access.is_expired() is False, "permanent license → not blocked")

    print("\n--- Back to unlicensed ---")
    access.clear_license_state()
    check(access.is_expired() is True, "cleared state blocks again")
    check(access.get_status_text() == NOT_ACTIVATED_TEXT,
          "cleared state shows the not-activated text")

    print("\n--- Invalid license note ---")
    access.set_license_inactive("许可证已过期")
    check(access.is_expired() is True, "inactive license → blocked")
    check(access.get_status_text() == "许可证已过期",
          "inactive reason is shown verbatim")

    print("\n=== Test Complete ===")


if __name__ == "__main__":
    test_access_status()
    total = _PASSED + _FAILED
    print(f"\nRESULT: {_PASSED}/{total} checks passed, {_FAILED} failed")
    sys.exit(1 if _FAILED else 0)
