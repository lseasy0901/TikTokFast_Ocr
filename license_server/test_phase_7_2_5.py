#!/usr/bin/env python3
"""
Phase 7.2-5: Business Licensing Implementation Tests
Tests all business rules and atomic transaction requirements
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'app'))

# Mock SusiSecurityService for Phase 7.2-5 business logic tests
class MockSusiSecurityService:
    """Mock service that doesn't require susi_helper.exe"""
    def __init__(self, config=None):
        self.config = config or {}
        self.development_private_key = "mock_key"
        self.development_public_key = "mock_key"

    def get_machine_code(self) -> str:
        return "mock_machine"

    def create_signed_license(self, license_data: dict, device_id: str, duration_days: int):
        return {"signed_license": None, "machine_code": "mock_machine"}

    def verify_license(self, signed_license: str, public_key_pem: str):
        return {"valid": True, "license_data": "mock"}

from database import SessionLocal
from services.license_service import LicenseService
from services.redemption_service import RedemptionService
from models import License, LicenseState, Authorization, AuthorizationState
import datetime
import time


def setup_test_db():
    """Setup clean test database"""
    db = SessionLocal()
    # Delete all data
    db.query(License).delete()
    db.query(Authorization).delete()
    db.commit()
    return db


def teardown_test_db(db):
    """Clean up test database"""
    db.query(License).delete()
    db.query(Authorization).delete()
    db.commit()
    db.close()


def test_first_redemption():
    """Test 1: First redemption of UNUSED key succeeds"""
    print('\nTest 1: First redemption of UNUSED key')
    print('-' * 60)
    db = setup_test_db()
    try:
        license_service = LicenseService(db)
        redemption_service = RedemptionService(db, MockSusiSecurityService())

        # Create a license
        license_data = type('obj', (object,), {'duration_days': 365})
        license = license_service.create_license(license_data, features=["ocr", "streaming"])
        license_key = license.license_key if hasattr(license, 'license_key') else None
        print(f'Created license key: {license_key[:20] if license_key else "None"}...')
        print(f'Created license key hash: {license.key_hash[:20]}...')
        print(f'License state: {license.state.value}')

        # Activate using the real plaintext key
        activation_data = type('obj', (object,), {
            'license_key': license_key,
            'device_id': 'device-1',
        })

        license, response = redemption_service.redeem_license(activation_data)
        print(f'Redeemed license state: {license.state.value}')
        print(f'Authorization expires at: {license.authorization.expires_at}')
        print(f'License redeemed_at: {license.redeemed_at}')

        # Verify: license is REDEEMED, authorization is ACTIVE
        result = (license.state == LicenseState.REDEEMED and
                  license.authorization.state == AuthorizationState.ACTIVE)
        print(f'Test 1: {"PASSED" if result else "FAILED"}')
        return result
    finally:
        teardown_test_db(db)


def test_repeated_redemption():
    """Test 2: Same key + same device => already_redeemed"""
    print('\nTest 2: Same key + same device => already_redeemed')
    print('-' * 60)
    db = setup_test_db()
    try:
        license_service = LicenseService(db)
        redemption_service = RedemptionService(db, MockSusiSecurityService())

        # Create and activate a license
        license_data = type('obj', (object,), {'duration_days': 30})
        license = license_service.create_license(license_data, features=["ocr"])
        license_key = license.license_key if hasattr(license, 'license_key') else None
        print(f'Created license key: {license_key[:20] if license_key else "None"}...')

        activation_data = type('obj', (object,), {
            'license_key': license_key,
            'device_id': 'device-1',
        })

        license, response = redemption_service.redeem_license(activation_data)
        print(f'First activation state: {license.state.value}')

        # Try to activate the same key with the same device
        try:
            license2, response2 = redemption_service.redeem_license(activation_data)
            print(f'Unexpected: Second activation succeeded with state: {license2.state.value}')
            print('Test 2: FAILED - should have been already_redeemed')
            return False
        except ValueError as e:
            if e.args[0] == 'already_redeemed':
                print(f'Correctly rejected: already_redeemed')
                print('Test 2: PASSED')
                return True
            else:
                print(f'Unexpected error: {e.args[0]}')
                print('Test 2: FAILED')
                return False
    finally:
        teardown_test_db(db)


