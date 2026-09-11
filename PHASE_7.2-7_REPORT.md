# Phase 7.2-7: SQLAdmin Admin Web MVP - IMPLEMENTATION REPORT

**Phase:** 7.2-7 (Admin Web / SQLAdmin)
**Status:** COMPLETED
**Baseline at phase start:** `67dbee59169e36cfadf7a061c4a4a3091d08b121` — `feat: complete phase 7.2-6.5 real client license activation`
**Date:** 2026-09-11

---

## Executive Summary

Phase 7.2-7 delivers a **local-only administration UI** over the existing Business
Licensing layer: generate License Keys, list/detail them, view redemption history,
revoke, and inspect device authorizations.

The phase adds **no business logic**. Every licensing mutation is performed by the
existing `services.license_service.LicenseService`; `schemas.LicenseCreate` still
supplies the validation bounds. No table was added, no existing rule was changed, and
the desktop client was not touched.

The UI is built on the **unmodified upstream SQLAdmin 0.31.1 snapshot** already present
at `admin_web/sqladmin` (BSD-3-Clause, gitignored), integrated through SQLAdmin's public
extension surface only.

---

## Agreed V1 Scope

Recorded here as the authoritative definition of done for this phase.

| Scope item | Decision |
|---|---|
| Product | **Dropped.** No Product table, no Product management. The product is already the product string in the Susi payload; `models.License` and the existing services stay authoritative. |
| Revoke | **UNUSED licenses only.** No Authorization revocation, and no attempt to invalidate an already-redeemed device entitlement. |
| Authorization | Read-only inspection. No mutation path exists in the Business Layer and none was added. |
| Deployment | SQLAdmin is **local-only administration**. Not a customer-facing surface, not a licensing backend. |
| Duration presets | **1 / 30 / 180 / 365 days**, enforced by a server-side whitelist. |

UI wording was made explicit so revoke semantics cannot be misread:

- `UNUSED` → 未使用 · 可吊销
- `REDEEMED` → 已兑换 · 不可吊销（吊销不会收回既有授权）
- `REVOKED` → 已吊销

---

## Implementation Complete

### Files Created

| File | Purpose |
|---|---|
| `license_server/app/admin/__init__.py` | Registers the vendored snapshot on `sys.path`, self-checks provenance and version, exposes `setup_admin(app)` |
| `license_server/app/admin/auth.py` | `AdminAuth(AuthenticationBackend)` |
| `license_server/app/admin/views.py` | `LicenseAdmin`, `AuthorizationAdmin`, `GenerateKeysView`, `RedeemedHistoryView` |
| `license_server/app/templates/admin/generate_keys.html` | Generation page; extends `sqladmin/layout.html` |

### Files Modified (additive only)

| File | Change |
|---|---|
| `license_server/app/main.py` | 10 lines appended: `from admin import setup_admin` + `setup_admin(app)`. Mounts at `/admin`. No existing route altered. |
| `license_server/requirements.txt` | SQLAdmin's runtime dependencies only (`wtforms`, `jinja2`, `itsdangerous`). SQLAdmin itself is **not** installed from PyPI. |

### Files Deliberately NOT Modified

Verified by an empty `git diff --stat`:

- `license_server/app/models.py`
- `license_server/app/schemas.py`
- `license_server/app/services/*`
- the desktop client (`gui/`, `utils/`)
- `susi_source/`, `susi_helper/`

No Product table was added. `admin_web/` remains gitignored (`.gitignore:47`).

---

## Architecture Verification

### Snapshot integrity (enforced, not just asserted)

`admin_web/sqladmin` is the unmodified upstream SQLAdmin 0.31.1 tree. No file in it was
edited; every source file still carries its original download mtime. Bytecode caches
(`__pycache__`) created by importing it during testing were removed, leaving 165 pristine
source files.

`app/admin/__init__.py` makes this a runtime property rather than a convention: it
prepends the snapshot to `sys.path`, then verifies that

1. `sqladmin.__file__` resolves **inside** `admin_web/sqladmin`, and
2. `sqladmin.__version__` is exactly `0.31.1`

and raises `RuntimeError` otherwise. A stray PyPI `sqladmin` therefore cannot be mixed in
silently — the server refuses to start rather than serve a different copy.

### Public extension surface only

Used: `Admin`, `ModelView`, `BaseView`, `@action`, `@expose`, `AuthenticationBackend`,
`Secret`, `Flash`, `StaticValuesFilter`.

No private attribute was reached into, and no snapshot file was patched.

### Business Layer remains authoritative

