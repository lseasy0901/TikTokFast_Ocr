# Phase 7.2-6: Susi Business Integration — REPORT

**Date:** 2026-09-11
**Status:** COMPLETE
**Branch:** master
**Next phase:** Phase 7.2-6.5

---

## 1. Objective

Connect the Phase 7.2-5 business licensing layer to the **real local Susi source**
through the existing `susi_helper.exe` subprocess interface, without adopting Susi's
native License → Machine model.

Susi supplies cryptography only (signing, verification, fingerprints). Redemption,
entitlement accumulation and feature union stay owned by the Business Layer.

---

## 2. Architecture Actually Implemented

```
Business Layer (SQLAlchemy)  ->  Susi Security Layer (susi_core)  ->  susi_helper.exe
  LicenseService                  sign_license()                       JSON over stdin/stdout
  RedemptionService               verify_license()                     {"command": ...}
  SusiSecurityService             fingerprint::get_machine_code()
```

`SusiSecurityService` is the only bridge. It shells out to the real
`susi_helper.exe` (built against the vendored `susi_source` via a path dependency)
and exchanges a single JSON command on stdin, receiving a single JSON response on
stdout.

Three commands are supported:

| Command | Purpose |
|---|---|
| `GetMachineCode` | Returns the 64-hex-char machine fingerprint |
| `SignLicense` | Signs a `LicensePayload`, returns a `SignedLicense` |
| `Verify` | Verifies a `SignedLicense` against a supplied public key |

No fake Susi APIs and no replacement crypto are used anywhere.

---

## 3. Files / Components Changed

### Created
- `license_server/app/services/susi_security_service.py` — the Susi bridge (no business logic).
- `license_server/test_validate_route.py` — smoke test for `POST /licenses/validate`.

### Modified
- `susi_helper/src/main.rs`
  - `Command::Verify` now declares and uses the caller-supplied `public_key_pem`.
  - Removed the hardcoded `"PUBLIC_KEY_HERE"` placeholder.
  - Removed DEBUG output that dumped full stdin (including the private key).
- `license_server/app/config.py`
  - Removed embedded (invalid) private/public key PEM defaults.
  - Key material now comes from environment / `.env` only.
- `license_server/app/services/susi_security_service.py`
  - Added `_to_rfc3339()` — naive DB datetimes → RFC3339 with explicit UTC offset.
  - Added `_get_setting()` — supports both `dict` (tests) and `Settings` (app).
  - `print("DEBUG: ...")` replaced with `logging`; no key material logged.
- `license_server/app/main.py`
  - `/licenses/validate` repointed to `RedemptionService` (see Defect 3).
- `license_server/test_key_loader.py` — removed DEBUG prints.
- `PROJECT_STATE.md` — recorded Phase 7.2-6 COMPLETE.

---

## 4. Root Cause: the datetime / RFC3339 issue

**Symptom.** `SignLicense` was rejected with:

```
serde_json error: Error("premature end of input", line: 0, column: 0)
```

**What it was NOT.** Measured stdin byte count was 2205, exactly equal to the Python
string length (2205 chars, pure ASCII). The payload parsed successfully in Python.
Re-running the same bytes through a file redirect reproduced the failure identically.
So this was not truncation, not a stdin-delivery fault, and not an encoding fault.

**Actual cause.** `LicensePayload.created` and `.expires` are
`chrono::DateTime<Utc>` (`susi_source/crates/susi_core/src/license.rs:53,55`).
chrono's RFC3339 parser **requires a timezone offset**. The SQLAlchemy columns
(`Authorization.expires_at`, `License.created_at`) are declared `Column(DateTime)`
without `timezone=True`, so they return **naive** datetimes — and `.isoformat()` on a
naive datetime emits `"2026-09-11T12:02:34"` with no offset.

chrono then fails with `ParseErrorKind::TooShort`, whose `Display` string is literally
`"premature end of input"`. Because it is raised through `D::Error::custom`, serde_json
reports position `0:0` — which is why the error looked like a JSON syntax fault at the
start of the document and sent the initial diagnosis in the wrong direction.

**Proof.** A/B test on the same payload, changing only the date strings:

| `created` / `expires` | Result |
|---|---|
| `"2026-09-11T12:02:34"` (naive — what the code sent) | rejected, `premature end of input` |
| `"2026-09-11T12:02:34Z"` | accepted (rc=0) |
| `"2026-09-11T12:02:34+00:00"` | accepted (rc=0) |

