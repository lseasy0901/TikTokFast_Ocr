#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 7.2-8A/B: portability fixes for the two hard blockers found in the
production-readiness audit.

A. Client -- the License Server URL was a hardcoded constant with no override
   reachable from the shipped product, so the packaged client could only ever
   talk to localhost.
B. Server -- SUSI_HELPER_PATH was declared but never read, while the helper path
   was hardcoded to a Windows filename and checked at import, so the server could
   not start on a non-Windows host.

Covers:
  client : env override, localhost fallback, explicit-argument precedence,
           empty/whitespace env treated as unset, trailing-slash normalization,
           and the real GUI construction path (LicenseManager)
  server : SUSI_HELPER_PATH override wins, empty falls back to the default,
           Windows vs non-Windows default basename, and the live Settings object

No network, no database, and no susi_helper binary is required.
"""

import importlib.util
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

PASSED = 0
FAILED = 0

ENV_VAR = "DLV_LICENSE_SERVER_URL"


def check(cond, msg):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print("  [ok]   " + msg, flush=True)
    else:
        FAILED += 1
        print("  [FAIL] " + msg, flush=True)


class env_var:
    """Set (or clear) an environment variable for the duration of a block."""

    def __init__(self, name, value):
        self.name = name
        self.value = value

    def __enter__(self):
        self.saved = os.environ.get(self.name)
        if self.value is None:
            os.environ.pop(self.name, None)
        else:
            os.environ[self.name] = self.value
        return self

    def __exit__(self, *exc):
        if self.saved is None:
            os.environ.pop(self.name, None)
        else:
            os.environ[self.name] = self.saved
        return False


def load_module(name, relative_path):
    """Load a module straight from its file, avoiding sys.path/package side effects."""
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ======================================================================
# [1] Client: DLV_LICENSE_SERVER_URL
# ======================================================================
def client_tests():
    print("\n[1] client: DLV_LICENSE_SERVER_URL override")
    from utils.license_client import (
        DEFAULT_SERVER_URL,
        LicenseServerClient,
        resolve_server_url,
    )

    check(DEFAULT_SERVER_URL == "http://127.0.0.1:8000/api/v1",
          "the localhost default is preserved verbatim")

    with env_var(ENV_VAR, None):
        check(resolve_server_url() == DEFAULT_SERVER_URL,
              "unset env -> localhost default")
        check(LicenseServerClient().base_url == DEFAULT_SERVER_URL,
              "unset env -> client.base_url is the localhost default")

    with env_var(ENV_VAR, "https://lic.example.com/api/v1"):
        check(resolve_server_url() == "https://lic.example.com/api/v1",
              "set env -> env value is used")
        check(LicenseServerClient().base_url == "https://lic.example.com/api/v1",
              "set env -> client.base_url is the env value")

    with env_var(ENV_VAR, "https://lic.example.com/api/v1/"):
        check(resolve_server_url() == "https://lic.example.com/api/v1",
              "a trailing slash is normalized away")

    with env_var(ENV_VAR, ""):
        check(resolve_server_url() == DEFAULT_SERVER_URL,
              "empty env -> falls back to the default (not an empty URL)")

    with env_var(ENV_VAR, "   "):
        check(resolve_server_url() == DEFAULT_SERVER_URL,
              "whitespace-only env -> falls back to the default")

    with env_var(ENV_VAR, "https://from-env.example.com"):
        check(resolve_server_url("https://explicit.example.com")
              == "https://explicit.example.com",
              "an explicit argument still outranks the env var (unchanged behavior)")
        check(LicenseServerClient("https://explicit.example.com").base_url
              == "https://explicit.example.com",
              "an explicit base_url still outranks the env var")

    print("\n[2] client: the real GUI construction path honors the override")
    from utils.license_manager import LicenseManager

    with env_var(ENV_VAR, "https://lic.example.com/api/v1"):
        manager = LicenseManager()
        check(manager.server_url == "https://lic.example.com/api/v1",
              "LicenseManager() picks up the env value")
        check(manager.client.base_url == "https://lic.example.com/api/v1",
              "the manager's HTTP client uses the env value")

    with env_var(ENV_VAR, None):
        check(LicenseManager().server_url == DEFAULT_SERVER_URL,
              "LicenseManager() still defaults to localhost when unset")


# ======================================================================
# [3] Server: helper path resolution
# ======================================================================
def server_tests(svc, settings_cls):
    print("\n[3] server: SUSI_HELPER_PATH override and OS-aware default")

    absolute = os.path.join(tempfile.gettempdir(), "helper-under-test")

    check(svc._resolve_helper_path(absolute) == absolute,
          "configured path is used as-is")
    check(svc._resolve_helper_path("   " + absolute + "   ") == absolute,
          "configured path is stripped")
    check(svc._resolve_helper_path("") == svc._default_helper_path(),
          "empty configuration falls back to the default")
    check(svc._resolve_helper_path(None) == svc._default_helper_path(),
          "absent configuration falls back to the default")
    check(svc._resolve_helper_path("   ") == svc._default_helper_path(),
          "whitespace-only configuration falls back to the default")

    print("\n[4] server: OS-aware default basename")
    check(svc._helper_basename("nt") == "susi_helper.exe",
          'os.name == "nt" -> susi_helper.exe')
    check(svc._helper_basename("posix") == "susi_helper",
          'os.name == "posix" -> susi_helper (no .exe)')
    check(svc._helper_basename() == svc._helper_basename(os.name),
          "the default basename follows the current platform")

    default = svc._default_helper_path()
    check(os.path.isabs(default), "the default path is absolute")
    check(default.endswith(os.path.join("susi_helper", "target", "release",
                                        svc._helper_basename())),
          "the default path uses the project-root layout and OS basename")
    check("susi_helper.exe" in default if os.name == "nt" else True,
          "on Windows the default still carries the .exe (unchanged behavior)")

    # Simulate the other platform by patching the basename seam.
    real = svc._helper_basename
    try:
        svc._helper_basename = lambda platform_name=None: "susi_helper"
        posix_default = svc._default_helper_path()
        check(posix_default.endswith(os.sep + "susi_helper"),
              "non-Windows default resolves to .../release/susi_helper")
        check(not posix_default.endswith(".exe"),
              "non-Windows default has no .exe suffix")

        svc._helper_basename = lambda platform_name=None: "susi_helper.exe"
        check(svc._default_helper_path().endswith(os.sep + "susi_helper.exe"),
              "Windows default resolves to .../release/susi_helper.exe")
    finally:
        svc._helper_basename = real

    print("\n[5] server: the live Settings object reads the env var")
    with env_var("SUSI_HELPER_PATH", absolute):
        check(settings_cls().SUSI_HELPER_PATH == absolute,
              "Settings() picks up SUSI_HELPER_PATH from the environment")
    with env_var("SUSI_HELPER_PATH", None):
        check(settings_cls().SUSI_HELPER_PATH == "",
              "unset SUSI_HELPER_PATH defaults to empty ('not configured')")
        check(not settings_cls().SUSI_HELPER_PATH,
              "an empty value cannot pin the server to a developer machine path")

    print("\n[6] server: SusiSecurityService.__init__ uses the configured path")
    # sys.executable stands in for the helper: it exists, so the presence check
    # passes and we observe which path the service actually resolved.
    service = svc.SusiSecurityService({"SUSI_HELPER_PATH": sys.executable})
    check(service.susi_helper_path == sys.executable,
          "__init__ honors the configured SUSI_HELPER_PATH")

    service = svc.SusiSecurityService({})
    check(service.susi_helper_path == svc._default_helper_path(),
          "__init__ falls back to the default when unconfigured")


if __name__ == "__main__":
    print("=" * 66)
    print("Phase 7.2-8A/B portability")
    print("=" * 66)

    svc = load_module("dlv_susi_security_service",
                      "license_server/app/services/susi_security_service.py")
    cfg = load_module("dlv_license_server_config", "license_server/app/config.py")

    client_tests()
    server_tests(svc, cfg.Settings)

    print("\n" + "=" * 66)
    print("RESULT: %d/%d checks passed, %d failed"
          % (PASSED, PASSED + FAILED, FAILED))
    print("=" * 66)
    sys.exit(1 if FAILED else 0)