`LicenseAdmin.revoke` and `GenerateKeysView` both open a session and call
`LicenseService`. The views contain no licensing rules of their own — they decide only
*which* rows the operator asked for and how to word the result.

`LicenseAdmin` sets `can_create = can_edit = can_delete = False`:

- **create** — `key_hash` is produced by the Business Layer; SQLAdmin's built-in form
  would submit `NULL`.
- **edit** — the key state machine should be driven only by redemption and revocation.
- **delete** — issued-key history should not be erasable.

---

## Design Decisions Forced by the Framework

### 1. One ModelView per model

`ModelViewMeta` assigns `identity` unconditionally from the model name
(`sqladmin/models.py:111`), overwriting any class attribute. A second `ModelView` on
`License` collides on both the route path and the `admin:list` endpoint name.

"Redeemed history" is therefore a `BaseView` that 302-redirects to
`/admin/license/list?state=REDEEMED` (reusing SQLAdmin's existing pagination, sorting and
export), and the Authorization detail page exposes the `licenses` relationship as
per-device history.

### 2. Plaintext keys are never persisted

`models.License` stores only `key_hash`. The backend therefore **cannot** list, search or
re-display a plaintext key — that is the existing security model, not a UI limitation.
Generation goes through `GenerateKeysView`, and the plaintext is displayed exactly once
via `Secret.reveal_once` (stashed on `request.state`, never in the session, never logged),
with `Secret.apply_no_store_headers` applied to the response.

### 3. Operator identity

The project has no user store. `AdminAuth` uses a single identity `admin`, with the
existing `config.settings.ADMIN_API_KEY` as the password, compared via
`secrets.compare_digest` for both fields. Login **fails closed** if `ADMIN_API_KEY` is
empty. `is_accessible()` is enforced on the built-in list/detail routes and on
`@action`/`@expose`, so all three entry paths share one control.

---

## Defect Found During Verification

### Upstream flash messages were silently dropped

**This is a genuine defect, found by testing — not a test artifact.** It was diagnosed to
source and proven empirically before being worked around.

SQLAdmin 0.31.1's `flash()` writes:

```python
if "_messages" not in request.session:
    request.session["_messages"] = []
request.session["_messages"].append({...})   # in-place mutation
```

Starlette 1.6.0's `Session` sets `modified` only in `__setitem__`, `__delitem__`, `pop`,
`clear`, `update` and `setdefault`. `SessionMiddleware` emits `Set-Cookie` only when
`session.modified` is true.

**Consequence:** when a message was already pending at the start of a request, the newly
queued message was never written back to the cookie and the operator saw nothing —
including the "已兑换，未吊销，吊销不会收回既有授权" warning, i.e. precisely the
message that must not be lost.

**Evidence:** with a message already pending, the second revoke response carried
`Set-Cookie: False`, and decoding the session cookie showed only the stale message
(`_messages` length 1 instead of 2).

**Resolution:** worked around in `views._persist_flashes()` by re-assigning the list, which
trips `__setitem__` and marks the session modified. **The snapshot is not patched** —
the fix lives entirely in project code and uses only public behaviour.

### Redirect status after an action

`views._back()` returns **303 (See Other)** rather than Starlette's default 307, so the
browser re-issues `GET` for the result page instead of replaying the action URL.

---

## Tests Actually Executed

An HTTP-level admin smoke suite was run against a real uvicorn server
(`127.0.0.1`, port 8123) backed by a **throwaway SQLite database** — never the
development database. It was driven with `requests`, and assertions were made against
both HTTP responses and the database file.

### Result: 39/39 checks PASSED

| # | Area | Checks |
|---|---|---|
| 1 | Unauthenticated access | All four entry points redirect to login |
| 2 | Login | Bad password rejected (400), bad username rejected (400), valid accepted |
| 3 | Authenticated pages | License list, Authorization list, Generate page all 200; redeemed-history 302 → `?state=REDEEMED` |
| 4 | Key generation | Row created via real `LicenseService`; `key_hash == sha256(plaintext)`; plaintext absent from every column; `no-store` present |
| 5 | Duration presets | Generator offers exactly `1/30/180/365`; `0`, `400`, `abc`, empty and `45` all rejected with no rows written |
| 6 | Revoke | UNUSED → REVOKED through `LicenseService`; re-revoke is a no-op with an info flash |
| 7 | Post-revoke redemption | Revoked key fails real redemption: `400 {"detail":"revoked"}` |
| 8 | REDEEMED protection | Left unchanged; non-withdrawal wording rendered |
| 9 | State filter | `StaticValuesFilter` over all three states; semantics text rendered |
| 10 | Authorization detail | Related licenses render |
| 11 | Server log | No tracebacks |