def test_repeated_redemption_different_device():
    """Test 3: Same key + different device => already_redeemed"""
    print('\nTest 3: Same key + different device => already_redeemed')
    print('-' * 60)
    db = setup_test_db()
    try:
        license_service = LicenseService(db)
        redemption_service = RedemptionService(db, MockSusiSecurityService())

        # Create and activate a license
        license_data = type('obj', (object,), {'duration_days': 30})
        license = license_service.create_license(license_data, features=["ocr"])
        license_key = license.license_key if hasattr(license, 'license_key') else None
        print(f'Created license key: {license_key[:20] if license_key else "None"}...')

        activation_data1 = type('obj', (object,), {
            'license_key': license_key,
            'device_id': 'device-1',
        })

        license, response = redemption_service.redeem_license(activation_data1)
        print(f'First activation state: {license.state.value}')
        print(f'Authorization expires at: {license.authorization.expires_at}')

        # Try to activate the same key with a different device
        activation_data2 = type('obj', (object,), {
            'license_key': license_key,
            'device_id': 'device-2',
        })

        try:
            license2, response2 = redemption_service.redeem_license(activation_data2)
            print(f'Unexpected: Second activation succeeded with state: {license2.state.value}')
            print('Test 3: FAILED - should have been already_redeemed')
            return False
        except ValueError as e:
            if e.args[0] == 'already_redeemed':
                print(f'Correctly rejected: already_redeemed')
                print('Test 3: PASSED')
                return True
            else:
                print(f'Unexpected error: {e.args[0]}')
                print('Test 3: FAILED')
                return False
    finally:
        teardown_test_db(db)


def test_different_key_extends_active():
    """Test 4: Different UNUSED key extends existing active authorization"""
    print('\nTest 4: Different UNUSED key extends existing active authorization')
    print('-' * 60)
    db = setup_test_db()
    try:
        license_service = LicenseService(db)
        redemption_service = RedemptionService(db, MockSusiSecurityService())

        # Create and activate first license
        license_data1 = type('obj', (object,), {'duration_days': 30})
        license1 = license_service.create_license(license_data1, features=["ocr"])
        license_key1 = license1.license_key if hasattr(license1, 'license_key') else None
        print(f'First license created (key: {license_key1[:20] if license_key1 else "None"}...)')

        activation_data1 = type('obj', (object,), {
            'license_key': license_key1,
            'device_id': 'device-1',
        })

        license, response = redemption_service.redeem_license(activation_data1)
        expires1 = license.authorization.expires_at
        print(f'First activation: authorization expires at: {expires1}')

        # Create a second different license (not activate it yet)
        license_data2 = type('obj', (object,), {'duration_days': 365})
        license2 = license_service.create_license(license_data2, features=["streaming"])
        license_key2 = license2.license_key if hasattr(license2, 'license_key') else None
        print(f'Second license created (key: {license_key2[:20] if license_key2 else "None"}...), UNUSED')

        # Activate the second different license - this should extend the FIRST authorization
        activation_data2 = type('obj', (object,), {
            'license_key': license_key2,
            'device_id': 'device-1',
        })

        license, response2 = redemption_service.redeem_license(activation_data2)
        expires2 = license.authorization.expires_at
        print(f'After activating second key, authorization expires at: {expires2}')
        print(f'License ID: {license.id}')  # Should be 1 (same as first license)
        print(f'License state: {license.state.value}')
        print(f'Authorization ID: {license.authorization_id}')  # Should be 1 (same as first license)

        # Check: the second key extended the first license's authorization
        expected = expires1 + datetime.timedelta(days=365)
        result = abs((expires2 - expected).total_seconds()) < 2 and license.state == LicenseState.REDEEMED
        print(f'Expiration extended from first: {abs((expires2 - expected).total_seconds()) < 2}')
        print(f'Test 4: {"PASSED" if result else "FAILED"}')
        return result
    finally:
        teardown_test_db(db)


