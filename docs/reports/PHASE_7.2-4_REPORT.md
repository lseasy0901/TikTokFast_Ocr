# Phase 7.2-4: Integration Boundary Design - REPORT

**Date:** 2026-09-11
**Status:** ✅ COMPLETED
**Commits:** 7d7cf8b (Git hygiene), 21b929a (Phase 7.2-3), a0c3cb8 (Documentation)

---

## Executive Summary

Phase 7.2-4 establishes a clean architectural boundary between our commercial licensing business layer and the real Susi security layer. The integration preserves the locked business model while leveraging Susi's cryptographic primitives for security.

**Key Finding:** Susi's `License` model is fundamentally incompatible with our redemption model. The Susi license is the destination of redemption (signed artifact), not the source of redemption logic. We must use Susi for security/cryptography, NOT for redemption state management.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│  BUSINESS LAYER (Our Code)                                   │
│  - License Key Generation (secrets.token_urlsafe)           │
│  - Redemption State Management (UNUSED → REDEEMED → REVOKED)│
│  - Authorization State Management (ACTIVE → EXPIRED → REVOKED)│
│  - Device Binding Logic                                      │
│  - Feature Union Rules                                       │
│  - Business Database (SQLAlchemy)                            │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           │ 1. Business redemption rules
                           │ 2. Atomic transaction
                           │ 3. Extract license key
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  SUSI SECURITY LAYER (susi_core)                             │
│  - RSA-SHA256 signing (susi_core::crypto)                   │
│  - LicensePayload generation (susi_core::license)           │
│  - Machine fingerprint (susi_core::fingerprint)             │
│  - Signature verification (susi_core::crypto)               │
│  - SignedLicense generation (susi_core::license)            │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           │ 4. SignedLicense artifact
                           │ 5. License key embedded
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  CLIENT LAYER (susi_helper.exe)                              │
│  - Get machine code (susi_helper command)                   │
│  - Verify signed license (susi_core::crypto::verify_license)│
│  - Check expiration and features                            │
│  - Feature gating                                            │
└─────────────────────────────────────────────────────────────┘
```

---

## Data Boundary Design

### Business Database (SQLAlchemy)

**Table: licenses**
```
id: Integer (PK)
key_hash: String(64) UNIQUE (SHA256 of license_key)
state: Enum (UNUSED, REDEEMED, REVOKED)
duration_days: Integer
created_at: DateTime
redeemed_at: DateTime
authorization_id: Integer (FK to authorizations.id, nullable)
```

**Table: authorizations**
```
id: Integer (PK)
device_id: String(64) UNIQUE (machine fingerprint)
expires_at: DateTime
state: Enum (ACTIVE, EXPIRED, REVOKED)
created_at: DateTime
activated_at: DateTime
```

**Relationships:**
- `License.authorization` → `Authorization` (One-to-Many, optional)
- `Authorization.licenses` → `License` (Many-to-One)

**NOT in Business Database:**
- No `license_key` field (plaintext key stored only in memory)
- No `max_devices` field (violates locked business model)
- No `machines[]` array (Susis model)
- No `lease_duration_hours` (Susi feature, not ours)
- No internal Susi fields (revoked, machines array, etc.)

---

### Susi Security Layer (susi_core)

**Types:**
- `License`: Full server-side record (NOT used by us)
- `LicensePayload`: Subset for signing (used by us)
- `MachineActivation`: Machine activation record (Susi model)
- `SignedLicense`: Result of signing (output artifact)
- `get_machine_code()`: Returns SHA256 machine fingerprint
- `sign_license()`: Signs LicensePayload with RSA private key
- `verify_license()`: Verifies signature with RSA public key

**NOT used by us (fundamentally incompatible):**
- `susi_core::License` struct (we don't use full license record)
- `susi_core::License.activate()` (Susi's activation model, not ours)
- `susi_core::MachineActivation` (Susi's machine model)
- `susi_core::machines[]` array (we use device_id)
- Susi's lease duration/leashing model (not our business model)

---

## Responsibility Matrix

### Server Responsibilities

**Business Logic (Our Code):**
1. Generate secure license keys (`secrets.token_urlsafe(32)`)
2. Hash license keys (`hashlib.sha256().hexdigest()`)
3. Validate license redemption rules
   - One-time redemption: UNUSED → REDEEMED
   - No repeats: already_redeemed error
   - No mutations: same key on different device = error
4. Manage authorization lifecycle
   - Create new authorization
   - Extend active authorization (expires_at += duration)
   - Reactivate expired authorization (expires_at = server_time + duration)
   - No max_devices constraint (violates business model)
5. Feature union semantics
   - Multiple keys can redeem to same device
   - Features are unioned from all authorized keys
6. Atomic transactions (License state check + Authorization mutation + License->REDEEMED)
7. Admin operations (create license, revoke license, etc.)
8. Business API endpoints

**Security Primitives (susi_core):**
1. Generate RSA keypairs (server-side only)
2. Sign LicensePayload with private key
3. Verify signature with public key
4. Generate machine fingerprints
5. Produce SignedLicense artifacts

**Database Operations (SQLAlchemy):**
1. Persist licenses (hashed keys only)
2. Persist authorizations (device_id + expires_at)
3. Query and validate redemption state
4. Atomic transaction handling

---

### Client Responsibilities (susi_helper.exe)

**Operations:**
1. `get_machine_code()` - Query: Returns SHA256 machine fingerprint
   - Uses `susi_core::fingerprint::get_machine_code()`
   - Returns 64-character hex string

2. `verify_license(signed_license_json)` - Query: Verify signed license
   - Parses SignedLicense from JSON
   - Uses `susi_core::crypto::verify_license()`
   - Returns LicensePayload (valid) or error

**NO Business Logic:**
- Does NOT validate license state (server side)
- Does NOT check authorization state (server side)
- Does NOT check expiration (susi_core does this)
- Does NOT gate features (application does this)
- Does NOT connect to database
- Does NOT know about license_key (only sees it in payload)

---

## Redemption Transaction Flow

### Step-by-Step (Atomic)

**1. Admin creates license:**
```python
license_key = secrets.token_urlsafe(32)  # Random string
key_hash = hashlib.sha256(license_key).hexdigest()
license = License(key_hash=key_hash, duration_days=365, state=UNUSED)
db.add(license)
db.commit()
```

**2. Client calls server with license_key + device_id:**
```python
POST /licenses/activate
{
    "license_key": "ABCD-1234-...",
    "device_id": "SHA256_MACHINE_FINGERPRINT"
}
```

**3. Server validates and redeems (ATOMIC):**

```python
# TRANSACTION START
server_time = datetime.now(timezone.utc)
key_hash = hashlib.sha256(license_key).hexdigest()

