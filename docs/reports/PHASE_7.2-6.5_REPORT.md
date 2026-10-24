# Phase 7.2-6.5: Real Client License Activation — REPORT

**Date:** 2026-09-11
**Status:** COMPLETE
**Branch:** master
**Baseline:** `181917e424100e712f0a519d21230405bb5e63ae` (Phase 7.2-6)
**Next phase:** Phase 7.2-7

---

## 1. Objective

Implement the real client-side activation flow:

```
User enters license key
  -> client obtains its own machine code (Susi fingerprint)
  -> client sends activation/redeem request to the License Server
  -> Business Layer validates and redeems the one-time key
  -> server returns a Susi SignedLicense
  -> client stores the signed license locally
  -> client verifies it LOCALLY (signature, machine binding, expiry)
  -> UI reflects the activation status
  -> subsequent startup loads and re-verifies the stored license
```

---

## 2. Architecture Implemented

```
  GUI (PySide6, GUI thread)
    gui/main_window.py         header status + 激活许可证 button + startup restore
    gui/activation_dialog.py   key entry; owns the background worker
         |
         |  background thread (never blocks the GUI)
         v
  utils/license_manager.py     THE orchestration + local verification
         |            |
         |            +--> utils/susi_verifier.py   susi_helper.exe: machine code + verify
         |            +--> utils/license_store.py   atomic local persistence
         |
         +--> utils/license_client.py  real HTTP POST -> License Server
                                          |
                                          v
                              license_server  (Business Layer, authoritative)
                                redemption_service.redeem_license()
                                susi_security_service.create_signed_license()
                                          |
                                          v
                                    susi_helper.exe SignLicense
```

**Division of responsibility (unchanged by this phase):**

| Concern | Owner |
|---|---|
| One-time key redemption, Authorization, duration accumulation, `max_hosts`, key/authorization state | **Business Layer (server)** |
| Signed license artifact, cryptographic signature verification, machine binding, expiry verification, local security verification | **Susi** |
| Orchestration, local re-verification before trusting, persistence | `LicenseManager` (client) |

The client implements **no** duration or key-consumption logic. Its expiry comes
solely from the `expires` field inside the server-signed artifact; whether a key
is still redeemable is decided only by the server.

---

## 3. Files Changed

### Created (client)

| File | Purpose |
|---|---|
| `utils/susi_verifier.py` | Client-side `susi_helper.exe` bridge: `get_machine_code()`, `verify_signature()`. Public key only. |
| `utils/license_client.py` | HTTP client for `POST /api/v1/licenses/activate`. Maps server failures to typed reasons. |
| `utils/license_store.py` | Atomic (tmp + `os.replace`) local persistence in the app data directory. |
| `utils/license_manager.py` | `LicenseManager`: activation flow, local verification, startup restore, `LicenseStatus`/`LicenseResult`. |
| `gui/activation_dialog.py` | `ActivationDialog` + `LicenseWorker` (threading + Qt queued signal). |
| `license_server/test_phase_7_2_6_5.py` | 55-check acceptance suite over the real path. |

### Modified

| File | Change |
|---|---|
| `gui/main_window.py` | Wires `LicenseManager`; button always enabled and opens the real dialog; background startup restore; maps results onto the header. Replaced the placeholder handler that reset the trial. |
| `utils/access_status.py` | Added server-driven license state (`set_license_active` / `set_license_inactive` / `clear_license_state`). License state overrides trial display; the "expired" dialog is no longer triggered by license states. |
| `license_server/app/schemas.py` | Defect 5 fix (below). |
| `license_server/app/services/redemption_service.py` | Defect 6 fix + Defect 7 fix (below). |
| `.gitignore` | Ignore the provisioned client public key file. |

### Not changed
`susi_helper/src/main.rs` (no change needed this phase), `susi_source/` (upstream,
untouched), and all video/FFmpeg/OCR/ROI/threading code.

---

## 4. Server / Client Contract (as actually implemented)

`POST {base}/api/v1/licenses/activate`

```json
{"license_key": "<16-64 chars>", "device_id": "<64-hex machine code>"}
```

| Response | Meaning | Client status |
|---|---|---|
| `200` `{... "signed_license": "<json string>"}` | redeemed, artifact issued | local verification decides |
| `400` `{"detail": "invalid_key"}` | unknown key | `INVALID` |
| `400` `{"detail": "already_redeemed"}` | one-time rule enforced | `INVALID` |
| `400` `{"detail": "revoked"}` | key revoked | `INVALID` |
| connection error / timeout | server unreachable | `ACTIVATION_FAILED` |
| `5xx` / malformed body | server fault | `ACTIVATION_FAILED` |

**Device identity:** the client uses its own Susi machine code as `device_id`
(64 hex chars, exactly the schema maximum). This is what binds an authorization
to the machine.

**Local verification order** (`LicenseManager._verify_artifact`) — the order is
the security property:

