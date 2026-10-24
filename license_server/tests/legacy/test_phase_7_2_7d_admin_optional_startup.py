# -*- coding: utf-8 -*-
"""
Phase 7.2-7d - the License Server must start WITHOUT the vendored admin snapshot.

Motivation
----------
``admin_web/`` (the unmodified upstream SQLAdmin 0.31.1 snapshot) is gitignored
(.gitignore:47) and is deployed out-of-band, so a fresh clone does not have it.
``app/main.py`` calls ``setup_admin(app)`` at import time, so a hard failure there
did not merely disable ``/admin`` -- it took the whole License Server down, API
routes included.

The contract these tests pin:

    snapshot absent   -> setup_admin() returns None, /admin is not mounted,
                         every API route still works, no traceback
    snapshot present  -> setup_admin() returns the Admin instance, /admin mounts

Case 3 is SKIPPED (not failed) when the snapshot is absent, so this file is
meaningful on a fresh clone as well as on a provisioned machine.

Nothing here writes to the development database: a throwaway SQLite file in a temp
directory is exported as DATABASE_URL before any app module is imported.
"""

import logging
import os
import pathlib
import subprocess
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
PORT = 8133            # the fresh-clone server (snapshot absent)
PORT_WITH_ADMIN = 8134  # the provisioned server (snapshot present)
BASE = "http://127.0.0.1:%d" % PORT

TMPDIR = pathlib.Path(tempfile.mkdtemp(prefix="dlv-adminopt-test-"))

# Must be set BEFORE `import config` / `import database` anywhere in this process.
# main.py runs Base.metadata.create_all() at import, so without this the dev
# database would be created/touched.
os.environ["DATABASE_URL"] = "sqlite:///" + (TMPDIR / "adminopt.db").as_posix()

sys.path.insert(0, str(HERE / "app"))

PASSED = 0
FAILED = 0
SKIPPED = 0

#: A path that is guaranteed not to exist, used to simulate a fresh clone.
ABSENT_SNAPSHOT = str(TMPDIR / "no-such-admin_web" / "sqladmin")


def check(cond, msg):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print("  [ok]   " + msg, flush=True)
    else:
        FAILED += 1
        print("  [FAIL] " + msg, flush=True)


def skip(msg):
    global SKIPPED
    SKIPPED += 1
    print("  [skip] " + msg, flush=True)


def admin_route_paths(app):
    """Every registered route path that lives under /admin."""
    return [getattr(r, "path", "") for r in app.routes
            if getattr(r, "path", "").startswith("/admin")]


class CaptureWarnings(logging.Handler):
    """Collect WARNING+ records from the `admin` logger."""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.records = []

    def emit(self, record):
        self.records.append(record)


# ======================================================================
# [1] Snapshot absent -> setup_admin skips, app stays healthy
# ======================================================================
def part_1_skip_in_process():
    print("\n[1] snapshot absent: setup_admin() skips, API app unaffected")
    from fastapi import FastAPI

    import admin

    real_snapshot = admin.VENDORED_SQLADMIN_DIR
    admin.VENDORED_SQLADMIN_DIR = ABSENT_SNAPSHOT

    handler = CaptureWarnings()
    admin.logger.addHandler(handler)
    try:
        check(admin.vendored_sqladmin_available() is False,
              "vendored_sqladmin_available() reports False")

        app = FastAPI()
        app.add_api_route("/", lambda: {"status": "ok"}, methods=["GET"])
        paths_before = [getattr(r, "path", "") for r in app.routes]

        result = admin.setup_admin(app)

        paths_after = [getattr(r, "path", "") for r in app.routes]
        check(result is None, "setup_admin() returns None instead of raising")
        check(admin_route_paths(app) == [], "no /admin route was mounted")
        check("/" in paths_after, "the app's own routes are still registered")
        check(len(paths_after) == len(paths_before),
              "setup_admin() neither added nor removed any route")

        messages = [r.getMessage() for r in handler.records]
        check(any("跳过管理后台挂载" in m for m in messages),
              "a warning explains that the admin was skipped")
        check(any(ABSENT_SNAPSHOT in m for m in messages),
              "the warning names the missing snapshot directory")
        check(all("Traceback" not in m for m in messages),
              "skipping is a warning, not an exception")
    finally:
        admin.logger.removeHandler(handler)
        admin.VENDORED_SQLADMIN_DIR = real_snapshot