### Other checks

- `git diff --check` — no whitespace errors.
- Scope verification — `git diff --stat` over `models.py`, `schemas.py` and `services/` is
  empty.

### Not verified

- No browser was driven; the suite is HTTP-level only. Client-side `select` behaviour is
  unverified (the server-side whitelist is verified, which is the security-relevant half).
- No load, concurrency or multi-operator testing was performed.

---

## Security Considerations

- **No private signing key** is present in the admin path; key generation only mints and
  hashes an opaque key.
- **Plaintext keys** are shown once, marked `no-store`, and never persisted.
- **Login fails closed** when `ADMIN_API_KEY` is unset.
- **No secret in logs** — the smoke suite asserts a traceback-free log.
- **`admin_web/` is gitignored** and contains no project secrets.
- **Local-only by design** — the admin has no customer-facing exposure and no CSRF
  hardening was added, consistent with its local-only deployment assumption.

---

## Business Logic Preservation

Confirmed unchanged:

- one-time redeemability; a redeemed key can never be redeemed again
- duration accumulation onto an existing active Authorization
- feature union on redemption
- restart-from-authoritative-server-time when the target Authorization has expired
- the Susi-native License→Machine model was **not** adopted; the project's own
  entitlement model remains authoritative
- the desktop client is untouched

Revocation is deliberately narrow: it can only stop an **unused** key from ever being
redeemed. It cannot and does not withdraw an entitlement that a device already holds.

---

## Out of Scope (NOT implemented)

- Product management / Product table
- Authorization revocation or entitlement withdrawal
- Redemption/validation event log (a genuinely immutable log needs a new table —
  "redeemed history" here is derived from `License` rows and is not an event log)
- Per-operator accounts, roles and permissions
- CSRF hardening
- Any customer-facing exposure of the admin UI

---

## Phase Result

**Phase 7.2-7 COMPLETE.** An operator can generate, list, inspect and revoke License Keys,
and inspect device authorizations, through a local admin UI mounted at `/admin` — with
every mutation performed by the existing Business Layer, without adding a Product table,
and without changing any existing business rule.

---

# Addendum 7.2-7a: Batch License Key Generation

**Added after the 7.2-7 result above.** Everything in the preceding report still holds;
this section covers only the batch-generation increment.

## What changed

The administrator can now generate **N keys in one submission** by choosing a duration from
the existing whitelist and entering a quantity. Single-key generation is no longer a
separate code path — `quantity=1` is the same batch path with a batch size of one.

| File | Change |
|---|---|
| `license_server/app/admin/views.py` | `MAX_BATCH_QUANTITY`, `_parse_int()`, `_create_license_batch()`; `GenerateKeysView` accepts `quantity` |
| `license_server/app/services/license_service.py` | `create_license(..., commit=True)` — `commit=False` flushes instead of committing |
| `license_server/app/templates/admin/generate_keys.html` | Quantity input + inline one-time result panel with "复制全部" |
| `license_server/test_phase_7_2_7_batch.py` | **new** — 43-check focused suite |

`admin_web/`, the Susi source, the desktop client, `models.py` and `schemas.py` were not touched.

## Exact quantity limit

**1 – 1000 per submission.** `MAX_BATCH_QUANTITY = 1000` in `app/admin/views.py`.

Enforced **server-side**: `quantity` is parsed with `_parse_int()`, and anything that is not
an integer in `[1, 1000]` is rejected before any database work. The form's `max` attribute is
a convenience for the operator, not the control. A 1000-key batch was executed and verified
(all 1000 keys distinct).

## Atomicity

`create_license(commit=False)` only **flushes**; it does not end the transaction. The view
loops, then commits once:

```python
for _ in range(quantity):
    license_obj = service.create_license(license_data, features=..., commit=False)
    keys.append(license_obj.license_key)
db.commit()          # one transaction for the whole batch
# on any exception: db.rollback() and return no keys at all
```

A failure anywhere in the batch therefore leaves **zero** rows. This was verified by forcing
a real failure mid-batch — `generate_secure_key` patched to return a colliding value, producing
an `IntegrityError` on the 4th of 5 inserts — and asserting that the rows already flushed by
the first three inserts were gone too (`0` rows remaining).

`commit=True` remains the default, so every existing caller of `create_license`
(`POST /api/v1/admin/licenses`) behaves exactly as before.

## Result presentation — intentional change

