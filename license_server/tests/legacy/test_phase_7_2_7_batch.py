# -*- coding: utf-8 -*-
"""
Phase 7.2-7 (batch generation addendum) - focused tests.

Covers the 15 required cases for batch License Key generation, plus the explicit
security checks. Everything runs against THROWAWAY SQLite databases created in a
temp directory; the development database is never touched.

Run from license_server/:

    python test_phase_7_2_7_batch.py

Case 12 (transaction rollback) is exercised in-process, because a mid-batch
failure cannot be triggered over HTTP without patching the running server.
All other cases go through the real HTTP endpoints of a real uvicorn server.
"""

import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import hashlib

HERE = pathlib.Path(__file__).resolve().parent
PORT = 8131
BASE = "http://127.0.0.1:%d" % PORT

TMPDIR = pathlib.Path(tempfile.mkdtemp(prefix="dlv-batch-test-"))
HTTP_DB = (TMPDIR / "http.db").as_posix()
UNIT_DB = (TMPDIR / "unit.db").as_posix()

PASSED = 0
FAILED = 0
SKIPPED = 0

#: Redemption spawns susi_helper.exe twice and signs for real; allow headroom.
REDEEM_TIMEOUT = 60


def check(cond, msg):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print("  [ok]   " + msg, flush=True)
    else:
        FAILED += 1
        print("  [FAIL] " + msg, flush=True)
    return bool(cond)


def skip(msg):
    """Report a case that could not be exercised. Never counted as a pass."""
    global SKIPPED
    SKIPPED += 1
    print("  [skip] " + msg, flush=True)


def keys_from(html):
    """Extract the plaintext keys from the generate-keys result panel."""
    m = re.search(r'<textarea[^>]*id="generated-keys"[^>]*>(.*?)</textarea>', html, re.S)
    if not m:
        return []
    return [k.strip() for k in m.group(1).splitlines() if k.strip()]


# ======================================================================
# Case 12 - transaction rollback (in-process, own database)
# ======================================================================
def part_b_rollback():
    print("\n[12] atomicity: rollback leaves zero partial rows (in-process)")

    os.environ["DATABASE_URL"] = "sqlite:///" + UNIT_DB
    sys.path.insert(0, str(HERE / "app"))

    import database  # noqa: E402  (imports models for table registration)
    from schemas import LicenseCreate  # noqa: E402
    from services import license_service  # noqa: E402
    from services.license_service import LicenseService, generate_secure_key  # noqa: E402

    database.Base.metadata.create_all(bind=database.engine)

    def license_count():
        return sqlite3.connect(UNIT_DB).execute(
            "select count(*) from licenses").fetchone()[0]

    # 12a - commit=False defers the write; an explicit rollback discards the batch.
    db = database.SessionLocal()
    svc = LicenseService(db)
    for _ in range(5):
        svc.create_license(LicenseCreate(duration_days=30), commit=False)
    check(license_count() == 0, "commit=False writes nothing before the batch commits")
    db.rollback()
    db.close()
    check(license_count() == 0, "rollback discarded the entire batch (0 rows)")

    # 12b - force a real failure mid-batch (duplicate key_hash) and confirm that
    # the rows already flushed earlier in the same transaction are also gone.
    original = license_service.generate_secure_key
    calls = {"n": 0}

    def colliding_key():
        calls["n"] += 1
        return "BATCH-COLLISION-PROBE" if calls["n"] >= 3 else original()

    license_service.generate_secure_key = colliding_key
    raised = None
    db = database.SessionLocal()
    try:
        svc = LicenseService(db)
        for _ in range(5):
            svc.create_license(LicenseCreate(duration_days=30), commit=False)
        db.commit()
    except Exception as exc:  # noqa: BLE001 - the failure IS the assertion
        raised = exc
        db.rollback()
    finally:
        db.close()
        license_service.generate_secure_key = original

    check(raised is not None,
          "a duplicate key_hash aborts the batch (%s)" % type(raised).__name__)
    check(license_count() == 0,
          "no partial batch left in the database after the failed transaction")