# ======================================================================
# [2] Fresh-clone simulation: real uvicorn main:app, snapshot absent
# ======================================================================
def part_2_real_server_without_snapshot():
    print("\n[2] fresh-clone simulation: real `main:app` boots without admin_web/")
    import requests

    env = dict(os.environ)
    env["DATABASE_URL"] = os.environ["DATABASE_URL"]
    env["PYTHONIOENCODING"] = "utf-8"

    # Patch the constant before `main` is imported, so main.py's module-level
    # setup_admin(app) call sees a clone without the snapshot. `admin` is imported
    # first, so the patch is in place by the time main does `from admin import ...`.
    launch = (
        "import sys; sys.path.insert(0,'app');"
        "import admin; admin.VENDORED_SQLADMIN_DIR = %r;" % ABSENT_SNAPSHOT +
        "import uvicorn; uvicorn.run('main:app', host='127.0.0.1', port=%d,"
        " log_level='warning')" % PORT
    )
    # Decode explicitly as UTF-8: the child is forced to UTF-8 via PYTHONIOENCODING,
    # but this process's locale is GBK, so a bare text=True would fail on the child's
    # log bytes.
    proc = subprocess.Popen([sys.executable, "-c", launch], cwd=str(HERE), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")
    try:
        ready = False
        for _ in range(160):
            try:
                if requests.get(BASE + "/", timeout=1).status_code == 200:
                    ready = True
                    break
            except Exception:
                pass
            if proc.poll() is not None:
                out = proc.stdout.read() if proc.stdout else ""
                print("SERVER DIED:\n" + out[:3000])
                break
            time.sleep(0.25)

        check(ready, "the License Server started without admin_web/")
        if not ready:
            return

        r = requests.get(BASE + "/", timeout=5)
        check(r.status_code == 200, "GET / still returns 200 (got %d)" % r.status_code)

        schema = requests.get(BASE + "/openapi.json", timeout=5)
        check(schema.status_code == 200,
              "GET /openapi.json returns 200 (got %d)" % schema.status_code)
        paths = list(schema.json().get("paths", {})) if schema.status_code == 200 else []
        for expected in ("/api/v1/licenses/activate", "/api/v1/licenses/validate"):
            check(expected in paths, "API route %s is registered" % expected)

        absent = requests.get(BASE + "/admin", timeout=5, allow_redirects=False)
        check(absent.status_code == 404,
              "GET /admin is 404 while the snapshot is absent (got %d)"
              % absent.status_code)

        proc.terminate()
        time.sleep(1.5)
        out = proc.stdout.read() if (proc.poll() is not None and proc.stdout) else ""
        check("Traceback" not in out, "no traceback in the server log")
        if "Traceback" in out:
            print(out[:3000])
    finally:
        if proc.poll() is None:
            proc.kill()


# ======================================================================
# [3] Regression guard: with the snapshot present, /admin still mounts
# ======================================================================
def part_3_snapshot_present_mounts():
    print("\n[3] snapshot present: /admin still mounts (normal path unchanged)")
    from fastapi import FastAPI

    import admin

    if not admin.vendored_sqladmin_available():
        skip("admin_web/ is not deployed here - skipped by design")
        return

    # --- 3a: SQLAdmin mounts as a Mount at base_url, so the login route lives on
    # the mounted sub-app rather than as a literal '/admin/login' path on the parent.
    app = FastAPI()
    result = admin.setup_admin(app)

    mounts = [r for r in app.routes if getattr(r, "path", "") == "/admin"]
    check(result is not None, "setup_admin() returns the Admin instance")
    check(mounts != [], "/admin is mounted on the app")

    inner = [getattr(r, "path", "") for r in mounts[0].app.routes] if mounts else []
    check("/login" in inner, "the admin login route is registered")
    check("/{identity}/list" in inner, "the admin list route is registered")

    # --- 3b: the same thing over real HTTP, which is what actually matters
    import requests

    env = dict(os.environ)
    env["DATABASE_URL"] = os.environ["DATABASE_URL"]
    env["PYTHONIOENCODING"] = "utf-8"
    launch = (
        "import sys; sys.path.insert(0,'app'); import uvicorn; "
        "uvicorn.run('main:app', host='127.0.0.1', port=%d, log_level='warning')"
        % PORT_WITH_ADMIN
    )
    base = "http://127.0.0.1:%d" % PORT_WITH_ADMIN
    proc = subprocess.Popen([sys.executable, "-c", launch], cwd=str(HERE), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")
    try:
        ready = False
        for _ in range(160):
            try:
                if requests.get(base + "/", timeout=1).status_code == 200:
                    ready = True
                    break
            except Exception:
                pass
            if proc.poll() is not None:
                out = proc.stdout.read() if proc.stdout else ""
                print("SERVER DIED:\n" + out[:3000])
                break
            time.sleep(0.25)

        check(ready, "the License Server started with admin_web/ present")
        if ready:
            r = requests.get(base + "/admin/login", timeout=5)
            check(r.status_code == 200,
                  "GET /admin/login returns 200 (got %d)" % r.status_code)

            g = requests.get(base + "/admin", timeout=5, allow_redirects=False)
            check(g.status_code in (302, 307),
                  "unauthenticated GET /admin redirects to login (got %d)"
                  % g.status_code)

        proc.terminate()
        time.sleep(1.5)
        out = proc.stdout.read() if (proc.poll() is not None and proc.stdout) else ""
        check("Traceback" not in out, "no traceback in the admin-enabled server log")
        if "Traceback" in out:
            print(out[:3000])
    finally:
        if proc.poll() is None:
            proc.kill()


if __name__ == "__main__":
    print("=" * 66)
    print("Phase 7.2-7d admin-optional startup")
    print("temp db: %s" % os.environ["DATABASE_URL"])
    print("=" * 66)
    part_1_skip_in_process()
    part_2_real_server_without_snapshot()
    part_3_snapshot_present_mounts()
    print("\n" + "=" * 66)
    print("RESULT: %d/%d checks passed, %d failed, %d skipped"
          % (PASSED, PASSED + FAILED, FAILED, SKIPPED))
    print("=" * 66)
    sys.exit(1 if FAILED else 0)