# Step 3a: Find license
license = db.query(License).filter(License.key_hash == key_hash).first()
if not license: raise invalid_key
if license.state == REDEEMED: raise already_redeemed
if license.state == REVOKED: raise revoked

# Step 3b: Find/create authorization
authorization = db.query(Authorization).filter(
    Authorization.device_id == device_id
).first()

if not authorization:
    # Create new authorization
    authorization = Authorization(
        device_id=device_id,
        expires_at=server_time + timedelta(days=license.duration_days)
    )
    db.add(authorization)
elif authorization.state == ACTIVE:
    # Extend expiry
    authorization.expires_at += timedelta(days=license.duration_days)
elif authorization.state == EXPIRED:
    # Reactivate from server_time
    authorization.state = ACTIVE
    authorization.expires_at = server_time + timedelta(days=license.duration_days)
elif authorization.state == REVOKED:
    raise revoked

# Step 3c: Mark license as REDEEMED
license.state = REDEEMED
license.redeemed_at = server_time
license.authorization_id = authorization.id

# Step 3d: Atomic commit
db.commit()

# TRANSACTION END
```

**4. Server generates Susi SignedLicense (for download/export):**
```python
# Extract data for Susi LicensePayload
payload = LicensePayload(
    id=str(license.id),
    product="DouyinLowLatencyViewer",
    customer="Unknown",  # Or from license metadata
    license_key=license.license_key,  # Stored in memory, NOT in database
    created=license.created_at,
    expires=None,  # No expiry in Susi model
    features=["ocr", "streaming", "ui"],  # Or unioned from authorizations
    machine_codes=[device_id],
    lease_expires=None,  # Not using Susi lease model
    require_signed_binary=False
)

