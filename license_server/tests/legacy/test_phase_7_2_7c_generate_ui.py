# -*- coding: utf-8 -*-
"""
Phase 7.2-7 (batch generation) - END-TO-END UI-path tests.

Motivation
----------
The 43-check batch suite posts to /admin/generate-keys with a hand-built dict.
That proves the *server* can generate N keys, but it does NOT prove that the
*rendered form* carries the quantity to the server: if the HTML form were
missing `name="quantity"`, or posted to the wrong action, or the field were
mistyped, every hand-built-dict test would still pass while the real browser
workflow silently generated one key.

These tests therefore drive the real thing:

    GET /admin/generate-keys
      -> parse the rendered HTML for the form's action + field names
      -> build the POST payload from THAT (never from a literal)
      -> POST it
      -> count the plaintext keys rendered AND the rows actually inserted

Run from license_server/:

    python test_phase_7_2_7c_generate_ui.py

Throwaway SQLite database in a temp dir; the development database is untouched.
"""

import hashlib
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
PORT = 8132
BASE = "http://127.0.0.1:%d" % PORT

TMPDIR = pathlib.Path(tempfile.mkdtemp(prefix="dlv-genui-test-"))
DB = (TMPDIR / "ui.db").as_posix()

PASSED = 0
FAILED = 0

#: The form SQLAdmin renders for this page, as the browser sees it.
EXPECTED_FIELDS = ["duration_days", "quantity", "features"]
EXPECTED_ACTION = "/admin/generate-keys"


def check(cond, msg):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print("  [ok]   " + msg, flush=True)
    else:
        FAILED += 1
        print("  [FAIL] " + msg, flush=True)
    return bool(cond)


def keys_from(html):
    """The plaintext keys the result panel actually shows to the user."""
    m = re.search(r'<textarea[^>]*id="generated-keys"[^>]*>(.*?)</textarea>', html, re.S)
    return [k.strip() for k in m.group(1).splitlines() if k.strip()] if m else []


def parse_form(html):
    """(action, field_names, quantity_input_tags) - exactly what a browser submits."""
    block = re.search(r'<form[^>]*method="POST".*?</form>', html, re.S)
    if not block:
        return None, [], []
    body = block.group(0)
    m = re.search(r'action="([^"]*)"', body)
    action = m.group(1) if m else None
    fields = []
    for tag in re.finditer(r"<(input|select|textarea)\b[^>]*>", body):
        nm = re.search(r'name="([^"]*)"', tag.group(0))
        if nm:
            fields.append(nm.group(1))
    qtags = re.findall(r"<input[^>]*name=\"quantity\"[^>]*>", body)
    return action, fields, qtags


def received_quantity_from(html):
    """What the result page reports the server received."""
    m = re.search(r"<code>(\d+)</code>", html)
    return m.group(1) if m else None


