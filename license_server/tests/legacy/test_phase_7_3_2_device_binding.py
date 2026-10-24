#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 7.3-2: remote client device binding regression.

Defect under test
-----------------
SusiSecurityService.create_signed_license() accepted a ``device_id`` but signed
``machine_codes: [self.get_machine_code()]`` -- the fingerprint of the machine the
LICENSE SERVER runs on. The credential was therefore bound to the server, and
clients only ever matched it while client and server were the same box (the local
E2E). Deployed to a remote ECS host every activation would return HTTP 200,
consume the key, and then fail the client's own binding check with WRONG_MACHINE.

What is pinned here
-------------------
A. activation on device A produces a credential whose machine_codes contains A,
   and the REAL client verification path accepts it.
B. the server's own machine code differs from A and is NOT what got signed.
C. device B cannot validate a credential issued to A (WRONG_MACHINE).
D. repeated redemption still returns already_redeemed.
E. the repeat does not duplicate or extend the Authorization.

Signing and verification are real: the test keypair signs, and the client-side
SusiVerifier calls susi_helper's Verify. Nothing is mocked. No private key
material is printed. No production key is touched.

Business rules are deliberately re-asserted (D, E) because the fix moved a value
inside the redemption transaction; the point is to show they did not move with it.

Run from license_server/:
    python test_phase_7_3_2_device_binding.py
"""

import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
APP_DIR = os.path.join(HERE, "app")

# The suite must not touch the developer's real license_server.db: it creates and
# redeems licenses, and redemption is irreversible. Point the app at a throwaway
# database BEFORE anything imports config/database.
_TMP_DIR = tempfile.mkdtemp(prefix="dlv_7_3_2_")
_DB_PATH = os.path.join(_TMP_DIR, "binding_test.db")
os.environ["DATABASE_URL"] = "sqlite:///" + _DB_PATH.replace("\\", "/")

for _p in (REPO, APP_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from schemas import LicenseActivate, LicenseCreate  # noqa: E402
from database import Base, SessionLocal, engine  # noqa: E402
import models  # noqa: E402,F401  (registers the tables on Base.metadata)
from models import Authorization, License  # noqa: E402

from services.license_service import LicenseService  # noqa: E402
from services.redemption_service import RedemptionService  # noqa: E402
from services.susi_security_service import SusiSecurityService  # noqa: E402
from test_key_loader import load_test_rsa_public_key  # noqa: E402

from utils.license_manager import LicenseManager, LicenseStatus  # noqa: E402
from utils.license_store import LicenseStore  # noqa: E402
from utils.susi_verifier import SusiVerifier  # noqa: E402

HELPER = os.path.join(
    REPO, "susi_helper", "target", "release",
    "susi_helper.exe" if os.name == "nt" else "susi_helper",
)
TEST_KEY_FILE = os.path.join(HERE, "test_rsa_key.pem")

#: Two synthetic devices. Shape matches what susi_helper GetMachineCode emits
#: (64 lowercase hex chars), so the values exercise the real code path.
DEVICE_A = "0123456789abcdef" * 4
DEVICE_B = "fedcba9876543210" * 4
#: Dedicated to test E so its "exactly one Authorization" assertion is scoped to
#: one device rather than counting rows left behind by the earlier tests.
DEVICE_C = "aabbccddeeff0011" * 4

RESULTS = []


def check(name, condition, detail=""):
    RESULTS.append((name, bool(condition), detail))
    print("%-4s %s%s" % ("PASS" if condition else "FAIL", name,
                         "" if condition or not detail else "  -- " + detail))


def _susi():
    """A real SusiSecurityService signing with the repository's TEST key."""
    return SusiSecurityService({
        "SUSI_HELPER_PATH": HELPER,
        "SUSI_DEVELOPMENT_PRIVATE_KEY_FILE": TEST_KEY_FILE,
        "SUSI_DEVELOPMENT_PRIVATE_KEY": "",
        "SUSI_DEVELOPMENT_PUBLIC_KEY": "",
    })