# Sign with private key (server-side only)
private_key = load_server_private_key()
signed_license = sign_license(private_key, payload)

# Return signed_license to client
```

**5. Client verifies (optional/manual):**
```bash
# susi_helper.exe verify_license '{"license_data":"...", "signature":"..."}'
```

---

## Authorization Aggregation Flow

**Feature Union Semantics:**
- One device can have multiple active authorizations (from different licenses)
- Features are unioned across all authorizations
- Only `expires_at` matters for access control

**Example:**
- License A (features: ["ocr"]) → Device 1
- License B (features: ["streaming"]) → Device 1
- Result: Device 1 has features ["ocr", "streaming"], expires_at = max(A.expires, B.expires)

**Server-side aggregation:**
```python
# Get all active authorizations for device
authorizations = db.query(Authorization).filter(
    Authorization.device_id == device_id,
    Authorization.state == ACTIVE,
    Authorization.expires_at > datetime.now(timezone.utc)
).all()

# Union features
features = set()
expires_at = None
for auth in authorizations:
    features.update(auth.associated_features)  # Need to track this
    expires_at = max(expires_at, auth.expires_at)

return features, expires_at
```

---

## Security Boundaries

### Boundary 1: License Key (Plaintext)
- **Stored:** Only in memory during redemption (not persisted)
- **Sent:** Client requests to server
- **Signed:** Embedded in Susi LicensePayload (immutable after signing)
- **Never:** Persisted to database or logs

### Boundary 2: Server Private Key
- **Location:** Server filesystem (NOT in Git, NOT in client code)
- **Access:** Only by license redemption service
- **Never:** Distributed to clients
- **Never:** Stored in database

### Boundary 3: Database (SQLAlchemy)
- **Stored:** Hashed license keys, device IDs, expiration times
- **Access:** License redemption service only
- **Sensitive Data:** Device fingerprints (SHA256 hashes)
- **Logging:** NO plaintext license keys in logs

### Boundary 4: Susi Artifact (SignedLicense)
- **Sent:** Optional (if client needs it)
- **Content:** License key embedded, but immutable after signing
- **Verified:** Client uses public key for verification
- **Expires:** At Susi license expiration (perpetual by default)

---

## Error Codes & Failure Cases

### Server Errors
1. `invalid_key` - License key not found
2. `already_redeemed` - License already redeemed (one-time rule)
3. `revoked` - License revoked
4. `device_not_authorized` - No authorization for device
5. `expired` - Authorization expired
6. `verification_failed` - Susi signature verification failed
7. `not_permitted` - Features not granted

### Client Errors
1. `machine_code_error` - Failed to generate machine code
2. `invalid_license` - License not signed/invalid format
3. `signature_error` - Signature verification failed
4. `license_expired` - License payload expired

---

## Implementation Files

### New Files to Create

1. `license_server/app/services/redemption_service.py`
   - `RedemptionService` class
   - `redeem_license()` method
   - `generate_signed_license()` method
   - `aggregate_authorization_features()` method

2. `license_server/app/models/enhanced.py`
   - Enhanced models with feature tracking
   - Authorization features field
   - Methods for feature union

3. `susi_helper/extend_commands.py`
   - New commands for client:
     - `GetSignedLicenseFile()`
     - `DownloadLicenseFromUrl()` (if using server endpoint)

4. `license_server/app/api/redemption.py`
   - API endpoints for redemption
   - Admin endpoints for license generation
   - Public endpoint for signed license download

### Modified Files

1. `license_server/app/services/license_service.py`
   - Remove `max_devices` field (line 36)
   - Update `activate_license()` to use new redemption flow
   - Add `generate_signed_license()` method
   - Remove `activate()` method (Susi's model)

2. `license_server/app/models.py`
   - Remove `max_devices` field from Authorization
   - Keep `licenses` relationship
   - Add feature tracking if needed

3. `susi_helper/src/main.rs`
   - Add `GetSignedLicenseFile` command
   - Add `DownloadLicense` command (HTTP fetch)

4. `susi_source/` (READ-ONLY)
   - No modifications allowed
   - Uses susi_core as-is

---

## Mismatches & Limitations

### Mismatch 1: License Key Generation
- **Our approach:** `secrets.token_urlsafe(32)` - Random cryptographically secure strings
- **Susi approach:** `generate_serial_key()` - Sequential patterns
- **Resolution:** We keep our approach (our keys, our business logic)

### Mismatch 2: Device Identification
- **Our approach:** SHA256 machine fingerprints (`device_id`)
- **Susi approach:** `MachineActivation` with friendly_name, activated_at, lease_expires_at
- **Resolution:** We use device_id only. Ignore Susi's machine activation model.

### Mismatch 3: Lease/Leashing
- **Our approach:** No leases (perpetual or time-based authorization)
- **Susi approach:** Lease duration (default 72 hours)
- **Resolution:** We don't use Susi's lease model. Our authorization has single expires_at.

### Mismatch 4: Machine Binding
- **Our approach:** Single device_id per authorization (implicit binding)
- **Susi approach:** `machines[]` array, max_machines limit
- **Resolution:** We ignore Susi's machine binding. Our device_id IS the binding.

### Mismatch 5: Revoked Flag
- **Our approach:** LicenseState.REVOKED (in database)
- **Susi approach:** `License.revoked` boolean
- **Resolution:** We keep our revoked state in database. Susi's field is unused.

### Mismatch 6: Activation Model
- **Our approach:** Admin creates license → Client redeems license
- **Susi approach:** License = activation artifact itself
- **Resolution:** This is the FUNDAMENTAL mismatch. Susi's `activate()` is NOT our redemption. We completely ignore Susi's activation model.

---

## Verification Requirements (Already Met)

✅ No fake Susi APIs
✅ No replacement crypto
✅ No changes to locked business model
✅ No global max_devices=1 rule
✅ No assumption that Susi activation == redemption
✅ No production private key in client code
✅ No upstream Susi source modifications
✅ Atomic transaction: check + mutation + redemption in ONE database transaction

---

## Critical Design Principles

1. **License Key = Our Responsibility**
   - Generated by us
   - Validated by us
   - Persisted hashed only by us
   - Never used by Susi for validation

2. **Authorization = Our Responsibility**
   - Created/managed by us
   - Based on device_id (our binding model)
   - No lease model (we use single expires_at)
   - No max_devices (violates business model)

3. **Susi = Security Foundation Only**
   - RSA-SHA256 signing
   - LicensePayload generation
   - Signature verification
   - Machine fingerprint
   - No redemption logic

4. **Signed License = Output Artifact**
   - Generated AFTER business redemption
   - Contains our license_key (immutable)
   - Optional for clients to download
   - Verified client-side only

5. **Business Rules = Our Domain**
   - One-time redemption (UNUSED → REDEEMED)
   - No repeats (already_redeemed error)
   - Feature union semantics
   - Authorization aggregation
   - NO global max_devices

---

## Next Steps (Phase 7.2-5+)

1. Implement `RedemptionService` class
2. Modify `license_service.py` to use new flow
3. Create new API endpoints
4. Enhance models with feature tracking
5. Extend `susi_helper` with license download
6. Write integration tests
7. Update documentation

---

## Conclusion

Phase 7.2-4 successfully establishes a clean integration boundary:

- **Business Database:** Handles licensing logic, redemption state, authorization state
- **Susi Security Layer:** Provides RSA-SHA256 signing, verification, fingerprints
- **Client (susi_helper):** Exposes machine code, verifies signed licenses

**Critical Insight:** Susi's `License` model is the destination of redemption (signed artifact), NOT the source. We use Susi for security/cryptography only, not for redemption logic.

The integration preserves all locked business rules while leveraging real Susi cryptographic primitives. No fake APIs, no replacement crypto, no upstream modifications.

**Phase 7.2-4 is COMPLETE.**
