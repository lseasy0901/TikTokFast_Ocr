#!/usr/bin/env python3
"""
Smoke test for the POST /api/v1/licenses/validate route.

Regression guard: the Phase 7.2-5/6 refactor moved validation logic from
LicenseService to RedemptionService, but app/main.py kept calling the removed
LicenseService.validate_license, so the endpoint raised AttributeError.

The route handler is invoked directly rather than through an ASGI test client,
because httpx (required by fastapi.testclient) is not a project dependency.
Calling it directly still exercises the exact binding that regressed.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app'))

from test_key_loader import load_test_rsa_key  # noqa: E402

# Configure the app's Settings *before* importing main, so the module-level
# SusiSecurityService (the one the routes actually use) has a usable signing key.
# This exercises the real production wiring rather than a test-only instance.
os.environ.setdefault('SUSI_DEVELOPMENT_PRIVATE_KEY', load_test_rsa_key())

import main  # noqa: E402  (import must follow sys.path / env setup)
import schemas  # noqa: E402
from database import SessionLocal  # noqa: E402
from models import License, Authorization, LicenseState  # noqa: E402
from services.license_service import LicenseService  # noqa: E402
from services.redemption_service import RedemptionService  # noqa: E402


def _reset(db):
    db.query(License).delete()
    db.query(Authorization).delete()
    db.commit()


def _validate(db, license_key, device_id):
    """Invoke the route handler exactly as FastAPI would."""
    return asyncio.run(
        main.validate_license(
            schemas.LicenseValidate(license_key=license_key, device_id=device_id),
            db=db,
        )
    )


def test_validate_route_unknown_key():
    """The handler resolves and reports an unknown key instead of raising."""
    print('\nTest 1: /licenses/validate with an unknown key')
    print('-' * 60)
    db = SessionLocal()
    try:
        _reset(db)

        result = _validate(db, 'A' * 32, 'device-unknown')

        assert isinstance(result, schemas.LicenseValidationResponse), type(result)
        assert result.valid is False, result
        assert result.state == 'invalid_key', result.state
        print(f'  state={result.state} valid={result.valid}')
        print('Test 1: PASSED - route is bound to a service that provides validate_license')
        return True
    except Exception as e:
        print(f'Test 1: FAILED - {e}')
        import traceback
        traceback.print_exc()
        return False
    finally:
        db.close()


def test_validate_route_redeemed_license():
    """A redeemed license validates for its own device, and only that device."""
    print('\nTest 2: /licenses/validate with a redeemed license')
    print('-' * 60)
    db = SessionLocal()
    try:
        _reset(db)

        license = LicenseService(db).create_license(
            type('obj', (object,), {'duration_days': 30}), features=["ocr"]
        )
        license_key = license.license_key

        RedemptionService(db, main.susi_security_service).redeem_license(
            type('obj', (object,), {
                'license_key': license_key,
                'device_id': 'device-smoke',
            })
        )

        own = _validate(db, license_key, 'device-smoke')
        assert own.valid is True, own
        assert own.state == LicenseState.REDEEMED.value, own.state
        assert own.remaining_seconds > 0, own.remaining_seconds
        print(f'  owning device: state={own.state} valid={own.valid} remaining={own.remaining_seconds}s')

        other = _validate(db, license_key, 'device-other')
        assert other.valid is False, other
        assert other.state == 'no_authorization', other.state
        print(f'  other device:  state={other.state} valid={other.valid}')

        print('Test 2: PASSED - validation stays scoped to the owning device')
        return True
    except Exception as e:
        print(f'Test 2: FAILED - {e}')
        import traceback
        traceback.print_exc()
        return False
    finally:
        db.close()


if __name__ == '__main__':
    print('=' * 60)
    print('License Server: /licenses/validate route smoke test')
    print('=' * 60)

    tests = [
        ('Unknown key rejected', test_validate_route_unknown_key),
        ('Redeemed license validates', test_validate_route_redeemed_license),
    ]

    results = []
    for test_name, test_func in tests:
        try:
            results.append((test_name, test_func()))
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
        print(f'{"[PASS]" if result else "[FAIL]"}: {test_name}')

    sys.exit(0 if passed == total else 1)
