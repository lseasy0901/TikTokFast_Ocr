#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 7.2-8E: SQLite concurrency robustness.

SQLite's default busy handler gives up almost immediately, so two concurrent
writers surface as "database is locked" instead of queueing. Phase 7.2-8E adds a
busy timeout and enables WAL, SQLite only.

Everything here runs in child processes, so the checks exercise the real
database.py wiring (engine creation, connect_args, the connect-event listener)
rather than a re-implementation of it in the test. All databases used are
temporary copies -- no test touches the repository's own database files, and the
URL is always supplied through the environment so this file is CWD-independent.

Covers:
  [1] wiring       -- the listener is registered and the dialect is sqlite
  [2] busy_timeout -- PRAGMA busy_timeout matches the configured constant
  [3] WAL          -- PRAGMA journal_mode is "wal", and it persists across
                      separate connections and process runs
  [4] non-SQLite   -- URL detection and connect_args are unchanged (empty)
  [5] existing DB  -- a copy of the project's real database stays readable and
                      keeps its row counts after initialization
  [6] idempotent   -- repeated initialization changes neither mode nor data
  [7] startup      -- `import main` succeeds and leaves a working, WAL database
"""

import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent          # <repo>/license_server
APP = ROOT / "app"
EXISTING_DB = ROOT / "test_license_server.db"

PASSED = 0
FAILED = 0
SKIPPED = 0

#: Values this test controls; cleared from the child environment so the baseline
#: reflects only what the test passes in (never the ambient shell).
MANAGED = ("DATABASE_URL", "SECRET_KEY", "ADMIN_API_KEY", "DEBUG", "API_PREFIX",
           "DEFAULT_MAX_DEVICES", "SUSI_HELPER_PATH",
           "SUSI_DEVELOPMENT_PRIVATE_KEY_FILE", "SUSI_DEVELOPMENT_PRIVATE_KEY",
           "SUSI_DEVELOPMENT_PUBLIC_KEY")

#: Exercises the real module wiring: import database, then read the pragmas and
#: the table contents back off a live connection.
DB_PROBE = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
import database
from sqlalchemy import event, text

database.Base.metadata.create_all(bind=database.engine)

out = {
    "listener": event.contains(database.engine, "connect", database.apply_sqlite_pragmas),
    "dialect": database.engine.dialect.name,
}
with database.engine.connect() as conn:
    out["busy_timeout"] = conn.execute(text("PRAGMA busy_timeout")).scalar()
    out["journal_mode"] = conn.execute(text("PRAGMA journal_mode")).scalar()
    out["licenses"] = conn.execute(text("SELECT COUNT(*) FROM licenses")).scalar()
    out["authorizations"] = conn.execute(text("SELECT COUNT(*) FROM authorizations")).scalar()
print(json.dumps(out))
'''

#: Proves the config helpers are backend-scoped. Importing database is enough:
#: create_engine does not connect, and no non-SQLite driver is needed.
HELPER_PROBE = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
import database
urls = [
    "sqlite:///x.db",
    "sqlite+pysqlite:///x.db",
    "postgresql://u:p@h/db",
    "postgresql+psycopg2://u:p@h/db",
    "mysql+pymysql://u:p@h/db",
]
print(json.dumps({
    u: {
        "is_sqlite": database.is_sqlite_url(u),
        "connect_args": database.sqlite_connect_args(u),
    } for u in urls
}))
'''

#: The startup path: importing main runs create_all and mounts the app.
STARTUP_PROBE = r'''
import sys
sys.path.insert(0, sys.argv[1])
import main
print("STARTED " + main.app.title)
'''

#: The timeout the phase is expected to configure, read from the module itself.
TIMEOUT_PROBE = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
import database
print(json.dumps({"busy_timeout_seconds": database.SQLITE_BUSY_TIMEOUT_SECONDS}))
'''


def check(cond, msg):
    global PASSED, FAILED, SKIPPED
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


def child_env(extra=None):
    env = dict(os.environ)
    for name in MANAGED:
        env.pop(name, None)
    env.update(extra or {})
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_probe(code, database_url, cwd=None):
    result = subprocess.run(
        [sys.executable, "-c", code, str(APP)],
        cwd=str(cwd or tempfile.gettempdir()),
        env=child_env({"DATABASE_URL": database_url}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()[-3:]
        return {"__error__": tail}
    return json.loads(result.stdout.strip().splitlines()[-1])


def sqlite_url(path):
    """sqlite:/// URL for an absolute local path (forward slashes on Windows)."""
    return "sqlite:///" + pathlib.Path(path).as_posix()


def counts_of(path):
    """Row counts read straight from a database file with the stdlib driver."""
    con = sqlite3.connect(str(path))
    try:
        licenses = con.execute("SELECT COUNT(*) FROM licenses").fetchone()[0]
        authorizations = con.execute("SELECT COUNT(*) FROM authorizations").fetchone()[0]
    finally:
        con.close()
    return {"licenses": licenses, "authorizations": authorizations}


