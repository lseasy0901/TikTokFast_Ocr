#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 7.2-6.5: Real Client License Activation - acceptance tests

These tests exercise the REAL activation path end to end:
    client LICENSE MANAGER
      -> real HTTP request (requests) to a real uvicorn fastapi server
      -> real SQLAlchemy business layer (one-time redemption)
      -> real susi_helper.exe signing (no fake Susi APIs)
      -> real client-side local verification (susi_helper.exe + public key)
      -> real local persistence on disk

The only things substituted are the deployment locations:
    - the server runs on a free localhost port against a temporary SQLite file
    - the client store points at a temporary directory

Both the private key used for signing and the public key used for
verification come from the existing test keypair (license_server/test_rsa_key.pem).
No production key material is involved.

Run:  python test_phase_7_2_6_5.py
"""

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LICENSE_SERVER_DIR = os.path.join(PROJECT_ROOT, "license_server")
SERVER_APP_DIR = os.path.join(LICENSE_SERVER_DIR, "app")

sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, LICENSE_SERVER_DIR)

from test_key_loader import load_test_rsa_key  # noqa: E402

from utils.license_client import LicenseServerClient  # noqa: E402
from utils.license_manager import (  # noqa: E402
    LicenseManager,
    LicenseStatus,
)
from utils.license_store import LicenseStore  # noqa: E402
from utils.susi_verifier import SusiVerifier, default_helper_path  # noqa: E402

PRIVATE_KEY_PEM = load_test_rsa_key()

# Public key matching test_rsa_key.pem, as pinned by test_phase_7_2_6.py.
PUBLIC_KEY_PEM = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEApniRTJwG5l5fBX0LqvGw\n"
    "YGqLmq4TwGm+FwBBV8dvr+DcKyPIuksfbTyYoznMKcc7EUtCvuiiaBvi/X5Ef2fi\n"
    "sZi2ENBN2TLDJhthuwZ7K4xrVoJ2U3IscaySz1C2I1iYY/cx+d+uAAAR2wG65+Tb\n"
    "FdlJS5ny7njiL91ZAkXHx4VpL4qnq5ctMFG6lv5dBgg4xu53LtEDFClBs/iubJpa\n"
    "P/rcnSgfvhopudluKt9TmPpndWHYW7PCVfrurOYmNUBnaokRcfGRrFiAn+lxx/kJ\n"
    "SkfZzm9p0Rhmzr4nAQRSVl+EkA4i9X04BV7QGwWprl6wmafpA9NLd+/pPXDYnOab\n"
    "3QIDAQAB\n"
    "-----END PUBLIC KEY-----"
)

ADMIN_API_KEY = "phase-7-2-6-5-admin-key"
API_PREFIX = "/api/v1"

_PASSED = 0
_FAILED = 0


# ======================================================================
# Test harness helpers
# ======================================================================
def check(condition: bool, label: str) -> bool:
    global _PASSED, _FAILED
    if condition:
        _PASSED += 1
        print(f"    [ok]   {label}")
    else:
        _FAILED += 1
        print(f"    [FAIL] {label}")
    return condition


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ServerFixture:
    """Runs the real license server (uvicorn) against a temporary database."""

    def __init__(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="dlv_7265_")
        self.db_path = os.path.join(self.tmp_dir, "license.db").replace("\\", "/")
        self.log_path = os.path.join(self.tmp_dir, "server.log")
        self.port = free_port()
        self.base_url = f"http://127.0.0.1:{self.port}{API_PREFIX}"
        self.proc = None
        self._log = None

    def start(self):
        env = dict(os.environ)
        env.update(
            {
                "DATABASE_URL": f"sqlite:///{self.db_path}",
                "ADMIN_API_KEY": ADMIN_API_KEY,
                "API_PREFIX": API_PREFIX,
                "SUSI_DEVELOPMENT_PRIVATE_KEY": PRIVATE_KEY_PEM,
                "SUSI_DEVELOPMENT_PUBLIC_KEY": PUBLIC_KEY_PEM,
                "PYTHONIOENCODING": "utf-8",
            }
        )
        self._log = open(self.log_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [
                sys.executable, "-m", "uvicorn", "main:app",
                "--host", "127.0.0.1", "--port", str(self.port),
            ],
            cwd=SERVER_APP_DIR,
            env=env,
            stdout=self._log,
            stderr=subprocess.STDOUT,
        )

        deadline = time.time() + 45
        while time.time() < deadline:
            try:
                if requests.get(f"http://127.0.0.1:{self.port}/", timeout=1).status_code == 200:
                    return
            except requests.RequestException:
                time.sleep(0.4)
        raise RuntimeError(f"license server did not start; see {self.log_path}")

    def stop(self):
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self._log is not None:
            self._log.close()

    def cleanup(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    # -- direct DB access, for server-side state the HTTP API does not expose --
    def sql(self, statement, params=()):
        conn = sqlite3.connect(self.db_path)
        try:
            cur = conn.execute(statement, params)
            rows = cur.fetchall()
            conn.commit()
            return rows
        finally:
            conn.close()

    def revoke_license(self, license_id: int):
        self.sql("UPDATE licenses SET state='REVOKED' WHERE id=?", (license_id,))

    def create_license(self, duration_days: int = 30) -> dict:
        r = requests.post(
            f"{self.base_url}/admin/licenses",
            json={"duration_days": duration_days},
            headers={"X-API-Key": ADMIN_API_KEY},
            timeout=20,
        )
        r.raise_for_status()
        return r.json()


def make_manager(server, store_dir: str, base_url: str | None = None) -> LicenseManager:
    """Build a real client LicenseManager pointed at the real server."""
    url = base_url or server.base_url
    return LicenseManager(
        server_url=url,
        store=LicenseStore(data_dir=store_dir),
        verifier=SusiVerifier(public_key_pem=PUBLIC_KEY_PEM),
        client=LicenseServerClient(base_url=url, timeout=20),
    )


def sign_payload(payload: dict) -> str:
    """Sign a LicensePayload with the REAL susi_helper (no fake Susi APIs).

    Used only to construct artifacts the server would not naturally issue
    (expired / bound to another machine).
    """
    result = subprocess.run(
        [default_helper_path()],
        input=json.dumps(
            {"command": "SignLicense", "private_key_pem": PRIVATE_KEY_PEM, "payload": payload}
        ),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=30,
    )
    outer = json.loads(result.stdout)
    if "Success" not in outer:
        raise RuntimeError(f"signing failed: {result.stdout[:200]}")
    return outer["Success"]["data"]


def machine_code() -> str:
    return SusiVerifier(public_key_pem=PUBLIC_KEY_PEM).get_machine_code()


def now_utc():
    return datetime.now(timezone.utc).replace(microsecond=0)


def rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


# ======================================================================
# Tests
# ======================================================================
def test_1_valid_unused_key_succeeds(server, store_dir):
    """Case 1: a valid, unused key activates successfully through the real path."""
    print("\n[1] Valid unused key succeeds (real HTTP client -> server -> Susi signed license)")
    lic = server.create_license(duration_days=30)
    key = lic["license_key"]
    check(bool(key), "admin API issued a plaintext license key")

    manager = make_manager(server, store_dir)
    result = manager.activate(key)

    check(result.status is LicenseStatus.ACTIVATED, f"client reports ACTIVATED (got {result.status.value}: {result.message})")
    check(result.expires_at is not None, "signed artifact carries an expiry")
    if result.expires_at is not None:
        expected = now_utc() + timedelta(days=30)
        drift = abs((result.expires_at - expected).total_seconds())
        check(drift < 300, f"expiry is server-issued ~30 days out (drift {drift:.0f}s)")
    check(os.path.isfile(os.path.join(store_dir, "license.json")), "license persisted to disk")

    record = LicenseStore(data_dir=store_dir).load()
    check(record is not None, "stored license reloads")
    check("PRIVATE" not in json.dumps(record), "stored license contains no private key material")
    check(key not in json.dumps(record), "stored license does not contain the plaintext key")

    # Server-side truth: license is REDEEMED and bound to this device.
    rows = server.sql("SELECT state, authorization_id FROM licenses WHERE id=?", (lic["id"],))
    check(rows and rows[0][0] == "REDEEMED", "server marked the license REDEEMED")
    check(rows and rows[0][1] is not None, "server attached an authorization")
    return manager, key


def test_2_same_key_rejected(server, store_dir, manager, key):
    """Case 2: reusing the same key is rejected by the business layer."""
    print("\n[2] Reusing the same key is rejected as already redeemed")
    before = LicenseStore(data_dir=store_dir).load()["signed_license"]

    result = manager.activate(key)

    check(result.status is LicenseStatus.INVALID, f"second redemption rejected (got {result.status.value})")
    check("已被使用" in result.message, f"reason surfaces as already-redeemed ('{result.message}')")
    after = LicenseStore(data_dir=store_dir).load()["signed_license"]
    check(before == after, "local license state untouched by the failed retry")

    # The stored license still verifies.
    reread = make_manager(server, store_dir).load_stored()
    check(reread.status is LicenseStatus.ACTIVATED, "existing license still valid afterwards")


def test_3_invalid_key_fails_safely(server, store_dir):
    """Case 3: an unknown key fails safely and writes nothing."""
    print("\n[3] Invalid key fails safely")
    fresh_dir = os.path.join(store_dir, "case3")
    manager = make_manager(server, fresh_dir)

    result = manager.activate("this-key-does-not-exist-at-all-0000")

    check(result.status is LicenseStatus.INVALID, f"unknown key rejected (got {result.status.value})")
    check("无效" in result.message, f"reason surfaces as invalid ('{result.message}')")
    check(not os.path.isfile(os.path.join(fresh_dir, "license.json")), "nothing written to local store")


def test_4_revoked_key_fails_safely(server, store_dir):
    """Case 4: a revoked key fails safely."""
    print("\n[4] Revoked key fails safely")
    lic = server.create_license(duration_days=30)
    server.revoke_license(lic["id"])

    fresh_dir = os.path.join(store_dir, "case4")
    manager = make_manager(server, fresh_dir)
    result = manager.activate(lic["license_key"])

    check(result.status is LicenseStatus.INVALID, f"revoked key rejected (got {result.status.value})")
    check("吊销" in result.message, f"reason surfaces as revoked ('{result.message}')")
    check(not os.path.isfile(os.path.join(fresh_dir, "license.json")), "nothing written to local store")


def test_5_survives_restart(server, store_dir, key):
    """Case 5: after a restart, the stored license is still active on the same machine."""
    print("\n[5] Valid license remains activated after restart")
    # A brand new manager with a fresh in-memory state == process restart.
    restarted = make_manager(server, store_dir)
    result = restarted.load_stored()

    check(result.status is LicenseStatus.ACTIVATED, f"stored license reloads as ACTIVATED (got {result.status.value})")
    check(result.expires_at is not None, "expiry recovered from the signed artifact")

    # And the expiry comes from the artifact, not from any local duration field.
    record = json.loads(open(os.path.join(store_dir, "license.json"), encoding="utf-8").read())
    payload = json.loads(json.loads(record["signed_license"])["license_data"])
    check(
        result.expires_at.isoformat().replace("+00:00", "Z") == payload["expires"].replace("+00:00", "Z"),
        "client expiry == server-signed expires field (no local duration computation)",
    )


def test_6_tampered_stored_license_rejected(server, store_dir):
    """Case 6: a tampered stored license fails local signature verification."""
    print("\n[6] Tampered stored license fails local verification")
    tamper_dir = os.path.join(store_dir, "case6")
    os.makedirs(tamper_dir, exist_ok=True)

    lic = server.create_license(duration_days=30)
    manager = make_manager(server, tamper_dir)
    check(manager.activate(lic["license_key"]).is_activated, "baseline activation succeeded")

    path = os.path.join(tamper_dir, "license.json")
    record = json.loads(open(path, encoding="utf-8").read())

    # Extend the expiry inside the signed payload, keeping the original signature.
    outer = json.loads(record["signed_license"])
    payload = json.loads(outer["license_data"])
    payload["expires"] = rfc3339(now_utc() + timedelta(days=3650))
    outer["license_data"] = json.dumps(payload)
    record["signed_license"] = json.dumps(outer)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f)

    result = make_manager(server, tamper_dir).load_stored()
    check(result.status is LicenseStatus.INVALID, f"tampered artifact rejected (got {result.status.value})")
    check("篡改" in result.message or "签名" in result.message, f"reason mentions signature ('{result.message}')")


def test_7_expired_license(server, store_dir):
    """Case 7: a stored, correctly-signed but expired license reports as expired."""
    print("\n[7] Expired stored license reports inactive/expired")
    expired_dir = os.path.join(store_dir, "case7")
    os.makedirs(expired_dir, exist_ok=True)

    code = machine_code()
    payload = {
        "id": "expired-1",
        "product": "DouyinLowLatencyViewer",
        "customer": "DouyinLowLatencyViewer",
        "license_key": "0" * 64,
        "created": rfc3339(now_utc() - timedelta(days=60)),
        "expires": rfc3339(now_utc() - timedelta(days=1)),
        "features": [],
        "machine_codes": [code],
        "lease_expires": None,
        "lease_grace_period": None,
        "require_signed_binary": False,
    }
    signed = sign_payload(payload)

    store = LicenseStore(data_dir=expired_dir)
    store.save(signed, code, server.base_url)

    result = make_manager(server, expired_dir).load_stored()
    check(result.status is LicenseStatus.EXPIRED, f"expired artifact reports EXPIRED (got {result.status.value})")
    check(result.expires_at is not None and result.expires_at < now_utc(), "expiry parsed from artifact")


def test_8_wrong_machine(server, store_dir):
    """Case 8: a correctly-signed license bound to another machine is rejected."""
    print("\n[8] Wrong-machine license fails local verification")
    wrong_dir = os.path.join(store_dir, "case8")
    os.makedirs(wrong_dir, exist_ok=True)

    payload = {
        "id": "other-machine",
        "product": "DouyinLowLatencyViewer",
        "customer": "DouyinLowLatencyViewer",
        "license_key": "1" * 64,
        "created": rfc3339(now_utc()),
        "expires": rfc3339(now_utc() + timedelta(days=30)),
        "features": [],
        "machine_codes": ["deadbeef" * 8],  # some other device
        "lease_expires": None,
        "lease_grace_period": None,
        "require_signed_binary": False,
    }
    signed = sign_payload(payload)

    store = LicenseStore(data_dir=wrong_dir)
    store.save(signed, "someone-elses-machine", server.base_url)

    result = make_manager(server, wrong_dir).load_stored()
    check(result.status is LicenseStatus.WRONG_MACHINE, f"foreign-machine artifact rejected (got {result.status.value})")
    check("设备" in result.message, f"reason mentions the device ('{result.message}')")


def test_9_concurrent_activation_blocked(server, store_dir):
    """Case 9: two simultaneous activations cannot both proceed."""
    print("\n[9] Concurrent activation is blocked")
    lic = server.create_license(duration_days=30)
    key = lic["license_key"]

    conc_dir = os.path.join(store_dir, "case9")
    manager = make_manager(server, conc_dir)

    barrier = threading.Barrier(2)
    results = []
    lock = threading.Lock()

    def worker():
        barrier.wait(timeout=10)
        r = manager.activate(key)
        with lock:
            results.append(r)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    check(len(results) == 2, "both concurrent calls returned")
    activated = [r for r in results if r.is_activated]
    blocked = [r for r in results if not r.is_activated]
    check(len(activated) == 1, f"exactly one activation succeeded (got {len(activated)})")
    check(len(blocked) == 1, f"the other was blocked (got {len(blocked)})")
    if blocked:
        check(
            "正在激活" in blocked[0].message or "已被使用" in blocked[0].message,
            f"blocked reason is coherent ('{blocked[0].message}')",
        )

    rows = server.sql("SELECT COUNT(*) FROM licenses WHERE id=? AND state='REDEEMED'", (lic["id"],))
    check(rows[0][0] == 1, "license redeemed exactly once server-side")

    # Deterministic guard check: with the lock held, a further call is refused
    # outright and never reaches the server.
    guard_dir = os.path.join(store_dir, "case9b")
    guard = make_manager(server, guard_dir)
    guard._activation_lock.acquire()
    try:
        r = guard.activate(key)
        check(not r.is_activated and "正在激活" in r.message, f"in-flight guard refuses a second call ('{r.message}')")
    finally:
        guard._activation_lock.release()


def test_10_server_unreachable_preserves_state(server, store_dir):
    """Case 10: an unreachable server fails gracefully and corrupts nothing."""
    print("\n[10] Server unreachable -> graceful failure, local state intact")
    offline_dir = os.path.join(store_dir, "case10")
    os.makedirs(offline_dir, exist_ok=True)

    # Seed the local store with a genuinely valid license first (fresh key).
    seed_key = server.create_license(duration_days=30)["license_key"]
    seeded = make_manager(server, offline_dir)
    check(seeded.activate(seed_key).is_activated, "seeded a valid license locally")
    before = open(os.path.join(offline_dir, "license.json"), encoding="utf-8").read()

    # Now point a client at a dead port.
    dead_port = free_port()
    offline_url = f"http://127.0.0.1:{dead_port}{API_PREFIX}"
    offline = make_manager(server, offline_dir, base_url=offline_url)
    result = offline.activate("some-brand-new-key-0000")

    check(result.status is LicenseStatus.ACTIVATION_FAILED, f"reported as activation failure (got {result.status.value})")
    check("无法连接" in result.message, f"reason is a connectivity failure ('{result.message}')")
    after = open(os.path.join(offline_dir, "license.json"), encoding="utf-8").read()
    check(before == after, "stored license byte-identical (not corrupted)")

    # And the pre-existing license still verifies against the real server.
    recovered = make_manager(server, offline_dir).load_stored()
    check(recovered.status is LicenseStatus.ACTIVATED, "pre-existing license still ACTIVATED")


def test_12_duration_semantics(store_dir):
    """Business rule guard: first redemption grants exactly one duration, and
    accumulation / restart-on-expiry still work.

    Guards the fix for the first-redemption double-count: the fix must not
    disable accumulation for genuinely pre-existing authorizations.

    Uses its OWN server + database, because the device identity is the real
    machine fingerprint and the shared server accumulates authorizations across
    the other cases -- there would be no "brand new device" left.
    """
    print("\n[12] Duration rules: no double-count, accumulation preserved")

    def seconds_out(res) -> float:
        assert res.expires_at is not None, "expected an expiry"
        return (res.expires_at - now_utc()).total_seconds()

    def near(actual: float, expected_days: float, label: str):
        drift = abs(actual - expected_days * 86400)
        check(drift < 300, f"{label} (~{expected_days:.0f}d, drift {drift:.0f}s)")

    fresh = ServerFixture()
    try:
        fresh.start()
        dev_dir = os.path.join(store_dir, "case12")
        device = machine_code()

        # --- first redemption on a brand-new device: exactly 1x duration ---
        k1 = fresh.create_license(duration_days=30)["license_key"]
        r1 = make_manager(fresh, dev_dir).activate(k1)
        check(r1.is_activated, "first key activated")
        near(seconds_out(r1), 30, "first redemption grants exactly 30 days")

        # --- second key on the SAME device: accumulation (30 -> 60) ---
        k2 = fresh.create_license(duration_days=30)["license_key"]
        r2 = make_manager(fresh, dev_dir).activate(k2)
        check(r2.is_activated, "second key activated")
        near(seconds_out(r2), 60, "second key accumulates onto the existing authorization")

        # --- expired authorization: restart from server time, not 2x ---
        fresh.sql(
            "UPDATE authorizations SET expires_at=?, state='EXPIRED' WHERE device_id=?",
            (rfc3339(now_utc() - timedelta(days=5)), device),
        )
        k3 = fresh.create_license(duration_days=30)["license_key"]
        r3 = make_manager(fresh, dev_dir).activate(k3)
        check(r3.is_activated, "expired authorization reactivates")
        near(seconds_out(r3), 30, "expired authorization restarts at exactly 30 days from now")

        rows = fresh.sql("SELECT COUNT(*) FROM authorizations WHERE device_id=?", (device,))
        check(rows[0][0] == 1, "still a single authorization row per device")

        # --- the client's expiry is the server-signed one ---
        record = json.loads(
            open(os.path.join(dev_dir, "license.json"), encoding="utf-8").read()
        )
        payload = json.loads(json.loads(record["signed_license"])["license_data"])
        check(
            abs(
                _parse(payload["expires"]) - r3.expires_at
            ).total_seconds() < 2,
            "stored artifact expiry == client-reported expiry",
        )
    finally:
        fresh.stop()
        fresh.cleanup()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def test_11_no_private_key_on_client(server, store_dir):
    """Security: the client never needs, stores, or receives private key material."""
    print("\n[11] Client-side security properties")
    client_sources = [
        os.path.join(PROJECT_ROOT, "utils", "license_manager.py"),
        os.path.join(PROJECT_ROOT, "utils", "license_client.py"),
        os.path.join(PROJECT_ROOT, "utils", "license_store.py"),
        os.path.join(PROJECT_ROOT, "utils", "susi_verifier.py"),
    ]
    for path in client_sources:
        text = open(path, encoding="utf-8").read()
        check("PRIVATE KEY" not in text, f"{os.path.basename(path)} holds no private key")

    record = LicenseStore(data_dir=store_dir).load()
    blob = json.dumps(record)
    check("PRIVATE KEY" not in blob, "stored record contains no private key")
    check(PRIVATE_KEY_PEM.strip() not in blob, "stored record does not match the test private key")


# ======================================================================
# Runner
# ======================================================================
def main() -> int:
    if not os.path.isfile(default_helper_path()):
        print(f"FATAL: susi_helper not built at {default_helper_path()}")
        return 1

    server = ServerFixture()
    store_root = tempfile.mkdtemp(prefix="dlv_7265_store_")
    print("=" * 70)
    print("Phase 7.2-6.5: Real Client License Activation")
    print("=" * 70)
    print(f"server:  {server.base_url}")
    print(f"helper:  {default_helper_path()}")
    print(f"store:   {store_root}")

    try:
        server.start()
        print("server is up\n")

        manager, key = test_1_valid_unused_key_succeeds(server, store_root)
        test_2_same_key_rejected(server, store_root, manager, key)
        test_3_invalid_key_fails_safely(server, store_root)
        test_4_revoked_key_fails_safely(server, store_root)
        test_5_survives_restart(server, store_root, key)
        test_6_tampered_stored_license_rejected(server, store_root)
        test_7_expired_license(server, store_root)
        test_8_wrong_machine(server, store_root)
        test_9_concurrent_activation_blocked(server, store_root)
        test_10_server_unreachable_preserves_state(server, store_root)
        test_11_no_private_key_on_client(server, store_root)
        test_12_duration_semantics(store_root)
    except Exception as e:
        global _FAILED
        _FAILED += 1
        print(f"\nEXCEPTION: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
    finally:
        server.stop()
        print("\n--- server log (tail) ---")
        try:
            with open(server.log_path, encoding="utf-8") as f:
                print("".join(f.readlines()[-12:]))
        except OSError:
            pass
        server.cleanup()
        shutil.rmtree(store_root, ignore_errors=True)

    total = _PASSED + _FAILED
    print("=" * 70)
    print(f"RESULT: {_PASSED}/{total} checks passed, {_FAILED} failed")
    print("=" * 70)
    return 0 if _FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