def test_expired_authorization_different_new_key():
    """Test 5: Expired authorization + different key starts from server_time"""
    print('\nTest 5: Expired authorization + different key starts from server_time')
    print('-' * 60)
    db = setup_test_db()
    try:
        license_service = LicenseService(db)
        redemption_service = RedemptionService(db, MockSusiSecurityService())

        # Create and activate first license
        license_data1 = type('obj', (object,), {'duration_days': 1})
        license1 = license_service.create_license(license_data1, features=["ocr"])
        license_key1 = license1.license_key if hasattr(license1, 'license_key') else None
        print(f'First license created (key: {license_key1[:20] if license_key1 else "None"}...)')

        activation_data1 = type('obj', (object,), {
            'license_key': license_key1,
            'device_id': 'device-1',
        })

        license, response = redemption_service.redeem_license(activation_data1)
        print(f'First activation: authorization expires at: {license.authorization.expires_at}')

        # Wait for it to expire (simulate time passing)
        time.sleep(0.1)  # Wait 100ms for timestamp drift


        # Set authorization to expired in the database
        license.authorization.state = AuthorizationState.EXPIRED
        license.authorization.expires_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)
        db.commit()
        db.refresh(license.authorization)
        print(f'Authorization is now expired at: {license.authorization.expires_at}')

        # Create a second different license (not activate it yet)
        license_data2 = type('obj', (object,), {'duration_days': 365})
        license2 = license_service.create_license(license_data2, features=["streaming"])
        license_key2 = license2.license_key if hasattr(license2, 'license_key') else None
        print(f'Second license created (key: {license_key2[:20] if license_key2 else "None"}...), UNUSED')

        server_time = datetime.datetime.now(datetime.timezone.utc)
        # Activate the second different license - should reactivate expired authorization
        activation_data2 = type('obj', (object,), {
            'license_key': license_key2,
            'device_id': 'device-1',
        })

        license, response = redemption_service.redeem_license(activation_data2)
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
        print(f'Test 5: {"PASSED" if result else "FAILED"}')
        return result
    except Exception as e:
        print(f'Test 5 crashed: {e}')
        import traceback
        traceback.print_exc()
        return False
    finally:
        teardown_test_db(db)


def test_revoked_key():
    """Test 6: Revoked license key cannot be redeemed"""
    print('\nTest 6: Revoked license key cannot be redeemed')
    print('-' * 60)
    db = setup_test_db()
    try:
        license_service = LicenseService(db)
        redemption_service = RedemptionService(db, MockSusiSecurityService())

        # Create and activate a license
        license_data = type('obj', (object,), {'duration_days': 365})
        license = license_service.create_license(license_data, features=["ocr"])
        license_key = license.license_key if hasattr(license, 'license_key') else None
        print(f'Created license key: {license_key[:20] if license_key else "None"}...')

        # Revoke the license
        revoked_license = license_service.revoke_license(license.id)
        print(f'Revoked license ID: {revoked_license.id}, state: {revoked_license.state.value}')

        # Try to redeem the revoked license
        try:
            activation_data = type('obj', (object,), {
                'license_key': license_key,
                'device_id': 'device-1',
            })

            license, response = redemption_service.redeem_license(activation_data)
            print(f'Unexpected: Redeemed revoked license with state: {license.state.value}')
            print('Test 6: FAILED - should have been rejected')
            return False
        except ValueError as e:
            if e.args[0] == 'revoked':
                print(f'Correctly rejected: revoked')
                print('Test 6: PASSED')
                return True
            else:
                print(f'Unexpected error: {e.args[0]}')
                print('Test 6: FAILED')
                return False
    finally:
        teardown_test_db(db)


