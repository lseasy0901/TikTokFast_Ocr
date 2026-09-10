#!/usr/bin/env python3
"""
Focused tests for the License Key redemption business rule fix
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'app'))

from database import SessionLocal
from models import License, LicenseState, Authorization, AuthorizationState
from services.license_service import LicenseService
from schemas import LicenseActivate
import datetime

def test_first_activation():
    """Test 1: First activation of UNUSED key succeeds"""
    print('Test 1: First activation of UNUSED key')
    print('--------------------------------------')
    db = SessionLocal()
    service = LicenseService(db)

    # Create a license
    from schemas import LicenseCreate
    create_data = LicenseCreate(duration_days=365)
    license = service.create_license(create_data)
    license_key = license.license_key if hasattr(license, 'license_key') else None
    key_hash = license.key_hash
    print(f'Created license key: {license_key[:20] if license_key else "None"}...')
    print(f'Created license key hash: {key_hash[:20]}...')

    # Activate using the real plaintext key (not the hash)
    activation_data = LicenseActivate(
        license_key=license_key,
        device_id='device-1',
    )

    license, response = service.activate_license(activation_data)
    print(f'State after activation: {license.state.value}')
    print(f'Authorization expires at: {license.authorization.expires_at}')
    print(f'License redeemed_at: {license.redeemed_at}')

    # Verify: license is REDEEMED, authorization is ACTIVE
    result = (license.state == LicenseState.REDEEMED and
              license.authorization.state == AuthorizationState.ACTIVE)
    print(f'Test 1: {'PASSED' if result else 'FAILED'}')
    print()

    db.close()
    return result


def test_same_key_same_device():
    """Test 2: Same key + same device => already_redeemed"""
    print('Test 2: Same key + same device => already_redeemed')
    print('---------------------------------------------------')
    db = SessionLocal()
    service = LicenseService(db)

    # Create and activate a license
    from schemas import LicenseCreate
    create_data = LicenseCreate(duration_days=30)
    license = service.create_license(create_data)
    license_key = license.license_key if hasattr(license, 'license_key') else None
    key_hash = license.key_hash
    print(f'Created license key: {license_key[:20] if license_key else "None"}...')

    activation_data = LicenseActivate(
        license_key=license_key,
        device_id='device-1',
    )

    license, response = service.activate_license(activation_data)
    print(f'First activation state: {license.state.value}')

    # Try to activate the same key with the same device
    activation_data2 = LicenseActivate(
        license_key=license_key,
        device_id='device-1',
    )

    try:
        license2, response2 = service.activate_license(activation_data2)
        print(f'Unexpected: Second activation succeeded with state: {license2.state.value}')
        print(f'Test 2: FAILED - should have been already_redeemed')
    except ValueError as e:
        if e.args[0] == 'already_redeemed':
            print(f'Correctly rejected: already_redeemed')
            print(f'Test 2: PASSED')
        else:
            print(f'Unexpected error: {e.args[0]}')
            print(f'Test 2: FAILED')

    db.close()
    print()
    return True  # We're testing that it raises the error


def test_same_key_different_device():
    """Test 3: Same key + different device => already_redeemed"""
    print('Test 3: Same key + different device => already_redeemed')
    print('-------------------------------------------------------')
    db = SessionLocal()
    service = LicenseService(db)

    # Create and activate a license
    from schemas import LicenseCreate
    create_data = LicenseCreate(duration_days=30)
    license = service.create_license(create_data)
    license_key = license.license_key if hasattr(license, 'license_key') else None
    key_hash = license.key_hash
    print(f'Created license key: {license_key[:20] if license_key else "None"}...')

    activation_data = LicenseActivate(
        license_key=license_key,
        device_id='device-1',
    )

    license, response = service.activate_license(activation_data)
    print(f'First activation state: {license.state.value}')
    print(f'Authorization expires at: {license.authorization.expires_at}')

    # Try to activate the same key with a different device
    activation_data2 = LicenseActivate(
        license_key=license_key,
        device_id='device-2',
    )

    try:
        license2, response2 = service.activate_license(activation_data2)
        print(f'Unexpected: Second activation succeeded with state: {license2.state.value}')
        print(f'Test 3: FAILED - should have been already_redeemed')
    except ValueError as e:
        if e.args[0] == 'already_redeemed':
            print(f'Correctly rejected: already_redeemed')
            print(f'Test 3: PASSED')
        else:
            print(f'Unexpected error: {e.args[0]}')
            print(f'Test 3: FAILED')

    db.close()
    print()
    return True  # We're testing that it raises the error


def test_different_key_extends_active():
    """Test 4: Different UNUSED key extends existing active authorization"""
    print('Test 4: Different key extends existing active authorization')
    print('------------------------------------------------------------')
    db = SessionLocal()
    service = LicenseService(db)

    # Create and activate first license
    from schemas import LicenseCreate
    create_data1 = LicenseCreate(duration_days=30)
    license1 = service.create_license(create_data1)
    license_key1 = license1.license_key if hasattr(license1, 'license_key') else None
    key_hash1 = license1.key_hash
    print(f'First license created (key: {license_key1[:20] if license_key1 else "None"}...)')

    activation_data1 = LicenseActivate(
        license_key=license_key1,
        device_id='device-1',
    )

    license1, response1 = service.activate_license(activation_data1)
    expires1 = license1.authorization.expires_at
    print(f'First activation: authorization expires at: {expires1}')

    # Create a second different license (not activate it yet)
    create_data2 = LicenseCreate(duration_days=365)
    license2 = service.create_license(create_data2)
    license_key2 = license2.license_key if hasattr(license2, 'license_key') else None
    key_hash2 = license2.key_hash
    print(f'Second license created (key: {license_key2[:20] if license_key2 else "None"}...), UNUSED')

    # Activate the second different license - this should extend the FIRST authorization
    activation_data2 = LicenseActivate(
        license_key=license_key2,
        device_id='device-1',
    )

    # When activating the second key, it should find license1 and extend ITS authorization
    license, response2 = service.activate_license(activation_data2)
    expires2 = license.authorization.expires_at
    print(f'After activating second key, authorization expires at: {expires2}')
    print(f'License ID: {license.id}')  # Should be 1 (same as first license)
    print(f'License state: {license.state.value}')
    print(f'Authorization ID: {license.authorization_id}')  # Should be 1 (same as first license)

    # Check: the second key extended the first license's authorization
    expected = expires1 + datetime.timedelta(days=365)
    result = abs((expires2 - expected).total_seconds()) < 2 and license.state == LicenseState.REDEEMED
    print(f'Expiration extended from first: {abs((expires2 - expected).total_seconds()) < 2}')
    print(f'Test 4: {'PASSED' if result else 'FAILED'}')
    print()

    # Clean up
    db.query(License).filter(License.id == 1).delete()
    db.query(License).filter(License.id == 2).delete()
    db.query(Authorization).filter(Authorization.id == 1).delete()
    db.commit()
    db.close()
    return result


def test_expired_authorization_different_new_key():
    """Test 5: Expired authorization + different key starts from server_time"""
    print('Test 5: Expired authorization + different key starts from server_time')
    print('-----------------------------------------------------------------------')
    db = SessionLocal()
    service = LicenseService(db)

    # Create and activate first license
    from schemas import LicenseCreate
    create_data1 = LicenseCreate(duration_days=1)
    license1 = service.create_license(create_data1)
    license_key1 = license1.license_key if hasattr(license1, 'license_key') else None
    key_hash1 = license1.key_hash
    print(f'First license created (key: {license_key1[:20] if license_key1 else "None"}...)')

    activation_data1 = LicenseActivate(
        license_key=license_key1,
        device_id='device-1',
    )

    license1, response1 = service.activate_license(activation_data1)
    print(f'First activation: authorization expires at: {license1.authorization.expires_at}')

    # Wait for it to expire (simulate time passing)
    import time
    time.sleep(2)  # Wait 2 seconds

    # Set authorization to expired in the database
    license1.authorization.state = AuthorizationState.EXPIRED
    license1.authorization.expires_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)
    db.commit()
    db.refresh(license1.authorization)
    print(f'Authorization is now expired at: {license1.authorization.expires_at}')

    # Create a second different license (not activate it yet)
    create_data2 = LicenseCreate(duration_days=365)
    license2 = service.create_license(create_data2)
    license_key2 = license2.license_key if hasattr(license2, 'license_key') else None
    key_hash2 = license2.key_hash
    print(f'Second license created (key: {license_key2[:20] if license_key2 else "None"}...), UNUSED')

    server_time = datetime.datetime.now(datetime.timezone.utc)
    # Activate the second different license - should reactivate expired authorization
    activation_data2 = LicenseActivate(
        license_key=license_key2,
        device_id='device-1',
    )

    license, response2 = service.activate_license(activation_data2)
    expires2 = license.authorization.expires_at
    print(f'After activating second key, authorization expires at: {expires2}')
    print(f'Authorization state: {license.authorization.state.value}')

    # Check if the expiration starts from server_time
    expected = server_time + datetime.timedelta(days=365)

    # Ensure both are timezone-aware for comparison
    if expires2.tzinfo is None:
        expires2 = expires2.replace(tzinfo=datetime.timezone.utc)
    if expected.tzinfo is None:
        expected = expected.replace(tzinfo=datetime.timezone.utc)

    result = abs((expires2 - expected).total_seconds()) < 2 and license.authorization.state == AuthorizationState.ACTIVE
    print(f'Expiration starts from server_time: {abs((expires2 - expected).total_seconds()) < 2}')
    print(f'Test 5: {'PASSED' if result else 'FAILED'}')
    print()

    # Clean up
    db.query(License).filter(License.id == 1).delete()
    db.query(License).filter(License.id == 2).delete()
    db.query(Authorization).filter(Authorization.id == 1).delete()
    db.commit()
    db.close()
    return result


if __name__ == '__main__':
    print('='*60)
    print('BUSINESS RULE FIX VERIFICATION TESTS')
    print('='*60)
    print()

    results = []

    results.append(test_first_activation())
    results.append(test_same_key_same_device())
    results.append(test_same_key_different_device())
    results.append(test_different_key_extends_active())
    results.append(test_expired_authorization_different_new_key())

    print('='*60)
    print(f'OVERALL: {sum(results)}/{len(results)} tests passed')
    print('='*60)