def _client_manager():
    """A real client LicenseManager wired to the TEST public key.

    This is the same class the shipped client uses, so _verify_artifact() runs the
    real sequence: structure -> signature (via susi_helper) -> machine binding ->
    expiry. The store is pointed at a temp dir so no real %APPDATA% file is read
    or written.
    """
    store = LicenseStore(data_dir=os.path.join(_TMP_DIR, "appdata"))
    verifier = SusiVerifier(
        public_key_pem=load_test_rsa_public_key(), helper_path=HELPER
    )
    return LicenseManager(
        server_url="http://127.0.0.1:1/api/v1", store=store, verifier=verifier
    )


def _signed_payload(signed_license: str) -> dict:
    """Parse license_data out of a SignedLicense JSON string."""
    return json.loads(json.loads(signed_license)["license_data"])


def _prereqs_ok():
    missing = [p for p in (HELPER, TEST_KEY_FILE) if not os.path.isfile(p)]
    if missing:
        for label in missing:
            print("      (missing prerequisite: %s)" % label)
        return False
    return True


Base.metadata.create_all(bind=engine)


# ----------------------------------------------------------------------
# A. Activation binds to the CLIENT device, and the client accepts it
# ----------------------------------------------------------------------
def test_a_binds_to_client_device():
    db = SessionLocal()
    try:
        license = LicenseService(db).create_license(
            LicenseCreate(duration_days=30), features=["ocr"]
        )
        license_id = license.id

        svc = RedemptionService(db, _susi())
        redeemed, response = svc.redeem_license(
            LicenseActivate(license_key=license.license_key, device_id=DEVICE_A)
        )

        signed = response.get("signed_license")
        check("A: activation returned a signed_license", bool(signed))
        if not signed:
            return

        payload = _signed_payload(signed)
        check("A: machine_codes is exactly [device A]",
              payload.get("machine_codes") == [DEVICE_A],
              "got %r" % (payload.get("machine_codes"),))

        # The Authorization the redemption created must name the same device.
        check("A: Authorization.device_id == device A",
              redeemed.authorization.device_id == DEVICE_A,
              "got %r" % (redeemed.authorization.device_id,))
        check("A: license.authorization_id points at that Authorization",
              license_id is not None
              and redeemed.authorization_id == redeemed.authorization.id)

        # The real client verification path, on the client's own device.
        result = _client_manager()._verify_artifact(signed, machine_code=DEVICE_A)
        check("A: client verification succeeds on device A",
              result.status is LicenseStatus.ACTIVATED,
              "%s: %s" % (result.status.value, result.message))
        check("A: client sees the server-signed expiry",
              result.expires_at is not None)
    finally:
        db.close()


# ----------------------------------------------------------------------
# B. The server's own machine code is not what was signed
# ----------------------------------------------------------------------
def test_b_server_code_not_used():
    db = SessionLocal()
    try:
        server_code = _susi().get_machine_code()
        check("B: this host's own machine code is not device A",
              server_code != DEVICE_A,
              "host fingerprint collided with the synthetic test device")

        license = LicenseService(db).create_license(
            LicenseCreate(duration_days=30), features=["ocr"]
        )
        svc = RedemptionService(db, _susi())
        _, response = svc.redeem_license(
            LicenseActivate(license_key=license.license_key, device_id=DEVICE_A)
        )

        payload = _signed_payload(response["signed_license"])
        bound = payload.get("machine_codes") or []
        check("B: server machine code is NOT in the signed binding",
              server_code not in bound,
              "the server's own fingerprint is still being signed")
        check("B: the signed binding is still exactly [device A]",
              bound == [DEVICE_A],
              "got %r" % (bound,))
    finally:
        db.close()


# ----------------------------------------------------------------------
# C. A different device cannot use device A's credential
# ----------------------------------------------------------------------
def test_c_other_device_rejected():
    db = SessionLocal()
    try:
        license = LicenseService(db).create_license(
            LicenseCreate(duration_days=30), features=["ocr"]
        )
        svc = RedemptionService(db, _susi())
        _, response = svc.redeem_license(
            LicenseActivate(license_key=license.license_key, device_id=DEVICE_A)
        )
        signed = response["signed_license"]

        manager = _client_manager()
        other = manager._verify_artifact(signed, machine_code=DEVICE_B)
        check("C: device B is rejected as WRONG_MACHINE",
              other.status is LicenseStatus.WRONG_MACHINE,
              "got %s: %s" % (other.status.value, other.message))

        owner = manager._verify_artifact(signed, machine_code=DEVICE_A)
        check("C: device A still verifies (control)",
              owner.status is LicenseStatus.ACTIVATED,
              "got %s: %s" % (owner.status.value, owner.message))
    finally:
        db.close()