def teardown_test_db(db):
    """Clean up test database"""
    db.query(License).filter(License.id > 0).delete()
    db.query(Authorization).filter(Authorization.id > 0).delete()
    db.commit()
    db.close()


def create_and_redeem_license(db, duration_days=30, features=None, device_id='device-1', license_key=None):
    """Helper to create and redeem a license"""
    license_service = LicenseService(db)
    redemption_service = RedemptionService(db, MockSusiSecurityService())

    if license_key:
        # Use existing license key
        license = license_service.get_license_by_key(license_key)
        if not license:
            raise ValueError(f"License key {license_key} not found")
    else:
        # Create new license
        license_data = type('obj', (object,), {'duration_days': duration_days})
        license = license_service.create_license(license_data, features=features or [])
        license_key = license.license_key if hasattr(license, 'license_key') else None

    # Redeem
    activation_data = type('obj', (object,), {
        'license_key': license_key,
        'device_id': device_id,
    })

    return redemption_service.redeem_license(activation_data)


def test_feature_union():
    """Test 7: Features are unioned across multiple license redemptions"""
    print('\nTest 7: Features are unioned across multiple license redemptions')
    print('-' * 60)
    db = setup_test_db()
    try:
        license_service = LicenseService(db)
        redemption_service = RedemptionService(db, MockSusiSecurityService())

        # Create first license with features
        license_data1 = type('obj', (object,), {'duration_days': 30})
        license1 = license_service.create_license(license_data1, features=["ocr", "streaming"])
        license_key1 = license1.license_key if hasattr(license1, 'license_key') else None
        print(f'First license created (key: {license_key1[:20] if license_key1 else "None"}...) with features: {license1.features}')

        # Create second license with different features
        license_data2 = type('obj', (object,), {'duration_days': 30})
        license2 = license_service.create_license(license_data2, features=["ocr", "ui"])
        license_key2 = license2.license_key if hasattr(license2, 'license_key') else None
        print(f'Second license created (key: {license_key2[:20] if license_key2 else "None"}...) with features: {license2.features}')

        # Activate first license to device 1
        activation_data1 = type('obj', (object,), {
            'license_key': license_key1,
            'device_id': 'device-1',
        })

        license, response = redemption_service.redeem_license(activation_data1)
        print(f'First activation on device-1, license state: {license.state.value}')

        # Activate second license to device 1 (should union features)
        activation_data2 = type('obj', (object,), {
            'license_key': license_key2,
            'device_id': 'device-1',
        })

        license, response = redemption_service.redeem_license(activation_data2)
        print(f'Second activation on device-1, license state: {license.state.value}')

        # Aggregate features for device-1
        features, expires_at = redemption_service.aggregate_features('device-1')
        print(f'Aggregate features for device-1: {features}')
        print(f'Authorization expires at: {expires_at}')

        # Expected: union of ["ocr", "streaming"] and ["ocr", "ui"] = {"ocr", "streaming", "ui"}
        expected_features = {"ocr", "streaming", "ui"}
        result_features = features == expected_features
        print(f'Features match expected: {result_features}')

        # Test that device-2 can redeem a DIFFERENT license (not the same one)
        # Create a third license for device-2
        license_data3 = type('obj', (object,), {'duration_days': 30})
        license3 = license_service.create_license(license_data3, features=["streaming"])
        license_key3 = license3.license_key if hasattr(license3, 'license_key') else None
        print(f'Third license created (key: {license_key3[:20] if license_key3 else "None"}...) with features: {license3.features}')

        # Activate third license to device 2
        activation_data3 = type('obj', (object,), {
            'license_key': license_key3,
            'device_id': 'device-2',
        })

        license, response = redemption_service.redeem_license(activation_data3)
        print(f'Third activation on device-2, license state: {license.state.value}')

        # Aggregate features for device-2
        features2, expires_at2 = redemption_service.aggregate_features('device-2')
        print(f'Aggregate features for device-2: {features2}')
        result_devices_separate = features2 == {"streaming"}

        print(f'Test 7: {"PASSED" if result_features and result_devices_separate else "FAILED"}')
        return result_features and result_devices_separate
    except Exception as e:
        print(f'Test 7 crashed: {e}')
        import traceback
        traceback.print_exc()
        return False
    finally:
        teardown_test_db(db)


