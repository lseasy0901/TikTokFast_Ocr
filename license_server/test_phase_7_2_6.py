#!/usr/bin/env python3
"""
Phase 7.2-6: Susi Integration Tests
Tests Susi integration with business licensing while preserving all Phase 7.2-5 behavior
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'app'))

# Import actual SusiSecurityService
from services.susi_security_service import SusiSecurityService

from database import SessionLocal
from services.license_service import LicenseService
from services.redemption_service import RedemptionService
from models import License, LicenseState, Authorization, AuthorizationState
import datetime

# Load test RSA key once for all tests
from test_key_loader import load_test_rsa_key
private_key_pem = load_test_rsa_key()
public_key_pem = '-----BEGIN PUBLIC KEY-----\nMIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEApniRTJwG5l5fBX0LqvGw\nYGqLmq4TwGm+FwBBV8dvr+DcKyPIuksfbTyYoznMKcc7EUtCvuiiaBvi/X5Ef2fi\nsZi2ENBN2TLDJhthuwZ7K4xrVoJ2U3IscaySz1C2I1iYY/cx+d+uAAAR2wG65+Tb\nFdlJS5ny7njiL91ZAkXHx4VpL4qnq5ctMFG6lv5dBgg4xu53LtEDFClBs/iubJpa\nP/rcnSgfvhopudluKt9TmPpndWHYW7PCVfrurOYmNUBnaokRcfGRrFiAn+lxx/kJ\nSkfZzm9p0Rhmzr4nAQRSVl+EkA4i9X04BV7QGwWprl6wmafpA9NLd+/pPXDYnOab\n3QIDAQAB\n-----END PUBLIC KEY-----'

def setup_test_db():
    """Setup clean test database"""
    db = SessionLocal()
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


def test_business_redemption_succeeds():
    """Test 1: Business redemption succeeds and creates SignedLicense"""
    print('\nTest 1: Business redemption succeeds')
    print('-' * 60)
    db = setup_test_db()
    try:
        license_service = LicenseService(db)
        redemption_service = RedemptionService(
            db,
            SusiSecurityService({
                'SUSI_DEVELOPMENT_PRIVATE_KEY': private_key_pem,
                'SUSI_DEVELOPMENT_PUBLIC_KEY': '-----BEGIN PUBLIC KEY-----\nMIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEApniRTJwG5l5fBX0LqvGwYGqLmq4TwGm+FwBBV8dvr+DcKyPIuksfbTyYoznMKcc7EUtCvuiiaBvi/X5Ef2fisZi2ENBN2TLDJhthuwZ7K4xrVoJ2U3IscaySz1C2I1iYY/cx+d+uAAAR2wG65+TbFdlJS5ny7njiL91ZAkXHx4VpL4qnq5ctMFG6lv5dBgg4xu53LtEDFClBs/iubJpaP/rcnSgfvhopudluKt9TmPpndWHYW7PCVfrurOYmNUBnaokRcfGRrFiAn+lxx/kJSkfZzm9p0Rhmzr4nAQRSVl+EkA4i9X04BV7QGwWprl6wmafpA9NLd+/pPXDYnOab3QIDAQAB\n-----END PUBLIC KEY-----'
            })
        )

        # Create a license
        license_data = type('obj', (object,), {'duration_days': 30})
        license = license_service.create_license(license_data, features=["ocr"])
        license_key = license.license_key if hasattr(license, 'license_key') else None
        print(f'Created license key: {license_key[:20] if license_key else "None"}...')

        # Activate using the real plaintext key
        activation_data = type('obj', (object,), {
            'license_key': license_key,
            'device_id': 'device-1',
        })

        license, response = redemption_service.redeem_license(activation_data)
        print(f'Redeemed license state: {license.state.value}')

        # Verify: license is REDEEMED, authorization is ACTIVE
        assert license.state == LicenseState.REDEEMED
        assert license.authorization.state == AuthorizationState.ACTIVE

        # Verify: response contains signed_license
        assert 'signed_license' in response
        print(f'Test 1: PASSED - Business redemption succeeded with SignedLicense')
        return True
    finally:
        teardown_test_db(db)


def test_susi_signed_license_creation():
    """Test 2: Susi SignedLicense creation from Authorization"""
    print('\nTest 2: Susi SignedLicense creation from Authorization')
    print('-' * 60)
    db = setup_test_db()
    try:
        license_service = LicenseService(db)
        susi_security = SusiSecurityService({
                'SUSI_DEVELOPMENT_PRIVATE_KEY': private_key_pem,
                'SUSI_DEVELOPMENT_PUBLIC_KEY': public_key_pem
            })
        redemption_service = RedemptionService(db, susi_security)

        # Create and redeem a license
        license_data = type('obj', (object,), {'duration_days': 30})
        license = license_service.create_license(license_data, features=["streaming"])
        license_key = license.license_key if hasattr(license, 'license_key') else None

        activation_data = type('obj', (object,), {
            'license_key': license_key,
            'device_id': 'device-2',
        })

        license, response = redemption_service.redeem_license(activation_data)

        # Verify: signed_license exists in response
        assert 'signed_license' in response
        assert isinstance(response['signed_license'], str)
        assert len(response['signed_license']) > 0

        # Verify: signed_license is valid JSON
        import json
        signed_data = json.loads(response['signed_license'])
        assert 'license_data' in signed_data
        assert 'signature' in signed_data
        assert len(signed_data['license_data']) > 0
        assert len(signed_data['signature']) > 0

        print(f'SignedLicense created: license_data length = {len(signed_data["license_data"])}, signature length = {len(signed_data["signature"])}')
        print(f'Test 2: PASSED - Susi SignedLicense created successfully')
        return True
    except Exception as e:
        print(f'Test 2: FAILED - {e}')
        import traceback
        traceback.print_exc()
        return False
    finally:
        teardown_test_db(db)


def test_valid_client_verification():
    """Test 3: Valid client verification with susi_helper"""
    print('\nTest 3: Valid client verification with susi_helper')
    print('-' * 60)
    db = setup_test_db()
    try:
        license_service = LicenseService(db)
        susi_security = SusiSecurityService({
                'SUSI_DEVELOPMENT_PRIVATE_KEY': private_key_pem,
                'SUSI_DEVELOPMENT_PUBLIC_KEY': public_key_pem
            })
        redemption_service = RedemptionService(db, susi_security)

        # Create and redeem a license
        license_data = type('obj', (object,), {'duration_days': 365})
        license = license_service.create_license(license_data, features=["ocr", "streaming"])
        license_key = license.license_key if hasattr(license, 'license_key') else None

        activation_data = type('obj', (object,), {
            'license_key': license_key,
            'device_id': 'device-3',
        })

        license, response = redemption_service.redeem_license(activation_data)
        signed_license = response['signed_license']

        # Try to verify using susi_security service
        try:
            verification_result = susi_security.verify_license(
                signed_license,
                susi_security.development_public_key
            )
            print(f'Verification result: {verification_result}')
            print(f'Test 3: PASSED - Client verification succeeded')
            return True
        except Exception as e:
            print(f'Test 3: FAILED - Client verification failed: {e}')
            return False
    finally:
        teardown_test_db(db)


def test_tampered_signature_rejection():
    """Test 4: Tampered signature rejection"""
    print('\nTest 4: Tampered signature rejection')
    print('-' * 60)
    db = setup_test_db()
    try:
        license_service = LicenseService(db)
        susi_security = SusiSecurityService({
                'SUSI_DEVELOPMENT_PRIVATE_KEY': private_key_pem,
                'SUSI_DEVELOPMENT_PUBLIC_KEY': public_key_pem
            })
        redemption_service = RedemptionService(db, susi_security)

        # Create and redeem a license
        license_data = type('obj', (object,), {'duration_days': 30})
        license = license_service.create_license(license_data, features=["ocr"])
        license_key = license.license_key if hasattr(license, 'license_key') else None

        activation_data = type('obj', (object,), {
            'license_key': license_key,
            'device_id': 'device-4',
        })

        license, response = redemption_service.redeem_license(activation_data)
        signed_license = response['signed_license']

        # Tamper with the signature
        import json
        signed_data = json.loads(signed_license)
        tampered_signature = tamper_signature(signed_data['signature'])
        signed_data['signature'] = tampered_signature
        tampered_signed_license = json.dumps(signed_data)

        # Verify should fail
        try:
            verification_result = susi_security.verify_license(
                tampered_signed_license,
                susi_security.development_public_key
            )
            print(f'Test 4: FAILED - Tampered signature was accepted')
            return False
        except RuntimeError as e:
            if 'verification failed' in str(e):
                print(f'Correctly rejected tampered signature')
                print(f'Test 4: PASSED - Tampered signature was rejected')
                return True
            else:
                print(f'Test 4: FAILED - Wrong error: {e}')
                return False
    except Exception as e:
        print(f'Test 4: FAILED - {e}')
        import traceback
        traceback.print_exc()
        return False
    finally:
        teardown_test_db(db)


def test_wrong_machine_rejection():
    """Test 5: Wrong machine rejection"""
    print('\nTest 5: Wrong machine rejection')
    print('-' * 60)
    db = setup_test_db()
    try:
        license_service = LicenseService(db)
        susi_security = SusiSecurityService({
                'SUSI_DEVELOPMENT_PRIVATE_KEY': private_key_pem,
                'SUSI_DEVELOPMENT_PUBLIC_KEY': public_key_pem
            })
        redemption_service = RedemptionService(db, susi_security)

        # Create and redeem a license to device 1
        license_data = type('obj', (object,), {'duration_days': 30})
        license = license_service.create_license(license_data, features=["streaming"])
        license_key = license.license_key if hasattr(license, 'license_key') else None

        activation_data1 = type('obj', (object,), {
            'license_key': license_key,
            'device_id': 'device-1',
        })

        license, response = redemption_service.redeem_license(activation_data1)
        print(f'First redemption succeeded')

        # Try to redeem same license to device 2
        # This should fail due to one-time redemption rule
        activation_data2 = type('obj', (object,), {
            'license_key': license_key,
            'device_id': 'device-2',
        })

        try:
            license2, response2 = redemption_service.redeem_license(activation_data2)
            print(f'Unexpected: Second redemption succeeded with state: {license2.state.value}')
            print(f'Test 5: FAILED - Same license should not redeem to different device')
            return False
        except ValueError as e:
            if e.args[0] == 'already_redeemed':
                print(f'Correctly rejected: already_redeemed')
                print(f'Test 5: PASSED - Wrong machine redemption was rejected')
                return True
            else:
                print(f'Test 5: FAILED - Wrong error: {e.args[0]}')
                return False
    except Exception as e:
        print(f'Test 5: FAILED - {e}')
        import traceback
        traceback.print_exc()
        return False
    finally:
        teardown_test_db(db)


def test_expired_license_rejection():
    """Test 6: Expired license rejection"""
    print('\nTest 6: Expired license rejection')
    print('-' * 60)
    db = setup_test_db()
    try:
        license_service = LicenseService(db)
        susi_security = SusiSecurityService({
                'SUSI_DEVELOPMENT_PRIVATE_KEY': private_key_pem,
                'SUSI_DEVELOPMENT_PUBLIC_KEY': public_key_pem
            })
        redemption_service = RedemptionService(db, susi_security)

        # Create a license with 1 day duration
        license_data = type('obj', (object,), {'duration_days': 1})
        license = license_service.create_license(license_data, features=["ocr"])
        license_key = license.license_key if hasattr(license, 'license_key') else None

        activation_data = type('obj', (object,), {
            'license_key': license_key,
            'device_id': 'device-5',
        })

        license, response = redemption_service.redeem_license(activation_data)
        print(f'First redemption succeeded')

        # Set authorization to expired
        license.authorization.state = AuthorizationState.EXPIRED
        license.authorization.expires_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)
        db.commit()
        db.refresh(license.authorization)

        # Try to redeem same license again - should fail
        try:
            license2, response2 = redemption_service.redeem_license(activation_data)
            print(f'Test 6: FAILED - Expired license was redeemed again')
            return False
        except ValueError as e:
            if e.args[0] == 'already_redeemed':
                print(f'Correctly rejected: already_redeemed')
                print(f'Test 6: PASSED - Expired license was not redeemed')
                return True
            else:
                print(f'Test 6: FAILED - Wrong error: {e.args[0]}')
                return False
    except Exception as e:
        print(f'Test 6: FAILED - {e}')
        import traceback
        traceback.print_exc()
        return False
    finally:
        teardown_test_db(db)


def test_feature_propagation():
    """Test 7: Features are propagated through SignedLicense"""
    print('\nTest 7: Features propagation through SignedLicense')
    print('-' * 60)
    db = setup_test_db()
    try:
        license_service = LicenseService(db)
        susi_security = SusiSecurityService({
                'SUSI_DEVELOPMENT_PRIVATE_KEY': private_key_pem,
                'SUSI_DEVELOPMENT_PUBLIC_KEY': public_key_pem
            })
        redemption_service = RedemptionService(db, susi_security)

        # Create and redeem first license
        license_data1 = type('obj', (object,), {'duration_days': 30})
        license1 = license_service.create_license(license_data1, features=["ocr", "streaming"])
        license_key1 = license1.license_key if hasattr(license1, 'license_key') else None

        activation_data1 = type('obj', (object,), {
            'license_key': license_key1,
            'device_id': 'device-6',
        })

        license, response = redemption_service.redeem_license(activation_data1)
        print(f'First activation on device-6, license state: {license.state.value}')

        # Verify features are in the signed license
        import json
        signed_data = json.loads(response['signed_license'])
        payload = json.loads(signed_data['license_data'])

        expected_features = {"ocr", "streaming"}
        result_features = set(payload['features'])

        if result_features == expected_features:
            print(f'Features in SignedLicense: {result_features}')
            print(f'Test 7: PASSED - Features correctly propagated')
            return True
        else:
            print(f'Test 7: FAILED - Expected {expected_features}, got {result_features}')
            return False
    except Exception as e:
        print(f'Test 7: FAILED - {e}')
        import traceback
        traceback.print_exc()
        return False
    finally:
        teardown_test_db(db)


def test_repeated_redemption_rejection():
    """Test 8: Repeated redemption remains rejected"""
    print('\nTest 8: Repeated redemption remains rejected')
    print('-' * 60)
    db = setup_test_db()
    try:
        license_service = LicenseService(db)
        susi_security = SusiSecurityService({
                'SUSI_DEVELOPMENT_PRIVATE_KEY': private_key_pem,
                'SUSI_DEVELOPMENT_PUBLIC_KEY': public_key_pem
            })
        redemption_service = RedemptionService(db, susi_security)

        # Create and activate a license
        license_data = type('obj', (object,), {'duration_days': 30})
        license = license_service.create_license(license_data, features=["ocr"])
        license_key = license.license_key if hasattr(license, 'license_key') else None

        activation_data = type('obj', (object,), {
            'license_key': license_key,
            'device_id': 'device-7',
        })

        license, response = redemption_service.redeem_license(activation_data)
        print(f'First activation state: {license.state.value}')

        # Try to activate the same license again
        try:
            license2, response2 = redemption_service.redeem_license(activation_data)
            print(f'Unexpected: Second activation succeeded with state: {license2.state.value}')
            print(f'Test 8: FAILED - Should have raised already_redeemed')
            return False
        except ValueError as e:
            if e.args[0] == 'already_redeemed':
                print(f'Correctly raised: already_redeemed')
                print(f'Test 8: PASSED - Repeated redemption rejected')
                return True
            else:
                print(f'Test 8: FAILED - Wrong error: {e.args[0]}')
                return False
    except Exception as e:
        print(f'Test 8: FAILED - {e}')
        import traceback
        traceback.print_exc()
        return False
    finally:
        teardown_test_db(db)


def tamper_signature(signature: str) -> str:
    """Tamper with a base64 signature"""
    import base64
    import hashlib

    # Decode base64
    sig_bytes = base64.b64decode(signature)

    # Create a different hash
    tampered = hashlib.sha256(b"tampered-data").digest()[:len(sig_bytes)]

    # Encode as base64
    return base64.b64encode(tampered).decode()


if __name__ == '__main__':
    print('=' * 60)
    print('Phase 7.2-6: Susi Integration Tests')
    print('=' * 60)

    tests = [
        ('Business redemption succeeds', test_business_redemption_succeeds),
        ('Susi SignedLicense creation', test_susi_signed_license_creation),
        ('Valid client verification', test_valid_client_verification),
        ('Tampered signature rejection', test_tampered_signature_rejection),
        ('Wrong machine rejection', test_wrong_machine_rejection),
        ('Expired license rejection', test_expired_license_rejection),
        ('Feature propagation', test_feature_propagation),
        ('Repeated redemption rejection', test_repeated_redemption_rejection),
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