The previous single-key path rendered the plaintext through SQLAdmin's `Secret.reveal_once`
modal, which carries **one** value. A batch needs N values, so the result is now an inline
read-only `<textarea>` (one key per line) inside the same SQLAdmin layout, showing:

- the number generated
- all plaintext keys from this generation
- a warning that plaintext is shown only now
- a **"复制全部"** button (`navigator.clipboard`, with a `execCommand` fallback)

The `no-store` headers are unchanged, and the plaintext still lives only in the request's
memory and this one response body — never the session, never a cookie, never the database.
`quantity=1` uses this same panel, so there is exactly one result renderer.

## Verification actually performed

`license_server/test_phase_7_2_7_batch.py` — **43/43 PASSED, 0 failed, 0 skipped**
(run from `license_server/`; throwaway SQLite databases, never the dev database):

| Required case | Result |
|---|---|
| 1. quantity=1 → exactly 1 key | ✅ 1 key shown, 1 row created |
| 2. quantity=10 → exactly 10 keys | ✅ 10 keys shown, 10 rows created |
| 3. all 10 keys unique | ✅ 10 distinct plaintext, 10 distinct `key_hash` |
| 4. all rows contain hashes only | ✅ each `key_hash == sha256(its plaintext)`; no plaintext in any column |
| 5. all rows use selected duration | ✅ all `duration_days == 180` |
| 6. all rows start UNUSED | ✅ all `UNUSED` |
| 7. quantity=0 rejected | ✅ rejected, no rows |
| 8. negative quantity rejected | ✅ rejected, no rows |
| 9. non-integer quantity rejected | ✅ rejected, no rows |
| 10. excessive quantity rejected | ✅ `1001` rejected, no rows |
| 11. invalid duration rejected | ✅ `0`/`45`/`400`/`abc`/empty all rejected in batch mode |
| 12. rollback leaves zero partial rows | ✅ deferred write + forced `IntegrityError`, 0 rows |
| 13. single-key generation still works | ✅ and the key redeems end to end (HTTP 200) |
| 14. revoke behaviour unchanged | ✅ batch-generated key → `REVOKED` |
| 15. redemption behaviour unchanged | ✅ `REDEEMED`, `remaining_seconds == 2592000`, `signed_license` present |

Plus the boundary case: **quantity=1000 accepted**, all 1000 keys distinct and in the existing
43-char format.

The Phase 7.2-7 admin smoke suite was re-run: **39/39 PASSED**.

## Security review

| Check | Verified how |
|---|---|
| Plaintext not persisted | Asserted for every one of the 10 rows that no plaintext appears in any column; the schema has no plaintext column |
| Plaintext not added to ORM/database fields | `models.py` untouched; plaintext remains an unmapped in-memory attribute set after flush |
| No accidental logging of plaintext | **None of the 1016 generated plaintext keys appeared anywhere in the captured server log**; also no tracebacks |
| Batch cannot bypass the duration whitelist | `duration_days` is checked against `DURATION_PRESETS` before any DB work, regardless of quantity |
| Batch cannot create Authorization records | Authorization count asserted unchanged, and every new row has `authorization_id IS NULL` |
| Batch size bounded | `MAX_BATCH_QUANTITY = 1000`, server-side, with `1001` verified rejected |

## Issue found while testing (pre-existing — reported, not fixed)

While exercising case 13/15 the redemption call **timed out at 10 s**. Investigation showed
the cause is environmental, not a regression:

- `SUSI_DEVELOPMENT_PRIVATE_KEY` is **not** defined in `license_server/.env` (only `SECRET_KEY`,
  `ADMIN_API_KEY`, `DATABASE_URL`, `DEBUG`, `API_PREFIX`, `DEFAULT_MAX_DEVICES`).
- Without it, `POST /api/v1/licenses/activate` **does not fail fast**: it hung past **90 s**
  and the server returned **HTTP 500 with no traceback in the log**.
- With the key supplied, the identical call returns **200 in ~0.15 s**.
- `susi_helper.exe` itself is not the cause — `GetMachineCode` returns in 0.08 s, and
  `SignLicense` with an empty or null key returns an error in 0.03 s, fast in every case.

Redemption is explicitly out of scope for this addendum ("do NOT change the existing business
redemption rules"), so this was **reported, not modified**. It is worth a separate look.

To avoid falsely passing, the suite reports redemption cases as **SKIPPED** (never as passes)
when no signing key is available; in the run above the key was supplied, so all ran.

## Scope

Deliberately **not** implemented: host release, change-machine, payment, order flows, or any
change to redemption, Authorization semantics, hashing/storage rules, or the duration whitelist.
