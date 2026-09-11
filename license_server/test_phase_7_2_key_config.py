#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 7.2 signing-key configuration test.

Regression cover for the blocking activation defect where
SUSI_DEVELOPMENT_PRIVATE_KEY resolved to an empty string, so susi_helper received
an empty PEM and reported:

    Invalid private key PEM: PEM error: PKCS#8 ASN.1 error:
    PEM error: PEM preamble contains invalid data (NUL byte)

That message is encoding-shaped, but the cause was an unconfigured key. These tests
pin the resolution precedence, the .env representations, and the replacement of the
misleading error with an actionable one.

Security: no test here prints, asserts on, or writes private key material. Assertions
about the resolved key are limited to "is a PEM" / length. Failure diagnostics are
reported as pass/fail text only.

Run from license_server/:
    python test_phase_7_2_key_config.py
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
APP_DIR = os.path.join(HERE, "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from test_key_loader import load_test_rsa_key  # noqa: E402

from services import susi_security_service  # noqa: E402
from services.susi_security_service import (  # noqa: E402
    SusiSecurityService,
    _looks_like_private_key_pem,
    _normalize_pem_text,
    _read_pem_file,
)

HELPER = os.path.join(
    os.path.dirname(HERE), "susi_helper", "target", "release", "susi_helper.exe"
)
PUBLIC_KEY_FILE = os.path.join(os.path.dirname(HERE), "license_public_key.pem")

RESULTS = []


def check(name, condition, detail=""):
    RESULTS.append((name, bool(condition), detail))
    print("%-4s %s%s" % ("PASS" if condition else "FAIL", name,
                         "" if condition or not detail else "  -- " + detail))


def service_with(config):
    return SusiSecurityService(config)


# ----------------------------------------------------------------------
# 1. PEM normalisation helpers
# ----------------------------------------------------------------------
def test_normalization():
    pem = load_test_rsa_key()
    body = pem.replace("\r\n", "\n")

    escaped = body.replace("\n", "\\n")
    check("normalize: escaped \\n is restored",
          _normalize_pem_text(escaped) == body)

    check("normalize: CRLF is unified to LF",
          _normalize_pem_text(pem) == body)

    check("normalize: surrounding whitespace trimmed",
          _normalize_pem_text("  \n" + body + "\n  ") == body)

    check("normalize: empty input stays empty",
          _normalize_pem_text("") == "" and _normalize_pem_text(None) == "")

    check("normalize: whitespace-only collapses to empty",
          _normalize_pem_text("   \n\t ") == "")


# ----------------------------------------------------------------------
# 2. Structural PEM check
# ----------------------------------------------------------------------
def test_structure_check():
    pem = load_test_rsa_key()
    check("structure: real PKCS#8 key accepted", _looks_like_private_key_pem(pem))
    check("structure: empty rejected", not _looks_like_private_key_pem(""))
    check("structure: whitespace rejected", not _looks_like_private_key_pem("   "))
    check("structure: truncated BEGIN marker rejected",
          not _looks_like_private_key_pem("-----BEGIN PRIVATE KEY-----"))
    check("structure: public key rejected",
          not _looks_like_private_key_pem(
              "-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----"))


# ----------------------------------------------------------------------
# 3. Resolution precedence
# ----------------------------------------------------------------------
def test_resolution_file():
    svc = service_with({
        "SUSI_DEVELOPMENT_PRIVATE_KEY_FILE": os.path.join(HERE, "test_rsa_key.pem"),
        "SUSI_DEVELOPMENT_PRIVATE_KEY": "",
    })
    check("resolve: file setting loads a private-key PEM",
          _looks_like_private_key_pem(svc.development_private_key))
    check("resolve: file source names the path",
          "test_rsa_key.pem" in svc._private_key_source)


def test_resolution_relative_path():
    """A relative path resolves against license_server/, not the CWD."""
    os.chdir(os.path.dirname(HERE))  # deliberately not license_server/
    try:
        svc = service_with({"SUSI_DEVELOPMENT_PRIVATE_KEY_FILE": "test_rsa_key.pem"})
        check("resolve: relative path is CWD-independent",
              _looks_like_private_key_pem(svc.development_private_key))
    finally:
        os.chdir(HERE)


def test_resolution_inline():
    """The inline .env form (escaped newlines) still works."""
    escaped = load_test_rsa_key().replace("\r\n", "\n").replace("\n", "\\n")
    svc = service_with({
        "SUSI_DEVELOPMENT_PRIVATE_KEY_FILE": "",
        "SUSI_DEVELOPMENT_PRIVATE_KEY": escaped,
    })
    check("resolve: inline escaped \\n loads a private-key PEM",
          _looks_like_private_key_pem(svc.development_private_key))
    check("resolve: inline source names the variable",
          svc._private_key_source == "SUSI_DEVELOPMENT_PRIVATE_KEY")


def test_resolution_file_wins_over_inline():
    svc = service_with({
        "SUSI_DEVELOPMENT_PRIVATE_KEY_FILE": os.path.join(HERE, "test_rsa_key.pem"),
        "SUSI_DEVELOPMENT_PRIVATE_KEY": load_test_rsa_key().replace("\r\n", "\n"),
    })
    check("resolve: explicit FILE takes precedence over inline",
          "test_rsa_key.pem" in svc._private_key_source)


def test_missing_file_is_reported_not_silently_skipped():
    """An explicitly set path that does not exist must not fall through to inline."""
    svc = service_with({
        "SUSI_DEVELOPMENT_PRIVATE_KEY_FILE": os.path.join(HERE, "no_such_key.pem"),
        "SUSI_DEVELOPMENT_PRIVATE_KEY": load_test_rsa_key().replace("\r\n", "\n"),
    })
    check("resolve: missing file is reported, not silently skipped",
          not _looks_like_private_key_pem(svc.development_private_key)
          and "not found" in svc._private_key_source)


def test_truncated_inline_is_reported():
    """The classic broken .env value: unquoted multi-line PEM truncated to line 1."""
    svc = service_with({
        "SUSI_DEVELOPMENT_PRIVATE_KEY_FILE": "",
        "SUSI_DEVELOPMENT_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----",
    })
    check("resolve: truncated inline value is reported as such",
          not _looks_like_private_key_pem(svc.development_private_key)
          and "truncated" in svc._private_key_source)


def test_utf16_file_rejected_clearly():
    """A UTF-16 key file is the one case that really does contain NUL bytes."""
    utf16_path = os.path.join(HERE, "_utf16_probe.pem")
    try:
        with open(utf16_path, "wb") as f:
            f.write(load_test_rsa_key().encode("utf-16"))
        raised = None
        try:
            _read_pem_file(utf16_path)
        except RuntimeError as e:
            raised = str(e)
        check("resolve: UTF-16 key file rejected with an encoding message",
              raised is not None and "NUL" in raised and "UTF-16" in raised)
    finally:
        if os.path.exists(utf16_path):
            os.remove(utf16_path)


# ----------------------------------------------------------------------
# 4. Signing fails fast and clearly when nothing is configured
# ----------------------------------------------------------------------
def _license_data():
    """A payload shaped like what RedemptionService passes to the signer.

    ``created_at`` must be a real datetime: Susi's LicensePayload.created is a
    non-optional chrono DateTime<Utc>.
    """
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    return {
        "id": "test-id",
        "license_key": "K" * 43,
        "created_at": now,
        "features": {"basic"},
        "authorization": {"expires_at": now + timedelta(days=30)},
    }


def test_unconfigured_key_fails_clearly():
    """Empty config must produce an actionable error, not the NUL-byte message."""
    # Blank every configured source *and* the developer default, so this exercises
    # the genuinely-unconfigured path even on a machine that has the dev key.
    saved = susi_security_service._DEV_SIGNING_KEY_PATH
    susi_security_service._DEV_SIGNING_KEY_PATH = os.path.join(HERE, "no_default.pem")
    try:
        svc = service_with({
            "SUSI_DEVELOPMENT_PRIVATE_KEY_FILE": "",
            "SUSI_DEVELOPMENT_PRIVATE_KEY": "",
        })
        check("unconfigured: resolver reports 'not configured'",
              svc._private_key_source == "not configured")

        raised = None
        try:
            svc.create_signed_license(_license_data(), device_id="dev", duration_days=30)
        except RuntimeError as e:
            raised = str(e)
    finally:
        susi_security_service._DEV_SIGNING_KEY_PATH = saved

    check("unconfigured key: raises RuntimeError", raised is not None)
    check("unconfigured key: message says not configured",
          raised is not None and "not configured" in raised)
    check("unconfigured key: message names the config variables",
          raised is not None
          and "SUSI_DEVELOPMENT_PRIVATE_KEY_FILE" in raised
          and "SUSI_DEVELOPMENT_PRIVATE_KEY" in raised)
    check("unconfigured key: message does NOT contain the misleading NUL error",
          raised is not None and "NUL byte" not in raised)
    # The error must never leak key material.
    check("unconfigured key: message contains no PEM body",
          raised is not None and "BEGIN" not in raised)


def test_no_key_material_in_errors_ever():
    """No resolution outcome may surface key content."""
    pem = load_test_rsa_key()
    body_first_b64 = [
        line for line in pem.replace("\r\n", "\n").splitlines()
        if line and not line.startswith("-----")
    ][0]

    sources = []
    for config in (
        {"SUSI_DEVELOPMENT_PRIVATE_KEY_FILE": os.path.join(HERE, "test_rsa_key.pem")},
        {"SUSI_DEVELOPMENT_PRIVATE_KEY_FILE": "",
         "SUSI_DEVELOPMENT_PRIVATE_KEY": pem.replace("\r\n", "\n")},
        {"SUSI_DEVELOPMENT_PRIVATE_KEY_FILE": os.path.join(HERE, "missing.pem"),
         "SUSI_DEVELOPMENT_PRIVATE_KEY": ""},
        {"SUSI_DEVELOPMENT_PRIVATE_KEY_FILE": "",
         "SUSI_DEVELOPMENT_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----"},
    ):
        svc = service_with(config)
        sources.append(svc._private_key_source)

    check("no key material in any resolution source string",
          all(body_first_b64 not in s for s in sources))


# ----------------------------------------------------------------------
# 5. Real signing round trip
# ----------------------------------------------------------------------
def test_real_sign_and_verify():
    if not os.path.exists(HELPER):
        check("real sign/verify round trip (SKIPPED: susi_helper missing)", True)
        return

    svc = service_with({
        "SUSI_DEVELOPMENT_PRIVATE_KEY_FILE": os.path.join(HERE, "test_rsa_key.pem"),
    })
    if not _looks_like_private_key_pem(svc.development_private_key):
        check("real sign/verify round trip (SKIPPED: no key)", True)
        return

    result = svc.create_signed_license(
        _license_data(), device_id="dev-device", duration_days=30
    )
    signed_license = result.get("signed_license")
    check("real signing: returns a signed_license", bool(signed_license))
    if not signed_license:
        return

    # Verify with the client-side public key, exactly as the desktop app would.
    if not os.path.exists(PUBLIC_KEY_FILE):
        check("real signing: verifies with client public key (SKIPPED: no pubkey)", True)
        return

    with open(PUBLIC_KEY_FILE, "r", encoding="utf-8") as f:
        public_key_pem = f.read().replace("\r\n", "\n").strip()

    cmd = {
        "command": "Verify",
        "signed_license": signed_license,
        "public_key_pem": public_key_pem,
    }
    import json
    proc = subprocess.run(
        [HELPER], input=json.dumps(cmd), capture_output=True, text=True, timeout=15
    )
    response = json.loads(proc.stdout)
    check("real signing: verifies with the client public key",
          "Success" in response,
          str(response.get("Error", {}).get("message", ""))[:120])


def main():
    print(__doc__.strip().splitlines()[0])
    print("=" * 70)

    test_normalization()
    test_structure_check()
    test_resolution_file()
    test_resolution_relative_path()
    test_resolution_inline()
    test_resolution_file_wins_over_inline()
    test_missing_file_is_reported_not_silently_skipped()
    test_truncated_inline_is_reported()
    test_utf16_file_rejected_clearly()
    test_unconfigured_key_fails_clearly()
    test_no_key_material_in_errors_ever()
    test_real_sign_and_verify()

    print("=" * 70)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = len(RESULTS) - passed
    print("%d passed, %d failed, %d total" % (passed, failed, len(RESULTS)))
    print("NOTE: no private key material was printed by this test.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