1. structure parses (`license_data` + `signature`)
2. **signature valid** via the public key — only now are payload fields trusted
3. `machine_codes` contains this machine
4. `expires` is in the future

**Persistence rule:** the artifact is written to disk **only after** local
verification passes. Every failure path (unreachable server, invalid key,
tampered artifact, wrong machine) leaves the local license byte-identical.

Stored record (`%APPDATA%/DouyinLowLatencyViewer/license.json`) contains only
the signed artifact plus non-secret metadata (`device_id`, `server_url`,
`activated_at`). No private key, no plaintext license key, no locally computed
expiry.

---

## 5. Defects Found by Exercising the Real Path

The Phase 7.2-6 suite could not detect any of these: it constructs
`SusiSecurityService` with a plain dict and calls services directly in **one
session**, so the HTTP route, the response schema and cross-request state were
never exercised. Running the real path surfaced seven defects.

### Defect 5 — `POST /admin/licenses` returned HTTP 500

`LicenseResponse.authorization_id` was a required `int`, but a freshly created
license has no authorization yet:

```
ResponseValidationError: 1 validation error
  {'loc': ('response', 'authorization_id'), 'msg': 'Input should be a valid integer', 'input': None}
```

There was no way to obtain a license key through the API at all.
**Fix:** `authorization_id: Optional[int] = None`. A license genuinely has no
authorization before redemption; no business rule changed.

### Defect 6 — signing failed for any real (cross-request) redemption

```
RuntimeError: Failed to create SignedLicense: 'License' object has no attribute 'license_key'
```

`models.License` deliberately has **no** `license_key` column (only `key_hash`);
the plaintext key exists only in the creating request's memory. Creation and
redemption are always different HTTP requests, so `license.license_key` was
never available — the deployment could not sign a single license.

**Fix:** the signed payload's `license_key` identifier is now the license's
`key_hash` — the canonical, non-secret identifier that is always available. The
plaintext key is still never persisted, and no business rule changed.
*(Impact: the artifact's internal identifier is a hash rather than the plaintext
key. Nothing consumes it except an informational human-readable string.)*

### Defect 7 — first redemption granted **2× the key duration**

`Authorization.state` defaults to `ACTIVE`. `find_or_create_authorization()`
creates a new authorization already expiring at `server_time + duration`, and
`redeem_license()` STEP 4 then found it `ACTIVE` and took the accumulation
branch, adding the duration a second time.

Measured before the fix (real server, 30-day key):

| | reported `remaining_seconds` |
|---|---|
| expected (CLAUDE.md: "No Authorization: `expires_at = server_time + duration`") | 2,592,000 |
| actual | **5,184,000** (60 days) |
| second 30-day key on the same device | 7,775,999 (≈90 days) |

Every new device silently received double entitlement, and the inflation then
compounded through the legitimate accumulation rule.

**Fix (approved by the user, as it touches business logic):**
`find_or_create_authorization()` now returns `(authorization, created)`, and
`redeem_license()` skips the accumulation branch when `created` is true — a
brand-new authorization already carries its full duration. Accumulation for
genuinely pre-existing authorizations and restart-on-expiry are unchanged and
are now covered by regression checks (test 12).

### Defects 1–4
Recorded in `PHASE_7.2-6_REPORT.md` (RFC3339 datetime rejection, silently
dropped Verify public key, broken `/licenses/validate` route, un-importable
`app/main.py`).

---

## 6. Client UI States

| Required state | Where it appears |
|---|---|
| 未激活 | activation dialog ("当前状态：未激活"); header keeps the trial display |
| 已激活 | header: `已激活 · 剩余 N天` (or `即将到期 · 剩余 N小时` inside 24h) |
| 激活失败 | activation dialog, with the reason; header untouched |
| 许可证已过期 | header: `许可证已过期` |
| 许可证无效 | header: `许可证无效` |
| 许可证不属于当前设备 | header: `许可证不属于当前设备` |

The 激活许可证 button is usable at any time (previously it was disabled unless
the trial had already expired, and its handler merely reset the trial).

---

## 7. Validation Results

All of the following were actually executed on 2026-09-11.

| Check | Command | Result |
|---|---|---|
| **Phase 7.2-6.5 acceptance** | `python test_phase_7_2_6_5.py` | **55/55 checks, exit 0** |
| Phase 7.2-6 baseline (regression) | `python test_phase_7_2_6.py` | **8/8 PASSED**, exit 0 |
| Route smoke test (regression) | `python test_validate_route.py` | **2/2 PASSED**, exit 0 |
| Release build | `cargo build --release` | SUCCESS, 0 errors (2 pre-existing warnings) |
| Whitespace | `git diff --check` | no whitespace errors |
| Application startup | offscreen `QApplication` + `MainWindow()` | constructs; button enabled; store path outside the source tree; startup restore returns `not_activated` |
| GUI activation path | real server, offscreen dialog | **16/16 checks** — dialog → worker thread → queued signal → activated; rejection path; all 5 header states |