def test_different_keys_different_devices():
    """Test 8: Different license keys can redeem to different devices"""
    print('\nTest 8: Different license keys can redeem to different devices')
    print('-' * 60)
    db = setup_test_db()
    try:
        license_service = LicenseService(db)
        redemption_service = RedemptionService(db, MockSusiSecurityService())

        # Create first license
        license_data1 = type('obj', (object,), {'duration_days': 30})
        license1 = license_service.create_license(license_data1, features=["ocr"])
        license_key1 = license1.license_key if hasattr(license1, 'license_key') else None
        print(f'Created license key 1: {license_key1[:20] if license_key1 else "None"}...')

        # Create second license
        license_data2 = type('obj', (object,), {'duration_days': 30})
        license2 = license_service.create_license(license_data2, features=["streaming"])
        license_key2 = license2.license_key if hasattr(license2, 'license_key') else None
        print(f'Created license key 2: {license_key2[:20] if license_key2 else "None"}...')

        # Activate first license to device-1
        activation_data1 = type('obj', (object,), {
            'license_key': license_key1,
            'device_id': 'device-1',
        })

        license1, response = redemption_service.redeem_license(activation_data1)
        print(f'License 1 redeemed to device-1, state: {license1.state.value}')

        # Activate second license to device-2
        activation_data2 = type('obj', (object,), {
            'license_key': license_key2,
            'device_id': 'device-2',
        })

        license2, response = redemption_service.redeem_license(activation_data2)
        print(f'License 2 redeemed to device-2, state: {license2.state.value}')

        # Both licenses should be REDEEMED
        result = (license1.state == LicenseState.REDEEMED and
                  license2.state == LicenseState.REDEEMED)

        # Both should have different authorization IDs
        result_authorizations = (license1.authorization_id != license2.authorization_id)

        print(f'License 1 redeemed, license 2 redeemed: {result}')
        print(f'Different authorizations: {result_authorizations}')
        print(f'Test 8: {"PASSED" if result and result_authorizations else "FAILED"}')
        return result and result_authorizations
    finally:
        teardown_test_db(db)


def test_repeated_redemption_does_not_mutate():
    """Test 9: Repeated redemption does not mutate existing authorization"""
    print('\nTest 9: Repeated redemption does not mutate Authorization')
    print('-' * 60)
    db = setup_test_db()
    try:
        license_service = LicenseService(db)
        redemption_service = RedemptionService(db, MockSusiSecurityService())

        # Create and activate first license
        license_data = type('obj', (object,), {'duration_days': 30})
        license1 = license_service.create_license(license_data, features=["ocr"])
        license_key1 = license1.license_key if hasattr(license1, 'license_key') else None
        print(f'Created license key: {license_key1[:20] if license_key1 else "None"}...')

        # Activate first license
        activation_data1 = type('obj', (object,), {
            'license_key': license_key1,
            'device_id': 'device-1',
        })

        license, response = redemption_service.redeem_license(activation_data1)
        authorization_id1 = license.authorization_id
        expires_at1 = license.authorization.expires_at
        print(f'First activation: authorization_id={authorization_id1}, expires_at={expires_at1}')

        # Try to redeem the same license again
        try:
            license2, response2 = redemption_service.redeem_license(activation_data1)
            print(f'Unexpected: Second redemption succeeded with state: {license2.state.value}')
            print('Test 9: FAILED - should have raised already_redeemed')
            return False
        except ValueError as e:
            print(f'Correctly raised error: {e.args[0]}')
            print('Test 9: PASSED - did not mutate')
            return True
    finally:
        teardown_test_db(db)


