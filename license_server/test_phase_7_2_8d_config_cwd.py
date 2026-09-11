#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 7.2-8D: configuration loading must not depend on the process CWD.

Before the fix, ``Settings.Config.env_file`` was the bare string ".env", which
python-dotenv resolves against the *process working directory*. Starting the
server with any other CWD (a process manager, a service unit, uvicorn
--app-dir from elsewhere) silently skipped the whole file and let the code
defaults -- including the development SECRET_KEY -- take over, with no error.

After the fix, config.py derives an absolute .env path from its own location.

Covers:
  [1] CWD = license_server/      -> .env is loaded, values match the file
  [2] CWD = repository root      -> identical to [1]
  [3] CWD = unrelated temp dir   -> identical to [1]
  [4] a process environment variable still outranks .env (precedence unchanged)
  [5] regression proof: the old relative ".env" form loses the file from a
      foreign CWD, while the new absolute form does not

Each probe runs in a child process, because CWD is a process-global and cannot
be varied in-process. The managed variables are stripped from the child
environment so the baseline runs reflect .env + code defaults only. No secret is
ever printed -- credentials are compared as digests.
"""

import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent          # <repo>/license_server
REPO = ROOT.parent                                       # <repo>
CONFIG = ROOT / "app" / "config.py"
ENV_FILE = ROOT / ".env"

PASSED = 0
FAILED = 0
SKIPPED = 0

#: Compared across runs; a credential is compared as a digest, never printed.
ALL_KEYS = [
    "DATABASE_URL", "DEBUG", "API_PREFIX", "DEFAULT_MAX_DEVICES", "ALGORITHM",
    "SUSI_HELPER_PATH", "SUSI_DEVELOPMENT_PRIVATE_KEY_FILE",
    "SUSI_DEVELOPMENT_PRIVATE_KEY", "SUSI_DEVELOPMENT_PUBLIC_KEY",
    "SECRET_KEY", "ADMIN_API_KEY",
]

#: Cleared from the child environment so the baselines reflect .env + defaults.
MANAGED = ALL_KEYS

#: Keys whose .env text is not a string, so the raw file text is coerced before
#: it is compared to the parsed setting.
BOOL_KEYS = {"DEBUG"}
INT_KEYS = {"DEFAULT_MAX_DEVICES"}

PROBE = r'''
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("dlv_cfg_probe", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
settings = module.Settings()
out = {"__env_file__": getattr(module, "_ENV_FILE", None)}
for name in sys.argv[2].split(","):
    out[name] = getattr(settings, name, None)
print(json.dumps(out))
'''

#: The pre-fix form: a bare, CWD-relative env_file. Used only to demonstrate the
#: bug being fixed; it does not import project code. `extra="ignore"` is needed
#: because this stub declares a single field while the real .env carries many --
#: BaseSettings defaults to extra="forbid", which would otherwise raise.
OLD_FORM_PROBE = r'''
import json, sys
from pydantic_settings import BaseSettings, SettingsConfigDict
class OldStyle(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    DEBUG: bool = False
print(json.dumps({"DEBUG": OldStyle().DEBUG}))
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


def digest(value):
    return hashlib.sha256(repr(value).encode("utf-8")).hexdigest()[:16]


def strip_managed(extra=None):
    env = dict(os.environ)
    for name in MANAGED:
        env.pop(name, None)
    env.update(extra or {})
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_child(code, cwd, extra_env=None):
    result = subprocess.run(
        [sys.executable, "-c", code, str(CONFIG), ",".join(ALL_KEYS)],
        cwd=str(cwd),
        env=strip_managed(extra_env),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()[-1:]
        return {"__error__": tail}
    return json.loads(result.stdout.strip().splitlines()[-1])


def load_env_file():
    """Parse license_server/.env into a plain dict (no interpolation needed)."""
    values = {}
    if not ENV_FILE.is_file():
        return values
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        values[name.strip()] = value.strip()
    return values


def diff(left, right):
    """Names whose values differ, secrets reported as digests."""
    names = []
    for name in ALL_KEYS:
        a, b = left.get(name), right.get(name)
        if a != b:
            names.append("%s(%s vs %s)" % (digest_name(name, a), digest(a), digest(b)))
    return names


def digest_name(name, value):
    return name if name not in ("SECRET_KEY", "ADMIN_API_KEY") else name + ":digest"


if __name__ == "__main__":
    print("=" * 66)
    print("Phase 7.2-8D config CWD robustness")
    print("=" * 66)

    env_values = load_env_file()
    have_env_file = ENV_FILE.is_file()

    # ------------------------------------------------------------------
    print("\n[1] CWD = license_server/")
    baseline = run_child(PROBE, ROOT)
    check("__error__" not in baseline, "Settings load from license_server/")
    if "__error__" in baseline:
        print("     " + str(baseline["__error__"]))
        sys.exit(1)

    env_file_path = baseline.get("__env_file__")
    check(bool(env_file_path) and os.path.isabs(env_file_path),
          "the resolved env_file path is absolute: %s" % env_file_path)
    check(env_file_path == str(ENV_FILE),
          "the resolved env_file path is <license_server>/.env")
    check(ENV_FILE.is_file(),
          "license_server/.env exists on this machine (values below come from it)"
          if have_env_file else "license_server/.env is absent -- using code defaults")

    if have_env_file:
        for name in sorted(env_values):
            if name not in ALL_KEYS:
                continue
            expected = env_values[name]
            if name in BOOL_KEYS:
                expected = expected.lower() == "true"
            elif name in INT_KEYS:
                expected = int(expected)
            check(baseline.get(name) == expected,
                  ".env value is loaded for %s" % name)
    else:
        skip("per-key .env assertions (no .env on this machine)")

    # ------------------------------------------------------------------
    print("\n[2] CWD = repository root")
    from_repo_root = run_child(PROBE, REPO)
    check("__error__" not in from_repo_root, "Settings load from the repository root")
    if "__error__" in from_repo_root:
        print("     " + str(from_repo_root["__error__"]))
    else:
        check(not diff(baseline, from_repo_root),
              "every setting is identical to the license_server/ run"
              + ("" if not diff(baseline, from_repo_root)
                 else " -- differs: " + ", ".join(diff(baseline, from_repo_root))))

    # ------------------------------------------------------------------
    print("\n[3] CWD = unrelated temporary directory")
    with tempfile.TemporaryDirectory(prefix="dlv-cwd-") as temp_dir:
        from_temp = run_child(PROBE, temp_dir)
        check("__error__" not in from_temp, "Settings load from an unrelated CWD")
        if "__error__" not in from_temp:
            check(from_temp.get("__env_file__") == env_file_path,
                  "the env_file path does not move with the CWD")
            check(not diff(baseline, from_temp),
                  "every setting is identical to the license_server/ run"
                  + ("" if not diff(baseline, from_temp)
                     else " -- differs: " + ", ".join(diff(baseline, from_temp))))
        if have_env_file:
            check(from_temp.get("DEBUG") == (env_values.get("DEBUG", "").lower() == "true"),
                  "the .env DEBUG value survives a foreign CWD")

    # ------------------------------------------------------------------
    print("\n[4] a process environment variable still outranks .env")
    overrides = {
        "DATABASE_URL": "sqlite:///from-environment.db",
        "DEBUG": "false",
        "DEFAULT_MAX_DEVICES": "7",
    }
    overridden = run_child(PROBE, REPO, extra_env=overrides)
    check("__error__" not in overridden, "Settings load with environment overrides")
    if "__error__" not in overridden:
        check(overridden.get("DATABASE_URL") == overrides["DATABASE_URL"],
              "DATABASE_URL comes from the environment, not .env")
        check(overridden.get("DEBUG") is False,
              "DEBUG=false in the environment outranks .env (precedence unchanged)")
        check(overridden.get("DEFAULT_MAX_DEVICES") == 7,
              "an int setting is coerced and read from the environment")

    # ------------------------------------------------------------------
    print("\n[5] regression proof: CWD-relative '.env' vs the absolute path")
    if have_env_file:
        with tempfile.TemporaryDirectory(prefix="dlv-cwd-") as temp_dir:
            old_from_temp = run_child(OLD_FORM_PROBE, temp_dir)
            check(old_from_temp.get("DEBUG") is False,
                  "the old relative '.env' form loses the file from a foreign CWD"
                  " (this is the bug that was fixed)")
            old_from_server = run_child(OLD_FORM_PROBE, ROOT)
            check(old_from_server.get("DEBUG") is True,
                  "the old form only appeared to work because the CWD happened"
                  " to be license_server/")
    else:
        skip("regression proof (no .env on this machine)")

    print("\n" + "=" * 66)
    print("RESULT: %d/%d checks passed, %d failed, %d skipped"
          % (PASSED, PASSED + FAILED, FAILED, SKIPPED))
    print("=" * 66)
    sys.exit(1 if FAILED else 0)