# ======================================================================
# HTTP suite
# ======================================================================
def part_a_http():
    import requests

    admin_key = None
    for line in (HERE / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("ADMIN_API_KEY="):
            admin_key = line.split("=", 1)[1].strip()
    if not admin_key:
        print("ADMIN_API_KEY not found in license_server/.env")
        sys.exit(2)

    env = dict(os.environ)
    env["DATABASE_URL"] = "sqlite:///" + HTTP_DB
    env["PYTHONIOENCODING"] = "utf-8"

    # A successful redemption requires the development signing key. It is NOT in
    # .env, so supply it from the local gitignored dev key when the environment
    # does not already provide one. Without a key, redemption cannot succeed and
    # the redemption cases are reported as SKIPPED rather than passed.
    signing_key_available = bool(env.get("SUSI_DEVELOPMENT_PRIVATE_KEY"))
    dev_key_file = HERE / "test_rsa_key.pem"
    if not signing_key_available and dev_key_file.exists():
        env["SUSI_DEVELOPMENT_PRIVATE_KEY"] = dev_key_file.read_text()
        signing_key_available = True
    print("development signing key available for redemption cases: %s"
          % signing_key_available)

    launch = (
        "import sys; sys.path.insert(0,'app'); import uvicorn; "
        "uvicorn.run('main:app', host='127.0.0.1', port=%d, log_level='warning')" % PORT
    )
    proc = subprocess.Popen([sys.executable, "-c", launch], cwd=str(HERE), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    all_plaintext = []

    try:
        s = requests.Session()
        ready = False
        for _ in range(160):
            try:
                if s.get(BASE + "/", timeout=1).status_code == 200:
                    ready = True
                    break
            except Exception:
                pass
            if proc.poll() is not None:
                print("SERVER DIED:\n" + proc.stdout.read())
                sys.exit(2)
            time.sleep(0.25)
        if not ready:
            print("server never came up")
            sys.exit(2)

        s.post(BASE + "/admin/login", data={"username": "admin", "password": admin_key})
        con = sqlite3.connect(HTTP_DB)

        def rows():
            return con.execute(
                "select id,key_hash,duration_days,state,features,authorization_id "
                "from licenses order by id").fetchall()

        def count_licenses():
            return con.execute("select count(*) from licenses").fetchone()[0]

        def count_auths():
            return con.execute("select count(*) from authorizations").fetchone()[0]

        def post(days, qty, features=""):
            return s.post(BASE + "/admin/generate-keys",
                          data={"duration_days": str(days),
                                "quantity": str(qty),
                                "features": features})

        # ---------------- 1. quantity=1 -> exactly 1 key
        print("\n[1] quantity=1 -> exactly 1 key")
        before = count_licenses()
        r = post(30, 1)
        got = keys_from(r.text)
        check(r.status_code == 200, "POST -> 200 (got %s)" % r.status_code)
        check(len(got) == 1, "exactly 1 plaintext key shown (got %d)" % len(got))
        check(count_licenses() == before + 1,
              "exactly 1 new database row (got %d)" % (count_licenses() - before))
        all_plaintext.extend(got)

        # ---------------- 13. existing single-key generation still works
        print("\n[13] single-key generation still works end to end")
        r = post(30, 1)
        single = keys_from(r.text)
        check(len(single) == 1, "single generation returns a usable plaintext key")
        if single and signing_key_available:
            all_plaintext.extend(single)
            act = requests.post(BASE + "/api/v1/licenses/activate",
                                json={"license_key": single[0], "device_id": "1" * 64},
                                timeout=REDEEM_TIMEOUT)
            check(act.status_code == 200,
                  "the single-generated key redeems successfully (got %s %s)"
                  % (act.status_code, act.text[:100]))
        elif single:
            skip("single-key redemption (no development signing key available)")

        # ---------------- 2 & 3 & 4 & 5 & 6. quantity=10
        print("\n[2] quantity=10 -> exactly 10 keys")
        auths_before = count_auths()
        before = count_licenses()
        r = post(180, 10)
        ten = keys_from(r.text)
        check(len(ten) == 10, "exactly 10 plaintext keys shown (got %d)" % len(ten))
        new_rows = rows()[before:]
        check(len(new_rows) == 10, "exactly 10 new database rows (got %d)" % len(new_rows))
        all_plaintext.extend(ten)

        print("\n[3] all 10 keys are unique")
        check(len(set(ten)) == 10, "10 distinct plaintext keys (got %d)" % len(set(ten)))
        check(len(set(r[1] for r in new_rows)) == 10,
              "10 distinct key_hash values (got %d)" % len(set(x[1] for x in new_rows)))

        print("\n[4] rows contain hashes only")
        matches = sum(1 for pk, row in zip(ten, new_rows)
                      if row[1] == hashlib.sha256(pk.encode()).hexdigest())
        check(matches == 10, "each stored key_hash == sha256(of its plaintext) (%d/10)" % matches)
        blob = "".join(str(c) for row in new_rows for c in row)
        check(not any(pk in blob for pk in ten),
              "no plaintext key appears anywhere in its row")

        print("\n[5] all 10 rows use the selected duration")
        check(all(row[2] == 180 for row in new_rows),
              "all rows duration_days == 180 (got %s)" % sorted({row[2] for row in new_rows}))

        print("\n[6] all 10 rows start as UNUSED")
        check(all(row[3] == "UNUSED" for row in new_rows),
              "all rows state == UNUSED (got %s)" % sorted({row[3] for row in new_rows}))

        print("\n[security] batch generation creates no Authorization")
        check(count_auths() == auths_before,
              "authorizations count unchanged (%d -> %d)" % (auths_before, count_auths()))
        check(all(row[5] is None for row in new_rows),
              "every new license has authorization_id NULL")

        # ---------------- 7-10. quantity validation
        print("\n[7-10] quantity validation, no rows written")
        n = count_licenses()
        for bad, label in [("0", "7: quantity=0"), ("-1", "8: negative quantity"),
                           ("abc", "9: non-integer quantity"),
                           ("1001", "10: quantity above the V1 limit")]:
            r = post(30, bad)
            check(r.status_code == 200 and "数量必须是" in r.text or "单次最多" in r.text,
                  "%s rejected with a message" % label)
        r = post(30, "")
        check(r.status_code == 200 and "数量必须是" in r.text,
              "empty quantity rejected")
        check(count_licenses() == n,
              "no rows written by any invalid quantity (count=%d)" % count_licenses())

        # ---------------- 11. invalid duration
        print("\n[11] invalid duration rejected in batch mode")
        n = count_licenses()
        for bad in ["0", "45", "400", "abc", ""]:
            r = post(bad, 3)
            check(r.status_code == 200 and "请选择有效期" in r.text,
                  "duration_days=%r rejected with quantity=3" % bad)
        check(count_licenses() == n,
              "no rows written by any invalid duration (count=%d)" % count_licenses())

        # ---------------- limit boundary, both sides
        print("\n[limit boundary]")
        r = post(1, 1000)
        big = keys_from(r.text)
        check(len(big) == 1000, "quantity=1000 (the V1 maximum) is accepted (got %d)" % len(big))
        all_plaintext.extend(big)
        check(len(set(big)) == 1000, "all 1000 keys are distinct")
        check(all(len(k) == 43 for k in big), "every key uses the existing key format")

        # ---------------- 14. revoke still works
        print("\n[14] existing revoke behaviour still works")
        r = post(30, 2)
        pair = keys_from(r.text)
        all_plaintext.extend(pair)
        ids = con.execute("select id from licenses order by id desc limit 2").fetchall()
        target = ids[-1][0]
        r = s.get("%s/admin/license/action/revoke?pks=%s" % (BASE, target),
                  allow_redirects=False)
        check(r.status_code == 303, "revoke action invoked (got %s)" % r.status_code)
        st = con.execute("select state from licenses where id=?", (target,)).fetchone()[0]
        check(st == "REVOKED", "revoked batch-generated key -> REVOKED (got %s)" % st)

        # ---------------- 15. redemption unchanged
        print("\n[15] existing redemption behaviour unchanged")
        r = post(30, 2)
        redeem_pair = keys_from(r.text)
        all_plaintext.extend(redeem_pair)
        # Look the redeemed key up by its hash: keys_from() returns generation
        # order, which is not necessarily descending id order.
        fresh_hash = hashlib.sha256(redeem_pair[0].encode()).hexdigest() if redeem_pair else None
        if signing_key_available and fresh_hash:
            act = requests.post(BASE + "/api/v1/licenses/activate",
                                json={"license_key": redeem_pair[0],
                                      "device_id": "2" * 64},
                                timeout=REDEEM_TIMEOUT)
            check(act.status_code == 200,
                  "a fresh batch-generated key redeems (got %s %s)"
                  % (act.status_code, act.text[:120]))
            st = con.execute("select state from licenses where key_hash=?",
                             (fresh_hash,)).fetchone()[0]
            check(st == "REDEEMED", "redeemed key moved to REDEEMED (got %s)" % st)
            if act.status_code == 200:
                body = act.json()
                check(bool(body.get("signed_license")),
                      "response still carries the Susi SignedLicense payload")
                check(body.get("remaining_seconds") == 30 * 24 * 3600,
                      "a 30-day key still grants exactly 30 days (%s s)"
                      % body.get("remaining_seconds"))
                check(body.get("authorization_id") is not None,
                      "redemption still creates/attaches the Authorization")
        else:
            skip("batch-generated key redemption (no development signing key)")

        print("\n[security] no plaintext key in the server log")
        proc.terminate()
        time.sleep(1.5)
        log = proc.stdout.read() if proc.poll() is not None else ""
        leaks = [k for k in all_plaintext if k and k in log]
        check(not leaks, "none of the %d generated plaintext keys appear in the log"
              % len(all_plaintext))
        check("Traceback" not in log, "no tracebacks in the server log")
        if "Traceback" in log:
            print(log[:2000])
    finally:
        if proc.poll() is None:
            proc.kill()


if __name__ == "__main__":
    print("=" * 66)
    print("Phase 7.2-7 batch generation - focused tests")
    print("temp dir: %s" % TMPDIR)
    print("=" * 66)

    part_b_rollback()
    part_a_http()

    print("\n" + "=" * 66)
    print("RESULT: %d/%d checks passed, %d failed, %d skipped"
          % (PASSED, PASSED + FAILED, FAILED, SKIPPED))
    print("=" * 66)
    sys.exit(1 if FAILED else 0)