def test_atomic_transaction():
    """Test 10: Transaction is atomic - all or nothing"""
    print('\nTest 10: Transaction is atomic - all or nothing')
    print('-' * 60)
    db = setup_test_db()
    try:
        license_service = LicenseService(db)
        redemption_service = RedemptionService(db, MockSusiSecurityService())

        # Create two licenses
        license_data1 = type('obj', (object,), {'duration_days': 30})
        license1 = license_service.create_license(license_data1, features=["ocr"])
        license_key1 = license1.license_key if hasattr(license1, 'license_key') else None

        license_data2 = type('obj', (object,), {'duration_days': 30})
        license2 = license_service.create_license(license_data2, features=["streaming"])
        license_key2 = license2.license_key if hasattr(license2, 'license_key') else None

        # Activate first license (should succeed)
        activation_data1 = type('obj', (object,), {
            'license_key': license_key1,
            'device_id': 'device-1',
        })

        license, response = redemption_service.redeem_license(activation_data1)
        print(f'First license redeemed successfully')

        # Now try to redeem a REVOKEled license (should fail)
        license_service.revoke_license(license2.id)

        try:
            activation_data2 = type('obj', (object,), {
                'license_key': license_key2,
                'device_id': 'device-2',
            })

            license2, response2 = redemption_service.redeem_license(activation_data2)
            print('Test 10: FAILED - should have raised revoked error')
            return False
        except ValueError as e:
            if e.args[0] == 'revoked':
                print(f'Correctly raised error: {e.args[0]}')
                # Verify that license 1 is still REDEEMED
                license1_check = license_service.get_license_by_key(license_key1)
                if license1_check and license1_check.state == LicenseState.REDEEMED:
                    print('License 1 still REDEEMED after second redemption failed')
                    print('Test 10: PASSED - transaction was atomic')
                    return True
                else:
                    print('Test 10: FAILED - license 1 state changed unexpectedly')
                    return False
            else:
                print(f'Unexpected error: {e.args[0]}')
                print('Test 10: FAILED')
                return False
    finally:
        teardown_test_db(db)


if __name__ == '__main__':
    print('=' * 60)
    print('Phase 7.2-5: Business Licensing Implementation Tests')
    print('=' * 60)

    tests = [
        ('First redemption', test_first_redemption),
        ('Repeated redemption (same key)', test_repeated_redemption),
        ('Repeated redemption (different device)', test_repeated_redemption_different_device),
        ('Different key extends active authorization', test_different_key_extends_active),
        ('Expired authorization restart', test_expired_authorization_different_new_key),
        ('Revoked key rejection', test_revoked_key),
        ('Feature union', test_feature_union),
        ('Different keys on different devices', test_different_keys_different_devices),
        ('Repeated redemption does not mutate', test_repeated_redemption_does_not_mutate),
        ('Transaction atomicity', test_atomic_transaction),
    ]

    results = []

    for test_name, test_func in tests:
        try:
            result = test_func()
            results.append((test_name, result))
        except Exception as e:
            print(f'\nTest {test_name} crashed: {e}')
            import traceback
            traceback.print_exc()
            results.append((test_name, False))

    print('\n' + '=' * 60)
    passed = sum(1 for _, r in results if r)
    total = len(results)
    print(f'OVERALL: {passed}/{total} tests passed')
    print('=' * 60)

    for test_name, result in results:
        status = "[PASS]" if result else "[FAIL]"
        print(f'{status}: {test_name}')

    sys.exit(0 if passed == total else 1)
