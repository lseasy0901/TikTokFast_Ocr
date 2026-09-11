# LiveLens

低延迟直播画面实时提取与 OCR 工具

A Windows desktop viewer for Douyin (抖音) live streams built around a **latest-frame video pipeline**: it always displays the newest decoded frame and deliberately discards older ones, so stream latency does not accumulate over time.

On top of that low-latency base the application adds **region OCR** — you draw a rectangle on the live video and the text inside it is recognized continuously and copied to the clipboard automatically whenever it changes.

The repository also contains a **self-hosted license server** (`license_server/`) and a small Rust bridge (`susi_helper/`) that together provide device-bound, server-authoritative license activation with offline signature verification on the client.

---

## Table of Contents

1. [Key Features](#1-key-features)
2. [Architecture Overview](#2-architecture-overview)
3. [Low-Latency Design](#3-low-latency-design)
4. [OCR, ROI and Clipboard Behaviour](#4-ocr-roi-and-clipboard-behaviour)
5. [Recent Streams and Per-URL Notes](#5-recent-streams-and-per-url-notes)
6. [Licensing Architecture and Business Rules](#6-licensing-architecture-and-business-rules)
7. [Admin Backend](#7-admin-backend)
8. [Requirements](#8-requirements)
9. [Development Setup](#9-development-setup)
10. [Running the Client](#10-running-the-client)
11. [License Server Setup](#11-license-server-setup)
12. [Configuration](#12-configuration)
13. [PyInstaller Build](#13-pyinstaller-build)
14. [Security Notes](#14-security-notes)
15. [Current Project Status](#15-current-project-status)
16. [Roadmap](#16-roadmap)
17. [Disclaimer](#17-disclaimer)
18. [License](#18-license)

---

## 1. Key Features

| Feature | Description |
|---|---|
| **Low-latency live playback** | Direct FFmpeg stream extraction and decode, latest-frame buffer, active frame dropping |
| **Douyin room URL resolution** | Resolves a `live.douyin.com/<room_id>` page to the real FLV playback URL |
| **Region OCR (ROI)** | Draw a rectangle over the live video; only that region is recognized |
| **Automatic clipboard copy** | Recognized text is copied automatically whenever it changes |
| **Recent streams** | Last 10 successful streams, de-duplicated, with a per-URL note field |
| **Live performance readout** | FPS, glass-to-glass latency, resolution, connection state |
| **License activation** | One-time key redemption, device binding, offline signature verification |
| **Expiry gating** | Protected actions are gated while the local authorization is expired |
| **Self-hosted license server** | FastAPI + SQLAlchemy service with a SQLAdmin operations console |

---

## 2. Architecture Overview

```
DouyinLowLatencyViewer/
├── main.py                  # Entry point: QApplication + MainWindow
├── config.yaml              # Runtime configuration (resolution, CUDA, monitoring)
├── requirements.txt         # Client dependencies
│
├── gui/                     # PySide6 user interface
│   ├── main_window.py       # Main window, stream control, OCR wiring, licensing
│   ├── video_widget.py      # Frame painting + ROI overlay
│   ├── activation_dialog.py # License key entry
│   └── expired_dialog.py    # Expired-authorization notice
│
├── stream/                  # Video input pipeline
│   ├── douyin.py            # Room URL -> real stream URL resolution
│   ├── ffmpeg_reader.py     # FFmpeg subprocess reader (low-latency flags)
│   ├── frame_buffer.py      # Single-slot LatestFrameBuffer
│   └── video_info.py        # ffprobe-based stream probing
│
├── ocr/                     # Recognition pipeline
│   ├── worker.py            # OCRWorker (QThread) — independent of video
│   ├── engine.py            # Tesseract wrapper + locator + whitelist
│   ├── preprocess.py        # Binarization / denoising for OCR input
│   ├── roi_manager.py       # Current ROI state
│   ├── roi_selector.py      # Drag-to-select ROI overlay
│   └── clipboard.py         # Clipboard writes with filtering
│
├── utils/                   # Cross-cutting helpers
│   ├── license_manager.py   # Activation, storage, verification orchestration
│   ├── license_client.py    # HTTP client for the activation endpoint
│   ├── license_store.py     # Local license persistence
│   ├── susi_verifier.py     # susi_helper wrapper: machine code + signature check
│   ├── access_status.py     # Expiry countdown and access gating
│   ├── stream_history.py    # Recent streams + per-URL notes
│   └── performance.py       # FPS / latency monitoring
│
├── license_server/          # FastAPI license server (separate deployment)
│   ├── app/                 # Config, models, services, API routes, admin
│   └── .env.example         # Server configuration template
│
├── susi_helper/             # Rust bridge to the Susi core library
├── susi_source/             # Vendored Susi sources (compatibility reference)
├── admin_web/               # Vendored SQLAdmin snapshot (not tracked in Git)
└── runtime/                 # Bundled ffmpeg and Tesseract binaries
```

**Data flow at runtime**

```
Douyin room URL
      │  stream/douyin.py
      ▼
Real stream URL ──► FFmpeg subprocess (rawvideo bgr24 over stdout)
                          │
                          ▼
                LatestFrameBuffer (single slot)
                          │
        ┌─────────────────┴─────────────────┐
        ▼                                   ▼
  GUI paint thread                    OCR worker thread
  (display + ROI overlay)             (crop ROI from source frame,
                                       recognize, copy on change)
```

The two consumers are independent: the OCR worker reads whatever the newest frame happens to be and never feeds anything back into the video path.

---

## 3. Low-Latency Design

The pipeline is built around one rule: **it is always better to drop a frame than to display a stale one.**

**Single-slot `LatestFrameBuffer`** (`stream/frame_buffer.py`)

The buffer holds exactly one frame. A write replaces the previous frame rather than appending to a queue, so a slow consumer can never cause a backlog. There is no `queue.Queue` and no frame history by design.

**Skip-to-latest reading** (`stream/ffmpeg_reader.py`)

After reading a frame, the reader non-blockingly checks how many bytes are already waiting in the FFmpeg stdout pipe (`PeekNamedPipe` on Windows, `select` on Unix). If a full additional frame is available, the current frame is discarded and the newer one is read instead. This repeats up to a safety cap of 30 frames per iteration, so the application always holds the newest frame the pipe can offer instead of draining a backlog.

**Aggressive FFmpeg decode flags**

The FFmpeg invocation includes, among others:

```
-fflags nobuffer+flush_packets   -flags low_delay
-probesize 32768                 -analyzeduration 100000
-thread_queue_size 1             -max_delay 0
-avioflags direct                -flush_packets 1
-pix_fmt bgr24  -f rawvideo  pipe:1
```

Audio and subtitles are dropped (`-an -sn`) since only video is needed. Frames are converted to NumPy through `np.frombuffer` as a zero-copy view — there is no per-frame pixel copy in Python.

**Isolation from the UI**

FFmpeg runs in a child process; reading happens on a dedicated daemon thread; stderr is drained on a second thread so a full stderr pipe can never block decoding. The GUI thread only ever reads the current slot.

**Not used for stream input**

Deliberately excluded from this project:

- Selenium
- Playwright
- browser screenshots
- OBS (no OBS runtime or OBS integration)
- OpenCV `VideoCapture`

Stream input is obtained by resolving the Douyin playback URL and decoding it directly with FFmpeg. Window capture and browser automation are never part of the video path.

**Measured latency**

`utils/performance.py` timestamps capture and display to report a live glass-to-glass latency figure, alongside FPS and resolution, in the status bar.

---

## 4. OCR, ROI and Clipboard Behaviour

**OCR runs as an independent worker.** `ocr/worker.py` implements `OCRWorker` as a `QThread` that loops on its own timer. It reads the newest frame from the buffer and performs recognition without ever blocking, throttling, or feeding back into the video pipeline. If recognition is slow, video playback is unaffected — the worker simply samples a later frame next time.

**Default interval: 300 ms.** `OCRWorker(interval_ms=300)` is the default, and the interval is adjustable at runtime. OCR starts and stops only via the existing OCR button (`开始OCR` / stop); nothing recognizes text until you start it.

**ROI selection**

- `选择识别区域` starts a selection pass; the current frame is shown frozen for drawing.
- Drag to draw the rectangle, **Enter** confirms, **Esc** cancels.
- Cancelling during a re-selection restores the previously confirmed ROI.
- A confirmed ROI stays drawn on the live video as an **overlay that is visual only**. The overlay is never part of the image that gets recognized.
- There is exactly one ROI.

**OCR reads source-frame pixels.** The worker crops the ROI rectangle directly out of the raw frame taken from the buffer, then preprocesses and recognizes that crop. The on-screen overlay is drawn by the video widget and is not involved.

**Recognized character set.** Tesseract is configured with a whitelist limited to English letters and digits:

```
ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789
```

No punctuation, and no spaces or newlines in the final copied text. There is no character-guessing or substitution heuristic — the engine reports what it recognizes, and multiple page-segmentation modes are tried in order.

**Clipboard behaviour**

- Text is copied to the clipboard **automatically whenever the recognized text changes**.
- Unchanged text is not re-copied, so repeated identical results do not churn the clipboard.
- Empty or whitespace-only results are **not** copied — a blank recognition never overwrites whatever you already had on the clipboard.
- A brief confirmation indicator appears in the UI after a successful copy.

---

## 5. Recent Streams and Per-URL Notes

`utils/stream_history.py` maintains a persisted list of the most recent successful streams (up to 10 entries, stored as JSON next to the application).

| Behaviour | Detail |
|---|---|
| Capacity | 10 entries maximum |
| De-duplication | The same URL appears once; reconnecting updates its timestamp instead of adding a row |
| Eviction | Least-recently-used entry is dropped past the limit |
| Notes | Each URL carries a free-text note, editable from the UI via the `备注` button |
| Persistence | Written to disk, so the list survives restarts |
| Clearing | The `清除` button empties the list |

Notes are purely local annotations — they are never sent to the license server, the stream provider, or anywhere else.

---

## 6. Licensing Architecture and Business Rules

### Product rule

**A License Key is a one-time redeemable, time-and-feature entitlement credential.**

| Situation | Result |
|---|---|
| Key is unused and redeemed successfully | The key becomes permanently `REDEEMED` and can never be redeemed again |
| Target device has no authorization | A new Authorization is created, expiring at *server time + key duration* |
| Target device has an **active** authorization | Duration is **accumulated** onto the existing expiry |
| Target device has an **expired** authorization | Expiry **restarts** from authoritative server time + key duration |
| Key already redeemed | Rejected with `already_redeemed`; no second Authorization, no expiry extension |

Features are **unioned** across every key a device has redeemed. Different unused keys may be redeemed on different devices, producing separate Authorizations.

### Server-authoritative, device-bound

The server — not the client — decides the resulting expiry. Timestamps are derived from server time, and the entitlement is bound to a device identifier (a machine code produced by `susi_helper`). A credential is only accepted by the client when the signature is valid **and** the embedded machine code matches the current machine.

### Server-side data model

| Entity | Meaning | Key fields |
|---|---|---|
| `License` | A single issued key | `key_hash`, `duration_days`, `state`, `features`, `redeemed_at`, `authorization_id` |
| `Authorization` | A device's current entitlement | `device_id`, `expires_at`, `state`, `activated_at` |

License keys are stored **only as a SHA-256 hash** (`key_hash`). The plaintext key exists in memory at generation time and is never persisted, so a database dump cannot be replayed as usable keys.

### Signed licenses and offline verification

On successful redemption the server returns a **SignedLicense** — a payload (license identifier, features, expiry, bound machine codes) plus an RSA signature over its bytes.

- Signing uses **RSA PKCS#1 v1.5 with SHA-256**, performed by the Rust Susi core through `susi_helper`.
- The client verifies the signature **locally, with no network access**, using a bundled public key.
- Client verification order is: structural validity → signature → machine binding → expiry. The signed license is only persisted after all four pass.

**Key provisioning.** The signing private key lives on the license server and is supplied through configuration — it is never committed to this repository. Only the **public** key is shipped with the client (bundled into the packaged application). Public-key resolution order on the client is: environment variable → bundled/repository public key → application data directory. For production, a deployment provisions its own keypair and places only the public half in the client build.

### Activation endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/licenses/activate` | Redeem a key, return a SignedLicense |
| `POST` | `/api/v1/licenses/validate` | Validate a key without redeeming |
| `POST` | `/api/v1/admin/licenses` | Create a license (API-key protected) |

### Client storage

The client stores its result at `%APPDATA%\DouyinLowLatencyViewer\license.json` — activation timestamp, device identifier, server URL, and the SignedLicense. On startup the stored license is re-verified **locally**; no server round-trip is required to remain active.

### Susi integration status

Susi is used as the **cryptographic and device-identification layer only** — signing, verification, and machine-code generation, reached through the `susi_helper` bridge.

Susi's own native License→Machine issuance model is **not** used. The entitlement semantics above (one-time redemption, duration accumulation, feature union, server-authoritative expiry) are implemented by this project's own business layer in `license_server/app/services/redemption_service.py`. The vendored `susi_source/` tree is kept as a compatibility reference; it is not the licensing backend.

---

## 7. Admin Backend

The license server exposes a **SQLAdmin** operations console at `/admin`, mounted alongside the JSON API without modifying it.

**Vendored, not installed.** The SQLAdmin snapshot is vendored under `admin_web/sqladmin/`, which is excluded from Git and provisioned separately per deployment. If that directory is absent — for example in a fresh clone — the server **skips mounting `/admin` entirely and starts normally**; the license API is unaffected. The admin console is an optional local operations interface, not a startup requirement.

**Authentication.** Login username is `admin`; the password must equal the configured `ADMIN_API_KEY`, compared in constant time. If `ADMIN_API_KEY` is unset, **all logins fail closed**. Sessions are signed with `SECRET_KEY`.

**Supported operations**

| Operation | Where | Notes |
|---|---|---|
| Generate license keys | `/admin/generate-keys` | Batch generation: duration preset, quantity, feature list |
| Browse / search license keys | License Key list | Shows the key fingerprint, not the plaintext key |
| Inspect a license | License detail | Includes features and the linked authorization |
| Export license list | License Key list | Export enabled |
| Revoke a license | License Key actions | **Unused keys only** — redeemed keys cannot be revoked, and existing device authorizations are not withdrawn |
| Redemption history | `/admin/redeemed-history` | Redeemed keys with their timestamps |
| Inspect authorizations | Authorization list | Device, state, expiry, activation time |
| Per-device redemption history | Authorization detail | The keys that device has redeemed |

**Deliberate restrictions.** Both the License and Authorization views disable create, edit, and delete. Keys are generated by the business layer (which the built-in form cannot do), the license state machine should only be driven by redemption and revocation, and issued records should not be erased for audit reasons. Authorization revocation is not offered — no code path in the current business layer sets a revoked authorization state.

---

## 8. Requirements

**Client**

| Requirement | Notes |
|---|---|
| Windows 10 / 11 | The client targets Windows; the stream reader has partial cross-platform checks but the product is Windows |
| Python 3.10+ | Recent verification was performed on Python 3.14 |
| FFmpeg | `ffmpeg` and `ffprobe` must be available on `PATH` — used for stream decoding and probing |
| Tesseract OCR | Located under `runtime/tesseract/` next to the application, or from a system installation |
| NVIDIA GPU (optional) | Enables CUDA hardware decoding; decoding falls back to CPU when disabled |

Client Python packages (`requirements.txt`): PySide6, opencv-python, numpy, pyyaml, requests, psutil.

> **Note:** the OCR engine additionally imports `pytesseract`, which is not currently declared in `requirements.txt`. Install it explicitly (`pip install pytesseract`) or OCR will be unavailable.

**License Server** (`license_server/requirements.txt`): fastapi, uvicorn, sqlalchemy, pydantic, pydantic-settings, python-multipart, wtforms, jinja2, itsdangerous.

**Build tooling**: PyInstaller, and a Rust toolchain if you need to build `susi_helper` from source.

---

## 9. Development Setup

```bash
# 1. Clone
git clone <repository-url>
cd DouyinLowLatencyViewer

# 2. Install client dependencies
pip install -r requirements.txt

# 3. Make FFmpeg available on PATH
ffmpeg -version     # must succeed

# 4. Make Tesseract available
#    Either install it system-wide, or place a portable build in runtime/tesseract/
```

---

## 10. Running the Client

```bash
python main.py
```

**Typical workflow**

1. Paste a Douyin live room URL (`https://live.douyin.com/<room_id>`) into the URL field and press **连接**.
2. The resolved stream starts playing with the live FPS / latency / resolution readout in the status bar.
3. Press **选择识别区域**, drag a rectangle over the text you want, and press **Enter** to confirm (**Esc** cancels).
4. Press **开始OCR** to begin continuous recognition; recognized text is copied to the clipboard automatically whenever it changes.
5. Use the **备注** button to attach a note to the current URL, and **清除** to empty the recent list.

Activation is available from the **激活许可证** button; when a local authorization has expired, protected actions are gated and an expiry notice is shown.

---

## 11. License Server Setup

The license server is a separate FastAPI application. It is **not deployed anywhere yet** — the steps below are for running it locally or on your own host.

**Run locally**

```bash
cd license_server

# 1. Create your configuration from the template
cp .env.example .env      # then edit it

# 2. Run the server
python -m uvicorn main:app --app-dir app --host 127.0.0.1 --port 8000
```

The `--app-dir app` flag is required: the server's modules are laid out so that `app/` is on `sys.path` and the application module is named `main`.

**Pointing the client at a server**

The client resolves its activation endpoint from the `DLV_LICENSE_SERVER_URL` environment variable, falling back to a local development default. This lets one build target different deployments without code changes.

**Admin console**

With the vendored SQLAdmin snapshot provisioned under `admin_web/`, the operations console is served at `/admin`. Set `ADMIN_API_KEY` before starting — logging in is impossible while it is unset.

---

## 12. Configuration

**Client — `config.yaml`**

```yaml
stream:
  width: 1920       # output frame width
  height: 1080      # output frame height (must match; auto-detected if omitted)

ffmpeg:
  cuda: true        # enable CUDA hardware decoding
  low_delay: true   # low-latency decode flags

performance:
  monitor_interval: 1   # performance sampling interval, seconds
```

**License server — `.env`** (see `license_server/.env.example`)

| Variable | Purpose |
|---|---|
| `SECRET_KEY` | Signs admin session cookies — change in production |
| `ADMIN_API_KEY` | Admin password and API key for license creation |
| `DATABASE_URL` | SQLAlchemy database URL |
| `DEBUG` | When true, serves interactive API docs; they are disabled otherwise |
| `API_PREFIX` | API route prefix (default `/api/v1`) |
| `SUSI_DEVELOPMENT_PRIVATE_KEY_FILE` | Path to the signing private key PEM — **preferred form** |
| `SUSI_DEVELOPMENT_PRIVATE_KEY` | Inline signing key; may use escaped `\n` |
| `SUSI_HELPER_PATH` | Override for the `susi_helper` executable location |

Precedence is environment variable → `.env` → built-in default. The server resolves its `.env` from its own location rather than the process working directory, so it can be started by a process manager or service unit without settings silently falling back to defaults.

Use the **file** form for the signing key. A multi-line PEM does not survive a bare `.env` value: unquoted it is silently truncated to its first line, and a value pasted from PowerShell may be UTF-16, whose NUL bytes a PEM parser rejects. The server reports these cases explicitly instead of failing with a misleading parse error.

---

## 13. PyInstaller Build

The build is driven by `douyin_viewer.spec` and must be run **from the repository root**, because the spec resolves its inputs relative to the working directory.

```bash
pyinstaller --noconfirm --clean douyin_viewer.spec
```

Output is a one-directory bundle at `dist/DouyinLowLatencyViewer/`.

**Bundled artifacts**

| Artifact | Why it is bundled |
|---|---|
| `license_public_key.pem` | Lets the client verify SignedLicenses offline |
| `susi_helper` executable | Required to compute a machine code and verify signatures |
| `config.yaml` | Runtime configuration |

**The build fails loudly rather than silently.** Two of those inputs are deliberately not tracked in Git — the public key is provisioned per deployment, and the helper is a compiled Rust binary. PyInstaller's default behaviour is to warn about a missing data file and produce a broken executable anyway, which would surface as "activation does not work" on a user's machine long after the build. The spec therefore verifies both inputs up front and **aborts the build** with an explicit list of what is missing and how to provision it.

---

## 14. Security Notes

- **Private keys never enter this repository.** The signing private key is supplied to the license server through configuration and must live outside the source tree. Only the public key is distributed with the client, and it is public by design.
- **Keys are stored hashed.** The license database keeps only a SHA-256 fingerprint of each key, so a database compromise does not yield usable keys.
- **Verification is cryptographic.** A client accepts a license only if the signature validates against the provisioned public key *and* the bound machine code matches — an edited local license file is rejected.
- **Admin access fails closed.** With no `ADMIN_API_KEY` configured, the admin login rejects every attempt rather than allowing an unprotected console.
- **Interactive API docs are development-only.** The FastAPI documentation routes are mounted only when `DEBUG` is enabled; in production they are absent so the API surface is not advertised.
- **The server binds to the loopback interface** by default and never enables auto-reload, which is a development convenience that would restart processes in production.
- **Do not commit** `.env` files, signing keys, license databases, or build output. The repository's `.gitignore` already excludes these categories.
- **Verify your deployment's own keys.** Any development keypair shipped with the source tree is for local testing only and must not be used to issue real licenses.

---

## 15. Current Project Status

**Completed**

- Low-latency FFmpeg direct-stream playback with a single-slot latest-frame buffer
- Douyin room URL resolution and stream probing
- ROI selection with visual-only overlay
- OCR worker with a 300 ms default interval and automatic clipboard copy on change
- Recent stream history with per-URL notes
- Full licensing stack: FastAPI server, one-time redemption, duration accumulation, feature union, server-authoritative expiry, device binding, RSA-signed licenses with offline client verification
- SQLAdmin operations console with batch key generation, license inspection, revocation, and redemption history
- Expiry gating in the client

**Phase 7.2-8 production hardening** — complete, committed, and verified:

| Area | Work |
|---|---|
| Client/server portability | Removed machine-specific absolute paths; OS-aware helper resolution |
| Configuration | `.env` and configuration paths resolved independently of the working directory |
| Database | SQLite WAL journal mode and a 30-second busy timeout for concurrent access |
| Startup hardening | Interactive API docs gated on `DEBUG`; loopback binding; auto-reload never enabled |
| Build safety | Fail-loud build guard aborting on missing bundled artifacts |
| Test coverage | Regression suites for portability, configuration, concurrency, startup, and the build guard |
| Key rotation | Test fixtures repaired to use a single matching test keypair, with an added assertion that the production public key rejects foreign signatures |

**Verified end-to-end locally.** A production-signed license was issued and activated against a locally running server using a production-keyed client build; the client reported active, re-verified the license locally after a restart with no server round-trip, and a repeated redemption of the same key was correctly rejected without creating a second authorization or extending the expiry. The production public key was confirmed to reject signatures made with a different key.

**Not yet done**

- **The license server has not been deployed to any cloud host.** All verification so far has been local. Cloud deployment, production key custody on the server, and re-verification against a remote endpoint remain outstanding.
- Authorization revocation is not implemented.
- The feature set is not yet used to gate functionality beyond expiry.

---

## 16. Roadmap

- Deploy the license server to a cloud host with production key custody on the server side
- Re-run end-to-end activation against the remote endpoint
- Move the license database off SQLite for multi-instance deployment
- Implement authorization revocation end-to-end
- Tie license features to actual client capabilities
- Expand OCR language and charset coverage (currently English letters and digits only)

---

## 17. Disclaimer

- This project is **not affiliated with, endorsed by, or associated with Douyin, ByteDance, or any of their affiliates.**
- It is intended for **personal, educational, and research use**. You are responsible for complying with the terms of service of any platform you use it with, and with any applicable laws in your jurisdiction.
- It does not circumvent, bypass, or break any access control, and it does not redistribute or re-host any stream content. It resolves and plays a stream that the platform already serves to the requesting client.
- OCR output is produced by Tesseract and is **best-effort**. It can be wrong, incomplete, or empty; it must not be relied upon for any decision where an error would cause harm.
- Use at your own risk. The authors accept no liability for any loss or damage arising from use of this software.

---

## 18. License

This repository does not currently include a license file, so **no license is granted** for the project's own code — all rights are reserved by the author by default. If you intend to use, modify, or distribute this project, contact the author to agree on terms.

Third-party components vendored or bundled with this repository remain under their own upstream terms:

| Component | Notes |
|---|---|
| SQLAdmin snapshot (`admin_web/sqladmin/`) | BSD-3-Clause |
| Susi sources (`susi_source/`) | Vendored for compatibility reference; carries its own upstream terms |
| FFmpeg binaries (`runtime/ffmpeg/`) | Subject to FFmpeg's own license terms |
| Tesseract OCR (`runtime/tesseract/`) | Subject to Tesseract's own license terms |

Anyone redistributing a build of this application is responsible for complying with the licensing terms of these bundled components.