**Fix.** `SusiSecurityService._to_rfc3339()` attaches UTC to naive values (assumed
already-UTC, matching the project's `server_time` convention), normalizes aware values
to UTC, and emits an explicit offset.

---

## 5. Root Cause: the Verify public-key issue

Two coupled defects made verification impossible by construction:

1. `SusiSecurityService.verify_license()` sent a `public_key_pem` field, but
   `Command::Verify` declared only `signed_license`. **Serde ignores unknown fields by
   default**, so the key was silently discarded — no error, no warning.
2. `main.rs` then verified against the hardcoded literal `"PUBLIC_KEY_HERE"`, which can
   never parse as a public key.

Because Defect (§4) failed first, the Verify path was never even reached during the
initial test run, which is why the two appeared unrelated.

**Fix.** `Command::Verify` now declares `public_key_pem: String` and passes it to
`verify_license_from_pem`. Verification now fails loudly if the key is missing rather
than silently using a placeholder.

---

## 6. Additional Defects Found During Acceptance

These were found while adding the route smoke test and were fixed with approval.

**Defect 3 — `/licenses/validate` raised `AttributeError`.**
The 7.2-5 refactor moved validation from `LicenseService` to `RedemptionService` but
`main.py` still called `LicenseService.validate_license`. The commit would have shipped
a broken endpoint not covered by the 7.2-6 acceptance suite. Fixed by repointing the
route to `RedemptionService.validate_license`.

**Defect 4 — `app/main.py` could not be imported at all.**
`main.py` constructs `SusiSecurityService(config.settings)` with a pydantic `Settings`
object, but the service called `self.config.get(...)` — a dict API:

```
AttributeError: 'Settings' object has no attribute 'get'
```

The 7.2-6 suite never caught this because it constructs the service with a plain dict,
so the production wiring was never exercised. Fixed by `_get_setting()`, which supports
both `dict` and `Settings`.

---

## 7. Security Constraints Respected

- No fake Susi APIs; the real vendored `susi_core` is used via `susi_helper.exe`.
- `susi_source` was **not modified**.
- `test_rsa_key.pem` is not embedded in source and is gitignored (`license_server/*.pem`).
- No production private key exists anywhere in source.
- No placeholder public key remains.
- DEBUG output exposing stdin contents, private key material or full JSON commands was
  removed (27 statements in `susi_security_service.py`, plus instrumentation in
  `susi_helper/src/main.rs`).
- Temporary Phase 7.2-6 diagnostic scripts were deleted (25 files).
- No `*.pem`, `*.key` or `target/` artifact is tracked by Git.

---

## 8. Validation Results

All of the following were actually executed:

| Check | Command | Result |
|---|---|---|
| Phase 7.2-6 acceptance | `python test_phase_7_2_6.py` | **8/8 PASSED**, exit 0 |
| Route smoke test | `python test_validate_route.py` | **2/2 PASSED**, exit 0 |
| Release build | `cargo build --release` | **SUCCESS**, 0 errors |
| Whitespace | `git diff --check` | no whitespace errors |

Acceptance coverage (`test_phase_7_2_6.py`):

1. Business redemption succeeds
2. Susi SignedLicense creation
3. Valid client verification
4. Tampered signature rejection
5. Wrong machine rejection
6. Expired license rejection
7. Feature propagation
8. Repeated redemption rejection

The route smoke test was verified to have teeth: temporarily reverting the
`/licenses/validate` fix produced **0/2** with the exact `AttributeError`, and restoring
it produced **2/2**.

---

## 9. Known Limitations

1. **`/licenses/validate` is not tested through the ASGI stack.** `httpx` is not a
   project dependency, so `fastapi.testclient.TestClient` is unavailable. The smoke test
   invokes the route handler directly, which catches the binding regressions above but
   does not exercise routing, serialization or the `response_model`.
2. **No Susi lease enforcement.** `lease_expires` and `lease_grace_period` are always
   `null` in the signed payload. Note that `susi_core`'s own
   `License.lease_duration_hours` defaults to 72 — that struct is simply never
   constructed by this integration, so it has no effect.
3. **`Command::Unknown` is inert.** The variant carries `#[serde(skip)]`, so an
   unrecognized `command` value produces a parse error rather than routing anywhere.
   It also emits a dead-code warning.
4. **`crypto::self` unused-import warning** in `main.rs` (the import is used only under
   `#[cfg(test)]`). Pre-existing; left untouched.
5. **Production requires `SUSI_DEVELOPMENT_PRIVATE_KEY`** via environment or `.env`.
   There is deliberately no embedded default, so signing fails loudly when it is absent.
6. **The test keypair is pinned.** The public key is hardcoded in
   `test_phase_7_2_6.py`. Regenerating `test_rsa_key.pem` would silently invalidate that
   value — `generate_test_key.py` was removed partly for this reason.
7. **`SECRET_KEY` in `config.py` still has a development placeholder default.**
   Pre-existing and outside Phase 7.2-6 scope; not addressed.
8. **`Verify` returns a human-readable string** (`"Valid license: <key>"`) rather than a
   structured payload.

---

## 10. Cleanup Performed

- Deleted 25 temporary diagnostic scripts (`debug_*.py`, `debug_public_key.pem`,
  `derive_public_key.py`, `generate_test_key.py`, `replicate_failure.py`, `run_test.py`,
  `test_public_key*.pem`, `validate_test_key.py`, `verify_key_match.py`, and ad-hoc
  `test_*.py` probe scripts).
- Removed all DEBUG output exposing secrets; replaced with `logging`.
- Added `license_server/*.pem` and `admin_web/` to `.gitignore`.

Not touched (unrelated to this phase): `debug_ocr.py`, `debug_gui.log` (main
application OCR/GUI artifacts), `admin_web/` (a vendored snapshot of the third-party
`sqladmin` package, now gitignored rather than committed).

---

## 11. Next Phase

**Phase 7.2-6.5** — not started. This report does not begin that work.