if __name__ == "__main__":
    print("=" * 66)
    print("Phase 7.2-8E SQLite concurrency")
    print("=" * 66)

    with tempfile.TemporaryDirectory(prefix="dlv-sqlite-") as temp_dir:
        temp = pathlib.Path(temp_dir)

        # --------------------------------------------------------------
        print("\n[1] wiring: engine setup and the WAL listener")
        fresh = temp / "fresh.db"
        first = run_probe(DB_PROBE, sqlite_url(fresh))
        check("__error__" not in first, "importing database and connecting succeeds")
        if "__error__" in first:
            print("     " + " / ".join(first["__error__"]))
            sys.exit(1)

        check(first["listener"] is True,
              "apply_sqlite_pragmas is registered on the engine's connect event")
        check(first["dialect"] == "sqlite", "the engine dialect is sqlite")

        # --------------------------------------------------------------
        print("\n[2] busy_timeout")
        timeout = run_probe(TIMEOUT_PROBE, sqlite_url(fresh))
        configured = timeout.get("busy_timeout_seconds")
        check(isinstance(configured, (int, float)) and configured > 0,
              "SQLITE_BUSY_TIMEOUT_SECONDS is configured (%s s)" % configured)
        check(first["busy_timeout"] == int(configured * 1000),
              "PRAGMA busy_timeout is %s ms (was SQLite's ~5000 ms default)"
              % first["busy_timeout"])

        # --------------------------------------------------------------
        print("\n[3] WAL")
        check(str(first["journal_mode"]).lower() == "wal",
              "PRAGMA journal_mode is 'wal' on a fresh database")

        again = run_probe(DB_PROBE, sqlite_url(fresh))
        check(str(again.get("journal_mode", "")).lower() == "wal",
              "a separate process/connection also sees 'wal' (it is persisted"
              " in the file, not per-connection state)")
        check(again.get("listener") is True,
              "the listener is attached on every run, so WAL is re-applied to"
              " databases created before this phase")

        # --------------------------------------------------------------
        print("\n[4] non-SQLite configuration is unchanged")
        helpers = run_probe(HELPER_PROBE, sqlite_url(fresh))
        check("__error__" not in helpers, "the config helpers are importable")
        if "__error__" not in helpers:
            check(helpers.get("sqlite:///x.db", {}).get("connect_args")
                  == {"check_same_thread": False, "timeout": configured},
                  "a sqlite URL keeps check_same_thread=False and adds the timeout")
            for url in ("postgresql://u:p@h/db", "postgresql+psycopg2://u:p@h/db",
                        "mysql+pymysql://u:p@h/db"):
                entry = helpers.get(url, {})
                check(entry.get("is_sqlite") is False,
                      "%s is not treated as SQLite" % url.split("://")[0])
                check(entry.get("connect_args") == {},
                      "%s gets no connect_args (no timeout, no pragma listener)"
                      % url.split("://")[0])

        # --------------------------------------------------------------
        print("\n[5] an existing database stays readable and intact")
        if EXISTING_DB.is_file():
            copy = temp / "existing.db"
            shutil.copy2(EXISTING_DB, copy)
            # Carry over any sidecar files so the copy is a faithful database.
            for suffix in ("-wal", "-shm"):
                sidecar = pathlib.Path(str(EXISTING_DB) + suffix)
                if sidecar.is_file():
                    shutil.copy2(sidecar, str(copy) + suffix)

            before = counts_of(copy)
            result = run_probe(DB_PROBE, sqlite_url(copy))
            check("__error__" not in result,
                  "a pre-existing database initializes without error")
            if "__error__" not in result:
                after = counts_of(copy)
                check(after == before,
                      "row counts are unchanged (%s)" % after)
                check(str(result.get("journal_mode", "")).lower() == "wal",
                      "the pre-existing database is now in WAL mode")
                check(result.get("licenses") == after["licenses"]
                      and result.get("authorizations") == after["authorizations"],
                      "the engine reads the same rows it did before")
        else:
            skip("no pre-existing database at %s" % EXISTING_DB.name)

        # --------------------------------------------------------------
        print("\n[6] initialization is idempotent")
        idem = temp / "idempotent.db"
        baseline = None
        stable = True
        for _ in range(3):
            run = run_probe(DB_PROBE, sqlite_url(idem))
            if "__error__" in run:
                stable = False
                break
            if baseline is None:
                baseline = run
            elif (run["journal_mode"], run["licenses"], run["authorizations"]) != (
                    baseline["journal_mode"], baseline["licenses"],
                    baseline["authorizations"]):
                stable = False
        check(stable, "three initializations leave journal_mode and row counts"
                      " unchanged")

        # --------------------------------------------------------------
        print("\n[7] startup still works")
        startup_db = temp / "startup.db"
        result = subprocess.run(
            [sys.executable, "-c", STARTUP_PROBE, str(APP)],
            cwd=str(ROOT),
            env=child_env({"DATABASE_URL": sqlite_url(startup_db)}),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        check(result.returncode == 0,
              "`import main` succeeds with the new engine setup"
              + ("" if result.returncode == 0 else
                 " :: " + (result.stderr or "").strip().splitlines()[-1:].__str__()))
        check("STARTED" in (result.stdout or ""),
              "the FastAPI app is constructed")
        check(startup_db.is_file(),
              "the configured database file was created at DATABASE_URL")
        if startup_db.is_file():
            con = sqlite3.connect(str(startup_db))
            try:
                mode = con.execute("PRAGMA journal_mode").fetchone()[0]
                tables = {row[0] for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            finally:
                con.close()
            check(str(mode).lower() == "wal", "the startup database is in WAL mode")
            check({"licenses", "authorizations"} <= tables,
                  "both business tables exist after startup")

    print("\n" + "=" * 66)
    print("RESULT: %d/%d checks passed, %d failed, %d skipped"
          % (PASSED, PASSED + FAILED, FAILED, SKIPPED))
    print("=" * 66)
    sys.exit(1 if FAILED else 0)