# ----------------------------------------------------------------------
# D. One-time redemption semantics are unchanged
# ----------------------------------------------------------------------
def test_d_repeat_is_already_redeemed():
    db = SessionLocal()
    try:
        license = LicenseService(db).create_license(
            LicenseCreate(duration_days=30), features=["ocr"]
        )
        key = license.license_key
        svc = RedemptionService(db, _susi())

        first, _ = svc.redeem_license(
            LicenseActivate(license_key=key, device_id=DEVICE_A)
        )
        check("D: first redemption marks the license REDEEMED",
              first.state.value == "REDEEMED", first.state.value)

        for device in (DEVICE_A, DEVICE_B):
            raised = None
            try:
                svc.redeem_license(LicenseActivate(license_key=key, device_id=device))
            except ValueError as e:
                raised = e.args[0] if e.args else ""
            label = "same device" if device == DEVICE_A else "different device"
            check("D: repeat redemption (%s) => already_redeemed" % label,
                  raised == "already_redeemed",
                  "got %r" % (raised,))
    finally:
        db.close()


# ----------------------------------------------------------------------
# E. The repeat neither duplicates nor extends the Authorization
# ----------------------------------------------------------------------
def test_e_repeat_does_not_mutate():
    db = SessionLocal()
    try:
        license = LicenseService(db).create_license(
            LicenseCreate(duration_days=30), features=["ocr"]
        )
        key = license.license_key
        svc = RedemptionService(db, _susi())

        _, _ = svc.redeem_license(
            LicenseActivate(license_key=key, device_id=DEVICE_C)
        )

        def snapshot():
            """Everything the repeat could plausibly disturb, for device C only."""
            auths = db.query(Authorization).filter(
                Authorization.device_id == DEVICE_C
            ).all()
            lic = db.query(License).filter(License.key_hash == license.key_hash).one()
            return (
                [(a.id, a.expires_at, a.state.value) for a in auths],
                (lic.redeemed_at, lic.authorization_id, lic.state.value),
            )

        auths_before, lic_before = snapshot()
        check("E: first redemption created exactly one Authorization for device C",
              len(auths_before) == 1, "got %d" % len(auths_before))

        try:
            svc.redeem_license(LicenseActivate(license_key=key, device_id=DEVICE_C))
        except ValueError:
            pass  # already_redeemed; test D pins the reason

        db.expire_all()
        auths_after, lic_after = snapshot()

        check("E: no second Authorization was created for device C",
              len(auths_after) == 1, "got %d" % len(auths_after))
        check("E: Authorization row is byte-for-byte unchanged",
              auths_before == auths_after,
              "%r -> %r" % (auths_before, auths_after))
        check("E: expiry was NOT extended",
              [a[1] for a in auths_before] == [a[1] for a in auths_after])
        check("E: redeemed_at / authorization_id / state unchanged",
              lic_before == lic_after,
              "%r -> %r" % (lic_before, lic_after))
        check("E: license is still REDEEMED",
              lic_after[2] == "REDEEMED", lic_after[2])
        check("E: device B never acquired an Authorization",
              db.query(Authorization).filter(
                  Authorization.device_id == DEVICE_B
              ).count() == 0)
    finally:
        db.close()


def main():
    print(__doc__.strip().splitlines()[0])
    print("=" * 70)
    print("database: %s" % _DB_PATH)

    if not _prereqs_ok():
        print("FATAL: susi_helper or the test key is missing; cannot sign.")
        return 1

    test_a_binds_to_client_device()
    test_b_server_code_not_used()
    test_c_other_device_rejected()
    test_d_repeat_is_already_redeemed()
    test_e_repeat_does_not_mutate()

    print("=" * 70)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = len(RESULTS) - passed
    print("%d passed, %d failed, %d total" % (passed, failed, len(RESULTS)))
    print("NOTE: no private key material was printed by this test.")

    shutil.rmtree(_TMP_DIR, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