### What the acceptance suite actually exercises

Real uvicorn server on a free localhost port, real temp SQLite database, real
SQLAlchemy business layer, **real `susi_helper.exe`** for both signing and
verification, real `requests` HTTP, real on-disk store. No Susi API is faked;
the only substitutions are deployment locations. Tests 7 and 8 use the real
signer to construct artifacts the server would not naturally issue (expired,
foreign-machine).

### The 10 required error cases

| # | Case | Result |
|---|---|---|
| 1 | valid unused key succeeds | PASS — expiry equals the server-signed field |
| 2 | same key again | PASS — `already_redeemed`, local state unchanged |
| 3 | invalid key | PASS — nothing written to disk |
| 4 | revoked key | PASS — nothing written to disk |
| 5 | restart on same machine | PASS — still activated; expiry == signed `expires` |
| 6 | stored license tampered | PASS — local verification fails |
| 7 | expired stored license | PASS — reported expired/inactive |
| 8 | wrong-machine license | PASS — local verification fails |
| 9 | concurrent activation | PASS — exactly one succeeds, one redeemed row server-side |
| 10 | server unreachable | PASS — graceful failure, stored license byte-identical |

Test 11 asserts client-side security properties; test 12 guards duration
semantics (no double-count, accumulation and restart preserved).

---

## 8. Security Audit

| Requirement | Status |
|---|---|
| No private signing key reaches the client | **Verified.** No client module references private key material; only `SUSI_PUBLIC_KEY` / `SUSI_DEVELOPMENT_PUBLIC_KEY` and `license_public_key.pem` are read. |
| No test private key bundled | **Verified.** `test_rsa_key.pem` is gitignored (`license_server/*.pem`) and not tracked (`git ls-files` clean). |
| No secret written to logs | **Verified.** Client logging records URLs, statuses and reason codes only. `git grep` finds no logging statement in the client modules that includes `license_key`. |
| No full activation request logged | **Verified.** `license_client` logs only the target URL and the failure reason; uvicorn logs request lines, not bodies. |
| Stored license holds only the intended artifact | **Verified.** Test 1 asserts the stored record contains neither `PRIVATE KEY` nor the plaintext license key. |
| No key material tracked by Git | **Verified.** `git ls-files` matches no `*.pem` / `*.key` / `target/`. The only `PRIVATE KEY` matches are `...`-truncated illustrations in the 7.2-6 report and an upstream assertion in unmodified `susi_source`. |
| `susi_source` unmodified | **Verified.** `git diff --stat -- susi_source` is empty. |

---

## 9. Known Limitations

1. **The client public key must be provisioned.** `LicenseManager` resolves it
   from `SUSI_PUBLIC_KEY` / `SUSI_DEVELOPMENT_PUBLIC_KEY`, then
   `<project root>/license_public_key.pem`, then `<app data>/license_public_key.pem`.
   With none present, the app logs a warning and activation cannot complete local
   verification. This is deliberate: no key material is compiled into the client.
2. **`license_key` in the activate response is `null`** for the same reason as
   Defect 6 (`_format_activation_response` still uses
   `hasattr(license, 'license_key')`). The client ignores that field; the signed
   payload carries the hash instead. Left untouched as out of scope.
3. **`LicenseCreate` has no `features` field**, so licenses created over HTTP
   always carry an empty feature set. Feature propagation is not exercised
   end-to-end. Pre-existing API gap.
4. **Lease is not enforced.** `lease_expires` / `lease_grace_period` are still
   `null`.
5. **The activation endpoint is unauthenticated.** Anyone who can reach it can
   attempt a redemption. Rate limiting and transport security are out of scope
   for this phase.
6. **Test 12 runs a second short-lived server**, because device identity is the
   real machine fingerprint and the shared server accumulates authorizations
   across cases — there would be no "brand new device" left.
7. **Test 9's concurrency check has inherent timing.** A barrier makes the two
   calls overlap in practice, and a separate deterministic check (holding the
   manager's lock) pins the in-flight guard exactly.
8. **The local store does not self-heal.** An expired/invalid stored license is
   reported but not deleted, so diagnostics remain possible; the user re-activates
   through the dialog.
9. **`SECRET_KEY` in `license_server/app/config.py`** still has a development
   placeholder default. Pre-existing, out of scope.

---

## 10. Phase 7.2-6 Baseline Integrity

The 7.2-6 baseline is **intact**: `test_phase_7_2_6.py` still 8/8 (exit 0) and
`test_validate_route.py` still 2/2 (exit 0) **after** the Defects 5–7 fixes.
Defect 7 changed business behaviour, so it was re-verified against both the
7.2-6 suite and the new duration regression checks.

---

## 11. Next Phase

**Phase 7.2-7** — not started. This report does not begin that work.