def main():
    import requests

    admin_key = None
    for line in (HERE / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("ADMIN_API_KEY="):
            admin_key = line.split("=", 1)[1].strip()
    if not admin_key:
        print("ADMIN_API_KEY not found in license_server/.env")
        sys.exit(2)

    env = dict(os.environ)
    env["DATABASE_URL"] = "sqlite:///" + DB
    env["PYTHONIOENCODING"] = "utf-8"

    launch = ("import sys; sys.path.insert(0,'app'); import uvicorn; "
              "uvicorn.run('main:app', host='127.0.0.1', port=%d, log_level='warning')" % PORT)
    proc = subprocess.Popen([sys.executable, "-c", launch], cwd=str(HERE), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
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
        con = sqlite3.connect(DB)

        def rows():
            return con.execute("select count(*) from licenses").fetchone()[0]

        def auths():
            return con.execute("select count(*) from authorizations").fetchone()[0]

        # ---------------------------------------------------------------
        # 1. Form integrity: the rendered form must actually carry quantity
        # ---------------------------------------------------------------
        print("\n[1] rendered form integrity (what the browser submits)")
        page = s.get(BASE + "/admin/generate-keys").text
        action, fields, qtags = parse_form(page)
        check(action == EXPECTED_ACTION, "form action is %r (got %r)" % (EXPECTED_ACTION, action))
        check(fields == EXPECTED_FIELDS,
              "form exposes exactly %r (got %r)" % (EXPECTED_FIELDS, fields))
        check(len(re.findall(r"<form", page)) == 1, "exactly one form on the page")
        check(len(qtags) == 1, "exactly one quantity input (got %d)" % len(qtags))
        if qtags:
            tag = qtags[0]
            check('name="quantity"' in tag, "quantity input carries name=\"quantity\"")
            check('max="1000"' in tag, "quantity input carries max=1000")
            check('min="1"' in tag, "quantity input carries min=1")

        def submit(html, qty, days="30"):
            """Post exactly the fields the given page declares."""
            a, fs, _ = parse_form(html)
            payload = {f: "" for f in fs}
            payload["duration_days"] = str(days)
            payload["quantity"] = str(qty)
            before = rows()
            r = s.post(BASE + (a or "/admin/generate-keys"), data=payload)
            return r, len(keys_from(r.text)), rows() - before

        # ---------------------------------------------------------------
        # 2. The real workflow: quantity N -> N keys, N rows
        # ---------------------------------------------------------------
        print("\n[2] end-to-end through the web route, form-declared fields only")
        for qty in (1, 2, 10, 100):
            auths_before = auths()
            r, shown, delta = submit(page, qty)
            check(r.status_code == 200, "quantity=%-4d POST -> 200 (got %s)" % (qty, r.status_code))
            check(shown == qty, "quantity=%-4d -> %d plaintext keys shown" % (qty, shown))
            check(delta == qty, "quantity=%-4d -> %d database rows inserted" % (qty, delta))
            check(auths() == auths_before, "quantity=%-4d created no Authorization" % qty)

        print("\n[3] the result page reports what the server actually received")
        r, shown, delta = submit(page, 10)
        check(received_quantity_from(r.text) == "10",
              "result page echoes received quantity (%r)" % received_quantity_from(r.text))
        check("已生成 10 个 License Key" in r.text, "result page states the generated count")

        # ---------------------------------------------------------------
        # 4. Re-submission from the RESULT page (not the fresh GET page)
        # ---------------------------------------------------------------
        print("\n[4] re-submitting from the result page (its own fields)")
        r2, shown, delta = submit(r.text, 4)
        _, fields2, _ = parse_form(r.text)
        check(fields2 == EXPECTED_FIELDS, "result page declares the same fields %r" % fields2)
        check(delta == 4, "re-submit from result page -> 4 rows (got %d)" % delta)

        r3, shown, delta = submit(r2.text, 25)
        check(delta == 25, "second re-submit -> 25 rows (got %d)" % delta)

        # ---------------------------------------------------------------
        # 5. Rejections leave the database untouched
        # ---------------------------------------------------------------
        print("\n[5] quantity validation, no rows written")
        n = rows()
        for bad in ("0", "-1", "abc", "1001", "", "10.5"):
            r, shown, delta = submit(page, bad)
            check(delta == 0, "quantity=%-6r written 0 rows (got %d)" % (bad, delta))
            check("alert-danger" in r.text, "quantity=%-6r rendered an error" % bad)
        check(rows() == n, "no rows by any invalid quantity (count=%d)" % rows())

        print("\n[6] duration whitelist still enforced in the UI path")
        n = rows()
        for bad in ("0", "45", "400", "abc", ""):
            r, shown, delta = submit(page, 3, days=bad)
            check(delta == 0, "duration_days=%-5r written 0 rows (got %d)" % (bad, delta))
        check(rows() == n, "no rows by any invalid duration (count=%d)" % rows())
        opts = re.findall(r'<option value="(\d+)"', page)
        check(opts == ["1", "30", "180", "365"],
              "generator still offers exactly 1/30/180/365 (got %r)" % opts)

        # ---------------------------------------------------------------
        # 6. Security: hashes only, no plaintext, no Authorization
        # ---------------------------------------------------------------
        print("\n[7] storage invariants unchanged")
        r, shown, delta = submit(page, 5)
        fresh = keys_from(r.text)
        all_rows = con.execute(
            "select id,key_hash,duration_days,state,authorization_id from licenses").fetchall()
        newest = all_rows[-5:]
        check(len(fresh) == 5, "5 plaintext keys rendered (got %d)" % len(fresh))
        check(all(x[1] == hashlib.sha256(k.encode()).hexdigest()
                  for k, x in zip(fresh, newest)), "every row stores sha256(its plaintext)")
        check(all(x[3] == "UNUSED" for x in newest), "all new rows are UNUSED")
        check(all(x[4] is None for x in newest), "no new row is attached to an Authorization")
        blob = "".join(str(c) for x in all_rows for c in x)
        check(not any(k in blob for k in fresh), "no plaintext key present in any column")

        print("\n[8] server log clean")
        proc.terminate()
        time.sleep(1.5)
        log = proc.stdout.read() if proc.poll() is not None else ""
        check("Traceback" not in log, "no tracebacks in the server log")
        check(not any(k in log for k in fresh), "no plaintext key leaked into the log")
        if "Traceback" in log:
            print(log[:2000])
    finally:
        if proc.poll() is None:
            proc.kill()


if __name__ == "__main__":
    print("=" * 66)
    print("Phase 7.2-7 batch generation - END-TO-END UI-path tests")
    print("temp db: %s" % DB)
    print("=" * 66)
    main()
    print("\n" + "=" * 66)
    print("RESULT: %d/%d checks passed, %d failed" % (PASSED, PASSED + FAILED, FAILED))
    print("=" * 66)
    sys.exit(1 if FAILED else 0)
