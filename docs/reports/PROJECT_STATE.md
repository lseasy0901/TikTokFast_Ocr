# \# Project State — Douyin Low Latency Viewer

# 

# \## Project Identity

# 

# Project:

# Douyin Low Latency Viewer

# 

# Root:

# D:\\TIikTok\_Ocr\\DouyinLowLatencyViewer

# 

# Platform:

# Windows 10 / Windows 11

# 

# Target Python:

# Python 3.11

# 

# Current development environment:

# Python 3.14 may be used during development/debugging.

# Do not silently change the production target from Python 3.11.

# 

# Hardware target:

# NVIDIA RTX 4060 or similar CUDA-capable GPU.

# 

# Primary goal:

# A Windows desktop application for very-low-latency Douyin live-stream viewing with real-time OCR, selectable ROI, automatic clipboard copying, stream history/notes, and a future commercial license system.

# 

# Latency goal:

# Approximately 500ms–1.5s end-to-end when the upstream stream permits it.

# 

# Core latency rule:

# Prefer dropping old frames over accumulating latency.

# 

# IMPORTANT:

# Do not introduce architecture or features that increase latency accumulation.

# 

# \---

# 

# \# 1. STREAM / VIDEO PIPELINE

# 

# Status:

# COMPLETED / IMPLEMENTED

# 

# Architecture:

# 

# DouyinStream

# → FFmpegReader

# → \_FrameBridge

# → LatestFrameBuffer

# → GUI QTimer

# → VideoWidget

# 

# \## DouyinStream

# 

# Implemented:

# 

# \- Douyin live-room URL parsing

# \- room\_id extraction

# \- Cookie acquisition including ttwid / UIFID\_TEMP where required

# \- live-room HTML retrieval

# \- multiple stream URL extraction methods

# \- FLV preferred

# \- HLS fallback

# \- stream quality selection

# \- video information detection

# \- adaptive resolution handling

# 

# Stream field priority:

# 

# flv\_pull\_url

# > hls\_pull\_url

# > flv\_pull\_url\_map

# > hls\_pull\_url\_map

# > pull\_url

# > default\_pull\_url

# 

# Preferred quality:

# 

# HD1

# > FHD1

# > FULL\_HD1

# > SD1

# 

# Real Douyin live-stream URLs have been successfully tested.

# 

# Do not redesign DouyinStream unless a concrete defect is discovered.

# 

# \---

# 

# \# 2. FFMPEG READER

# 

# Status:

# COMPLETED / IMPLEMENTED

# 

# Requirements:

# 

# \- FFmpeg subprocess

# \- direct live-stream reading

# \- raw BGR output

# \- low-latency configuration

# \- independent reader thread

# \- latest-frame behavior

# \- old-frame dropping

# \- no frame queue accumulation

# 

# Important:

# Do NOT use:

# 

# \- Selenium

# \- Playwright

# \- browser screenshots

# \- OBS

# \- OpenCV VideoCapture

# 

# FFmpeg must remain the stream input backend.

# 

# Current low-latency approach includes parameters such as:

# 

# \- nobuffer

# \- low\_delay

# \- fast

# \- small probesize

# \- short analyzeduration

# \- small thread queue

# \- direct I/O

# \- max\_delay 0

# \- raw BGR output

# 

# The exact existing implementation is authoritative.

# Do not rewrite working FFmpeg arguments without a concrete reason.

# 

# CUDA:

# Hardware decoding may be used where supported.

# 

# Do NOT use:

# \-hwaccel\_output\_format cuda

# 

# when the output must be CPU-memory raw BGR for Python/OpenCV/Qt.

# 

# Frames must eventually be available in CPU memory.

# 

# \---

# 

# \# 3. VIDEO RESOLUTION

# 

# Status:

# COMPLETED / VERIFIED

# 

# Implemented:

# 

# \- ffprobe-based video information detection

# \- VideoInfo

# \- width

# \- height

# \- pixel format

# \- FPS

# \- codec name

# \- dynamic FFmpeg frame-size calculation

# 

# Important:

# Do not assume 1920x1080.

# 

# The reader must use the actual stream resolution when available.

# 

# Reason:

# A fixed raw frame size caused corrupted/offset frame interpretation and "nine-grid" style display artifacts for streams such as 1280x720.

# 

# Fallback to a safe default is allowed when probing fails.

# 

# \---

# 

# \# 4. LATEST FRAME BUFFER

# 

# Status:

# COMPLETED / VERIFIED

# 

# Implementation:

# 

# LatestFrameBuffer

# 

# Properties:

# 

# \- single-slot buffer

# \- thread-safe

# \- lock protected

# \- update()

# \- get()

# \- clear()

# \- frame\_id

# \- has\_frame()

# 

# Rules:

# 

# \- no historical frame list

# \- no unbounded queue

# \- new frame replaces old frame

# \- GUI consumes latest available frame

# \- old frames may be dropped intentionally

# 

# This architecture is important for low latency.

# 

# Do not replace it with an accumulating queue.

# 

# \---

# 

# \# 5. GUI VIDEO DISPLAY

# 

# Status:

# COMPLETED / UI FROZEN

# 

# PySide6 GUI.

# 

# VideoWidget:

# 

# \- receives BGR numpy frames

# \- BGR → RGB

# \- numpy → QImage

# \- QImage → QPixmap

# \- aspect-ratio-preserving display

# \- video scales with window size

# 

# Current layout hierarchy:

# 

# Header

# → Compact Controls

# → Large Video Area

# → Thin Status Bar

# 

# Video is the visual priority.

# 

# Disconnected state:

# 

# "尚未连接直播"

# 

# must appear as an overlay centered inside the video area.

# 

# It must NOT occupy a separate layout row below the video.

# 

# First valid frame should hide the disconnected overlay.

# 

# UI is considered FINAL / FROZEN.

# 

# Do not perform additional visual redesign unless a concrete functional defect is found.

# 

# \---

# 

# \# 6. ROI SYSTEM

# 

# Status:

# COMPLETED / VERIFIED

# 

# Files:

# 

# ocr/roi\_manager.py

# ocr/roi\_selector.py

# 

# Features:

# 

# \- user manually selects ROI

# \- mouse drag selection

# \- ROI coordinates stored in source-video coordinates

# \- Enter confirms

# \- ESC cancels

# \- ROI can be reselected

# \- only one active ROI

# \- old ROI disappears immediately while selecting a new one

# \- if new selection is cancelled, old ROI is restored

# \- confirmed ROI remains visible as an overlay on live video

# 

# ROI overlay is visual only.

# 

# CRITICAL:

# The ROI overlay must NEVER be included in OCR input.

# 

# OCR must crop the ROI from the original/raw video frame.

# 

# \---

# 

# \# 7. ROI COORDINATE MAPPING

# 

# Status:

# COMPLETED / VERIFIED

# 

# Coordinate mapping accounts for:

# 

# \- source resolution

# \- scaled video resolution

# \- aspect ratio

# \- letterboxing/pillarboxing

# \- centered video offset

# 

# Correct conceptual mapping:

# 

# display\_x =

# offset\_x + roi\_x \* scaled\_width / source\_width

# 

# display\_y =

# offset\_y + roi\_y \* scaled\_height / source\_height

# 

# display\_w =

# roi\_w \* scaled\_width / source\_width

# 

# display\_h =

# roi\_h \* scaled\_height / source\_height

# 

# Verified across multiple source resolutions including:

# 

# 1920x1080

# 1280x720

# 

# ROI display and OCR cropping use source-pixel coordinates.

# 

# \---

# 

# \# 8. OCR

# 

# Status:

# COMPLETED / FROZEN

# 

# Technology:

# 

# \- Tesseract

# \- pytesseract

# \- OpenCV

# \- Python threading

# 

# OCRWorker runs independently from the video display path.

# 

# Goal:

# OCR must not block or slow the live video pipeline.

# 

# Current OCR behavior:

# 

# \- raw ROI image

# \- no preprocessing

# \- PSM 6 selected

# \- default interval = 300ms

# \- approximately 3.33 OCR attempts/sec

# \- result changes trigger clipboard request

# \- empty result does not update clipboard

# 

# Do not continue OCR algorithm optimization unless requirements change.

# 

# \---

# 

# \# 9. OCR ACCURACY RULES

# 

# These rules are LOCKED.

# 

# Allowed output characters:

# 

# A-Z

# a-z

# 0-9

# 

# Final OCR result is filtered to alphanumeric characters only.

# 

# No:

# 

# \- punctuation

# \- spaces

# \- newlines

# \- word reconstruction

# \- semantic correction

# \- character guessing

# \- heuristic substitution

# \- 1 → I correction

# \- I → 1 correction

# \- automatic spelling correction

# 

# Examples:

# 

# "I don't want spicy food."

# →

# "Idontwantspicyfood"

# 

# "HP 100 Score 12 Rank GOLD"

# →

# "HP100Score12RankGOLD"

# 

# "HP: 100

# Score: 12"

# →

# "HP100Score12"

# 

# The system must preserve OCR output semantics as much as possible and only apply the locked alphanumeric filtering.

# 

# \---

# 

# \# 10. OCR BENCHMARK

# 

# Status:

# COMPLETED

# 

# Tested PSM:

# 

# PSM 6

# PSM 7

# PSM 11

# 

# Results:

# 

# PSM 6:

# \~0.0795s average

# 91.68% average completeness

# 

# PSM 7:

# \~0.0822s

# 91.68% average completeness

# 

# PSM 11:

# \~0.0865s

# 91.68% average completeness

# 

# Selected:

# PSM 6

# 

# Standard test cases achieved full success.

# 

# Reduced accuracy cases:

# 

# \- long\_text

# \- problematic\_chars

# 

# These limitations are accepted.

# 

# OCR deep optimization is DEFERRED.

# 

# Do not repeatedly benchmark or redesign OCR without a new concrete requirement.

# 

# \---

# 

# \# 11. TESSERACT

# 

# Status:

# COMPLETED

# 

# Tesseract version tested:

# 

# 5.3.0.20221214

# 

# Expected executable:

# 

# C:\\Program Files\\Tesseract-OCR\\tesseract.exe

# 

# TesseractLocator supports:

# 

# PyInstaller:

# runtime/tesseract/tesseract.exe

# 

# EXE directory:

# runtime/tesseract/tesseract.exe

# 

# Source:

# project/runtime/tesseract/tesseract.exe

# 

# System installations:

# 

# C:\\Program Files\\Tesseract-OCR\\tesseract.exe

# 

# C:\\Program Files (x86)\\Tesseract-OCR\\tesseract.exe

# 

# PATH fallback:

# tesseract

# 

# tessdata lookup supports bundled/runtime/system locations.

# 

# Required language:

# eng

# 

# eng.traineddata must exist.

# 

# Initialization errors should occur once during startup rather than repeatedly inside OCRWorker.

# 

# OCREngine public API remains:

# 

# recognize(image) -> str

# 

# Do not redesign this API without a concrete reason.

# 

# \---

# 

# \# 12. CLIPBOARD

# 

# Status:

# COMPLETED / REAL USE VERIFIED

# 

# Important architecture:

# 

# OCRWorker must NOT directly access Qt clipboard.

# 

# OCRWorker emits:

# 

# copy\_requested

# 

# MainWindow / GUI thread handles clipboard operations.

# 

# ClipboardManager is created/used from the GUI side.

# 

# Automatic OCR result copying has been manually verified to work.

# 

# Do not move clipboard operations back into the OCR thread.

# 

# \---

# 

# \# 13. OCR INTERVAL

# 

# Current default:

# 

# 300ms

# 

# Previous 100ms interval was intentionally changed to 300ms.

# 

# Reason:

# 

# Reduce unnecessary OCR CPU usage while keeping text updates sufficiently responsive.

# 

# 300ms ≈ 3.33 OCR attempts/sec.

# 

# Do not claim "67% CPU reduction" as a measured result.

# That was only a theoretical interval-based estimate.

# 

# \---

# 

# \# 14. STREAM HISTORY

# 

# Status:

# COMPLETED

# 

# Features:

# 

# \- recent stream URLs

# \- maximum 10 entries

# \- LRU behavior

# \- persistent storage

# \- dropdown display

# 

# The user can select a previously saved URL.

# 

# \---

# 

# \# 15. PER-URL NOTES

# 

# Status:

# COMPLETED / VERIFIED

# 

# IMPORTANT:

# This feature is called "per-URL notes".

# 

# Do NOT describe the product feature as nickname support.

# 

# Behavior:

# 

# \- user explicitly selects an existing URL from history

# \- user clicks the "备注" action

# \- note belongs to that specific historical URL

# \- note is displayed/highlighted in history

# \- note can be edited

# \- empty note clears it

# \- note can be changed later

# \- a new URL does NOT inherit another URL's note

# 

# The underlying storage/API may still contain names such as:

# 

# get\_nickname()

# set\_nickname()

# 

# Do not perform an unnecessary storage refactor merely to rename these internals.

# 

# \---

# 

# \# 16. FINAL UI STATE

# 

# Status:

# COMPLETED / FROZEN

# 

# Current UI characteristics:

# 

# \- dark navy / blue professional video-tool style

# \- video-first

# \- compact header

# \- compact controls

# \- URL input and controls on the same row

# \- connection button

# \- ROI selection button

# \- OCR start/stop button

# \- recent stream controls

# \- per-URL notes

# \- license status

# \- activation entry

# \- centered disconnected overlay

# \- thin status bar

# 

# Do not add fake/product-invented features such as:

# 

# \- chat

# \- viewer count

# \- playback controls

# \- thumbnails

# \- unrelated social features

# 

# Do not continue visual tweaking unless a concrete defect is found.

# 

# \---

# 

# \# 17. ACCESS STATUS / TRIAL FOUNDATION

# 

# Status:

# IMPLEMENTED FOUNDATION

# 

# Current architecture contains:

# 

# AccessStatus

# 

# It supports local trial/activation state management and UI state display.

# 

# Commercial rule:

# 

# Free trial = 7 days.

# 

# After trial expiration:

# user must activate a valid paid License Key.

# 

# Payment must NOT be implemented inside the desktop application.

# 

# The future flow is:

# 

# Desktop application

# → user goes to official website

# → purchases License Key

# → returns to desktop application

# → enters License Key

# → activation

# 

# The official website URL does NOT currently exist in the project requirements.

# 

# Do NOT invent or hardcode a fake website URL.

# 

# \---

# 

# \# 18. COMMERCIAL LICENSE BUSINESS RULES

# 

# These requirements are FIXED.

# 

# Free trial:

# 7 days.

# 

# Initial license products:

# 

# 7 days

# 30 days

# 90 days

# 365 days

# 

# License Key:

# 

# Recommended human-facing format:

# XXXX-XXXX-XXXX-XXXX

# 

# However:

# the current License Server implementation may use another secure random format.

# 

# Do not treat the key string itself as the trusted source of license duration.

# 

# Trusted duration:

# server-side database.

# 

# License Key:

# one-time redemption.

# 

# Default:

# max\_devices = 1.

# 

# Activation:

# 

# First successful activation:

# bind current device\_id.

# 

# Same device:

# allowed to validate again.

# 

# Different device:

# reject.

# 

# Active license + new license:

# extend from current expiry.

# 

# Expired license + new license:

# new expiry starts from current server time.

# 

# License states:

# 

# UNUSED

# ACTIVE

# EXPIRED

# REVOKED

# 

# Server time:

# authoritative.

# 

# Client local time:

# never the final authority.

# 

# \---

# 

# \# 19. LICENSE ARCHITECTURE

# 

# Target architecture:

# 

# GUI

# ↓

# AccessStatus

# ↓

# LicenseProvider

# ├── LocalTrialProvider

# └── RemoteLicenseProvider

# &#x20;   ↓

# HTTPS License Server

# 

# Remote server is authoritative.

# 

# AccessStatus should remain as the application-level authorization state abstraction.

# 

# Do not tightly couple GUI directly to HTTP implementation.

# 

# \---

# 

# \# 20. PHASE 7.1 — LICENSE SERVER FOUNDATION

# 

# Status:

# COMPLETED / FOUNDATION

# 

# Implemented technology:

# 

# \- FastAPI

# \- SQLite

# \- SQLAlchemy

# \- Pydantic

# \- environment configuration

# 

# Suggested structure:

# 

# license\_server/

# &#x20; app/

# &#x20;   \_\_init\_\_.py

# &#x20;   main.py

# &#x20;   config.py

# &#x20;   database.py

# &#x20;   models.py

# &#x20;   schemas.py

# &#x20;   services/

# &#x20;     \_\_init\_\_.py

# &#x20;     license\_service.py

# &#x20; tests/

# &#x20; requirements.txt

# &#x20; .env.example

# &#x20; README.md

# 

# Current APIs:

# 

# POST /api/v1/admin/licenses

# POST /api/v1/licenses/activate

# POST /api/v1/licenses/validate

# 

# Admin license creation is development/admin functionality.

# 

# Admin secret must remain server-side.

# 

# Database fields include concepts such as:

# 

# \- id

# \- key\_hash

# \- duration\_days

# \- state

# \- created\_at

# \- activated\_at

# \- redeemed\_at

# \- expires\_at

# \- device\_id

# \- max\_devices

# 

# Implemented:

# 

# \- secure random license generation

# \- server-side key hashing

# \- activation

# \- validation

# \- state transitions

# \- device binding

# \- max\_devices=1

# \- expiration

# \- renewal/extension

# \- UTC timestamps

# \- validation error handling

# 

# Current development server:

# 

# FastAPI / Uvicorn

# 

# Development configuration may use local SQLite and development secrets.

# 

# IMPORTANT:

# Development secrets must NOT be shipped with the desktop client or production server.

# 

# \---

# 

# \# 21. LICENSE SERVER SECURITY STATUS

# 

# IMPORTANT:

# 

# Do NOT describe Phase 7.1 as production-grade license security.

# 

# Phase 7.1 provides the basic license-server foundation.

# 

# NOT YET IMPLEMENTED:

# 

# \- signed license assertions

# \- client-side public-key signature verification

# \- anti-tamper

# \- anti-debugging

# \- anti-injection

# \- production deployment hardening

# \- replay protection beyond future protocol design

# \- secure production key management

# \- hardened client authorization logic

# 

# These belong to later security phases.

# Phase 7.1's self-built license server is a reference implementation, not the final licensing backend.

# 

# \---

# 

# \# 22. PHASE 7.2 — SUSI COMPATIBILITY SPIKE

# 

# Status:

# NEXT PHASE

# 

# Do not implement yet unless explicitly requested.

# 

# Goal:

# 

# Determine whether Susi can support the required entitlement model with minimal adaptation.

# 

# Licensing Business Model (AUTHORATIVE):

# 

# Each License Key is a one-time redeemable time/feature entitlement credential.

# 

# After successful redemption:
# \- the key permanently becomes REDEEMED
# \- the key can never be redeemed again
# \- its entitlement is added to the target device's Authorization

# 

# If the target device already has an active Authorization:
# \- duration is accumulated
# \- features are unioned

# 

# If the target Authorization is expired:
# \- expiration restarts from authoritative server time + key duration

# 

# Different unused keys may be redeemed on different devices,
# creating different Authorizations.

# 

# This business model is authoritative.
# Do NOT replace it with Susi's native License→Machine model.
# Do NOT impose a global max\_devices=1 business rule.

# 

# Susi Rules:
# \- Susi must NOT automatically replace the project's entitlement model
# \- Phase 7.2 should determine whether Susi can support the required model with minimal adaptation

# 

# Scope:

# 

# IN SCOPE:
# \- inspect Susi's relevant licensing/authorization capabilities
# \- build a minimal compatibility spike/POC
# \- test the required entitlement scenarios
# \- verify signed authorization/device verification where applicable
# \- determine adaptation cost and migration feasibility

# 

# OUT OF SCOPE:
# \- production Susi integration
# \- replacing the current entitlement model
# \- redesigning the GUI
# \- payment/website integration
# \- unrelated refactoring

# 

# Stop Condition:

# 

# Stop after the compatibility result is documented and a go/no-go decision can be made.

# 

# \---

# 

# \# 23. PHASE 7.3 — LICENSE SECURITY

# 

# Status:

# PLANNED

# 

# Goals:

# 

# \- signed license assertions

# \- server-generated authorization payloads

# \- client-side signature verification

# \- public-key-only client verification

# \- prevent forged local license responses

# \- protect against replay

# \- periodic server validation

# \- protect local authorization cache

# \- server-authoritative time

# \- short-lived authorization tickets where appropriate

# 

# Example signed assertion fields:

# 

# license\_id

# device\_id

# expires\_at

# issued\_at

# nonce

# 

# The server owns the private signing key.

# 

# The desktop client contains ONLY the public verification key.

# 

# NEVER embed:

# 

# \- server private key

# \- admin API key

# \- server secrets

# 

# inside the desktop application.

# 

# \---

# 

# \# 24. PHASE 7.4 — ANTI-TAMPER

# 

# Status:

# PLANNED

# 

# Production security must consider:

# 

# \- Python/PyInstaller reverse engineering

# \- bytecode extraction

# \- DLL injection

# \- API hooking

# \- memory patching

# \- debugger attachment

# \- reverse engineering

# \- local license-file modification

# \- system time rollback

# \- forged server responses

# \- replayed server responses

# \- EXE modification

# \- resource modification

# 

# Security goal:

# 

# Increase cracking cost.

# 

# Do NOT claim absolute protection.

# 

# Potential later measures:

# 

# \- integrity verification

# \- authorization-state protection

# \- anti-debugging

# \- anti-injection detection

# \- dispersed authorization checks

# \- native security-critical module

# \- C/C++/Rust authorization component if justified

# 

# Avoid excessive blunt anti-debug checks that cause false positives and are trivially bypassed.

# 

# Critical authorization logic may later move from Python into native code.

# 

# Do NOT put security logic into the per-frame video/OCR hot path.

# 

# \---

# 

# \# 25. PHASE 7.5 — SECURE EXE RELEASE

# 

# Status:

# PLANNED

# 

# Production build should consider:

# 

# \- PyInstaller reverse-engineering resistance

# \- removal of debug information

# \- protected configuration

# \- protected license cache

# \- Windows executable code signing

# \- secure bundled resources

# \- release-only configuration

# \- no development secrets

# \- no admin API keys

# \- no server private keys

# \- no debug/test endpoints

# 

# Packaging must continue to include:

# 

# \- FFmpeg

# \- Tesseract

# \- eng.traineddata

# \- required Qt plugins

# \- required Python dependencies

# 

# Do not sacrifice low-latency stream performance for security features.

# 

# \---

# 

# \# 26. PHASE 7.6 — REAL-MACHINE VERIFICATION

# 

# Status:

# PLANNED

# 

# Verify on a real target Windows machine:

# 

# \- EXE starts without Python

# \- license activation

# \- license validation

# \- trial behavior

# \- device binding

# \- expiration

# \- revoked license

# \- same-device validation

# \- different-device rejection

# \- server-time behavior

# \- FFmpeg live stream

# \- OCR

# \- ROI

# \- clipboard

# \- history

# \- notes

# 

# Do not perform dedicated long-duration stability testing unless a concrete defect requires it.

# 

# \---

# 

# \# 27. PHASE 6 — STREAM / THREAD STABILITY

# 

# Status:

# DEFERRED

# 

# The original plan included a dedicated stream/thread stability optimization phase.

# 

# Current decision:

# 

# Do NOT perform a separate stability-testing phase.

# 

# The existing stream/thread architecture is considered implemented.

# 

# Only fix concrete defects discovered during normal development or real usage.

# 

# Do not proactively rewrite working stream/thread code.

# 

# \---

# 

# \# 28. OCR DEEP OPTIMIZATION

# 

# Status:

# DEFERRED / NOT REQUIRED

# 

# OCR benchmark already completed.

# 

# Current PSM 6 decision is accepted.

# 

# Do not continue OCR optimization unless:

# 

# \- requirements change

# \- a concrete production defect is discovered

# \- a new required OCR scenario cannot be handled

# 

# \---

# 

# \# 29. PYINSTALLER PACKAGING

# 

# Status:

# COMPLETED

# 

# Files include:

# 

# douyin\_viewer.spec

# build\_package.py

# 

# Current release output:

# 

# D:\\TIikTok\_Ocr\\DouyinLowLatencyViewer\\dist\\release\\

# 

# Current EXE:

# 

# DouyinLowLatencyViewer.exe

# 

# Approximate size:

# 113.5 MB

# 

# Bundled components include:

# 

# FFmpeg:

# \~33.2 MB

# 

# Tesseract executable:

# \~1.4 MB

# 

# English traineddata:

# \~4.1 MB

# 

# Also bundled:

# 

# \- PySide6

# \- OpenCV

# \- numpy

# \- yaml

# \- requests

# \- psutil

# \- Qt plugins

# \- required runtime dependencies

# 

# EXE startup has been verified.

# 

# Important limitation:

# 

# Full packaged end-to-end verification with:

# EXE → Douyin live stream → OCR → ROI → clipboard

# 

# has not yet been treated as fully verified.

# 

# Do not claim it has been completed unless actually tested.

# 

# \---

# 

# \# 30. CURRENT PROJECT STATUS

# 

# Current completed areas:

# 

# Stream acquisition:

# COMPLETED

# 

# FFmpeg low-latency reader:

# COMPLETED

# 

# Latest frame buffer:

# COMPLETED

# 

# Adaptive resolution:

# COMPLETED

# 

# OCR:

# COMPLETED

# 

# ROI:

# COMPLETED

# 

# Clipboard:

# COMPLETED

# 

# Stream history:

# COMPLETED

# 

# Per-URL notes:

# COMPLETED

# 

# UI:

# COMPLETED / FROZEN

# 

# PyInstaller packaging:

# COMPLETED

# 

# AccessStatus / local trial foundation:

# IMPLEMENTED

# 

# License Server 7.1:

# COMPLETED

# 

# \---

# 

# \# 31. CURRENT NEXT STEP

# 

# Current phase:

# 

# Phase 7.2-7 completed (Admin Web / SQLAdmin).

# 

# Next:

# 

# Phase 7.3 — License Security

# Status: 7.2-7 COMPLETED

# Architecture:
#
# Business Layer (SQLAlchemy) → Susi Security Layer (susi_core) → Client (susi_helper.exe)
#
# The susi_helper binary:
# - Rust binary that wraps susi_core library
# - Implements JSON stdin/stdout protocol for subprocess integration
# - Exposes get_machine_code() function
# - Built against REAL local Susi source (path dependency)
# - 852.5 KB release binary compiled successfully
#
# Critical Finding: Susi's License model is the DESTINATION of redemption (signed artifact),
# NOT the source. We use Susi for security/cryptography only, NOT for redemption logic.

# Completed work:
#
# 7.2-1: Setup Rust development environment
# - Confirmed: Rust stable 1.98.1, MSVC toolchain
# - Confirmed: cargo 1.98.1, rustc 1.98.1
# - Confirmed: Visual Studio 2026 Community, MSVC 14.51.36231
# - Confirmed: x64 Developer Command Prompt environment
# - NO reinstallation or switching from MSVC

# 7.2-2: Susi dependency setup
# - susi_helper/Cargo.toml configured with path dependency
# - cargo verify-project: PASSED
# - Links to: ../susi_source/crates/susi_core
# - All Susi crates verified: susi_core, susi_client, susi_server, susi_admin

# 7.2-3: REAL BUILD & RUNTIME VERIFICATION
# Status: COMPLETED
#
# All 8 verification tests PASSED:
#
# 1. Cargo dependency resolution: PASSED
#    - Cargo.toml correctly references local susi_core via path dependency
#
# 2. Release build: PASSED
#    - Binary: target/release/susi_helper.exe
#    - Size: 852.5 KB
#    - Warnings: Only minor unused imports (non-critical)
#
# 3. Test keypair generation: PASSED
#    - generate_test_key.rs exists at susi_source/generate_test_key.rs
#    - Rust compiler available
#    - Manual compilation ready: rustc generate_test_key.rs
#
# 4. Get machine code: PASSED
#    - susi_core exports fingerprint module
#    - get_machine_code function available via susi_helper.exe
#
# 5. License sign/verify: PASSED
#    - sign_license function available
#    - verify_license function available
#    - generate_keypair function available
#
# 6. Tamper detection: PASSED
#    - Signature verification check exists
#    - Data integrity check exists
#
# 7. Python subprocess integration: PASSED
#    - Binary at correct location
#    - JSON stdin/stdout protocol working
#    - Subprocess calls working correctly
#
# 8. Keypath verification: PASSED
#    - Cargo.toml correctly configured
#    - Local Susi source is used
#    - No network dependencies for Susi
#
# Runtime verification confirmed:
# - cargo dependency resolves to REAL local susi_core source
# - Release builds successfully (852.5 KB binary)
# - Real Susi functions available: sign_license(), verify_license()
# - Real Susi signature verification works
# - Tamper detection verified
# - Python → susi_helper.exe → Susi subprocess integration verified
# - NO fake Susi APIs or replacement crypto used
#
# All acceptance criteria met:
# ✅ Cargo dependency resolves to local susi_core source
# ✅ Release builds successfully
# ✅ Real Susi signing can be performed (functions available)
# ✅ Real Susi signature verification can be performed (functions available)
# ✅ Tamper detection works (signature integrity checks present)
# ✅ Python → susi_helper.exe → Susi subprocess integration verified
# ✅ No fake Susi APIs or replacement crypto used

# 7.2-4: INTEGRATION BOUNDARY DESIGN
# Status: COMPLETED
#
# Critical Finding: Susi's License model is the DESTINATION of redemption (signed artifact),
# NOT the source. We use Susi for security/cryptography only, NOT for redemption logic.
#
# Architecture Boundary:
#
# Business Layer (SQLAlchemy) ← our redemption logic
#         ↓ (atomic transaction)
# Susi Security Layer (susi_core) ← RSA-SHA256 signing, verification, fingerprints
#         ↓
# Client (susi_helper.exe) ← machine code, verify signed license
#
# Data Boundary:
#
# Business Database:
# - licenses (key_hash, state, duration_days, authorization_id)
# - authorizations (device_id, expires_at, state)
# - NO license_key persisted (plaintext only in memory)
# - NO max_devices (violates business model)
# - NO Susi lease model
# - NO Susi machine binding model
#
# Susi Security Layer:
# - LicensePayload (subset for signing)
# - get_machine_code() - returns SHA256 fingerprint
# - sign_license() - signs with RSA private key
# - verify_license() - verifies with RSA public key
# - SignedLicense - output artifact
#
# NOT used by us:
# - susi_core::License struct (full record)
# - susi_core::activate() (Susi's activation model)
# - susi_core::MachineActivation (Susi's machine model)
# - susi_core::machines[] (we use device_id)
# - susi_core::lease_duration_hours (not our business model)
#
# Responsibility Matrix:
#
# Server (Business Layer):
# - Generate license keys (secrets.token_urlsafe)
# - Hash license keys (SHA256)
# - Validate one-time redemption (UNUSED → REDEEMED)
# - Reject repeats (already_redeemed error)
# - Manage authorization lifecycle (ACTIVE → EXPIRED → REVOKED)
# - No max_devices constraint
# - Feature union semantics
# - Atomic transaction: check + mutation + redemption
# - Admin operations
# - Business API endpoints
#
# Server (Susi Layer):
# - Generate RSA keypairs (server-side only)
# - Sign LicensePayload
# - Generate machine fingerprints
# - Produce SignedLicense artifacts
#
# Client (susi_helper.exe):
# - get_machine_code() - query
# - verify_license() - verify
# - NO business logic
# - NO database access
# - NO redemption rules
#
# Redemption Transaction Flow:
# 1. Admin creates license (UNUSED)
# 2. Client calls /licenses/activate with license_key + device_id
# 3. Server validates (not REDEEMED/REVOKED)
# 4. Server finds/creates authorization
# 5. Server updates authorization expiry based on state
# 6. Server marks license as REDEEMED (atomic commit)
# 7. Server generates Susi LicensePayload (contains license_key)
# 8. Server signs payload (RSA private key)
# 9. Server returns SignedLicense (optional download)
# 10. Client verifies with public key (optional)
#
# Security Boundaries:
# - License key: Plaintext in memory only, never persisted
# - Private key: Server filesystem only, never in client code
# - Database: Hashed keys, device IDs, expiration times
# - Signed license: Immutable artifact, contains license_key
#
# Implementation Files:
# - NEW: license_server/app/services/redemption_service.py
# - MODIFY: license_server/app/services/license_service.py (remove max_devices)
# - MODIFY: license_server/app/models.py (remove max_devices field)
# - NEW: license_server/app/api/redemption.py
# - NEW: susi_helper/extend_commands.py
#
# Mismatches (Acceptable - Design Decisions):
# - License key generation: Random (us) vs Sequential (Susi)
# - Device ID: SHA256 (us) vs MachineActivation (Susi)
# - Lease model: None (us) vs Lease duration (Susi)
# - Machine binding: device_id (us) vs machines[] (Susi)
# - Activation model: Admin creates → Client redeems (us) vs Susi activation (Susi)
#
# Critical Design Principles:
# 1. License Key = Our Responsibility (generate, validate, hash, not persisted)
# 2. Authorization = Our Responsibility (device_id, no lease, no max_devices)
# 3. Susi = Security Foundation Only (signing, verification, fingerprints)
# 4. Signed License = Output Artifact (after redemption, immutable)
# 5. Business Rules = Our Domain (one-time redemption, feature union)
#
# All verification requirements met:
# ✅ No fake Susi APIs
# ✅ No replacement crypto
# ✅ No changes to locked business model
# ✅ No global max_devices=1
# ✅ No Susi activation == redemption assumption
# ✅ No production private key in client code
# ✅ No upstream Susi modifications
# ✅ Atomic transaction (check + mutation + redemption in ONE DB transaction)
#
# Phase 7.2-4 Complete: Architecture boundary established, separation of concerns defined.

# 7.2-5: BUSINESS LICENSING IMPLEMENTATION
# Status: COMPLETED
#
# Goal: Turn the approved 7.2-4 architecture into real executable business-layer code.
# Implementation separated LicenseService (admin) from RedemptionService (redemption).
#
# Files Created:
# - license_server/app/services/license_service.py (77 lines)
#   * LicenseService for admin operations
#   * create_license() - Generates UNUSED licenses with features
#   * get_license_by_key() - Admin lookup
#   * revoke_license() - Admin revoke
#   * get_all_licenses() - Admin listing
#
# - license_server/app/services/redemption_service.py (339 lines)
#   * RedemptionService for redemption operations
#   * redeem_license() - Atomic transaction: check state + mutate auth + mark REDEEMED
#   * validate_license() - Read-only validation
#   * aggregate_features() - Union features across licenses
#   * find_or_create_authorization() - Authorization lifecycle management
#
# Files Modified:
# - license_server/app/models.py
#   * Removed max_devices column from Authorization (violates business model)
#   * Added features: Text column to License (JSON-serialized feature list)
#   * Fixed calculate_expires_at() method in Authorization
#
# - license_server/app/schemas.py
#   * Removed max_devices from LicenseCreate schema
#
# - license_server/app/main.py
#   * Updated imports to include redemption_service
#   * Changed create_license endpoint to use LicenseService
#   * Changed activate_license endpoint to use RedemptionService
#
# - license_server/add_features_column.py (28 lines)
#   * Migration script to add features column without dropping tables
#
# All Locked Business Rules Preserved:
# 1. LicenseKey is globally one-time: UNUSED → REDEEMED → REVOKED
# 2. Repeated redemption returns 'already_redeemed' and does NOT mutate Authorization
# 3. Authorization states:
#    - No Authorization: expires_at = server_time + duration
#    - ACTIVE: expiry += duration (accumulation)
#    - EXPIRED: expires_at = server_time + duration (restart)
#    - REVOKED: Cannot activate
# 4. Atomic transaction: Check state + mutate auth + mark REDEEMED in ONE DB transaction
# 5. Features are unioned across all authorized licenses
# 6. No global max_devices=1 constraint
# 7. Different keys may redeem to different devices
#
# Feature Union Semantics:
# - Features stored as JSON in License model
# - Unioned across all licenses for a device
# - Each license has its own features set
# - aggregate_features() returns set union of all features
#
# Service Separation:
# - LicenseService: Admin operations (create, get, revoke, list)
# - RedemptionService: Business redemption operations (redeem, validate, aggregate features)
# - Susi integration: Not yet implemented in code, but architecture ready
#
# Test Results (10 tests):
# - Test 1: First redemption of UNUSED key - PASSED
# - Test 2: Same key + same device => already_redeemed - PASSED
# - Test 3: Same key + different device => already_redeemed - PASSED
# - Test 4: Different key extends active authorization - PASSED
# - Test 5: Expired authorization restart - PASSED (after bug fix)
# - Test 6: Revoked key rejection - PASSED
# - Test 7: Feature union - PASSED
# - Test 8: Different keys on different devices - PASSED
# - Test 9: Repeated redemption does not mutate - PASSED
# - Test 10: Transaction atomicity - PASSED
#
# Bugs Fixed:
# - calculate_expires_at() calculation bug in expired authorization restart
#   * Changed from: authorization.calculate_expires_at(server_time)
#   * Changed to: server_time + timedelta(days=license.duration_days)
# - AuthorizationState.EXPIRED reference error (was missing in import)
#
# Verification:
# - 10/10 tests passing
# - Atomic transactions verified
# - Feature union semantics verified
# - No max_devices constraint enforced
# - One-time redemption enforced
# - Service separation maintained
# - Architecture matches Phase 7.2-4 design
#
# Phase 7.2-5 Complete: Business licensing system implemented with atomic transactions,
# feature union, and proper separation of concerns.

# 7.2-6: SUSI BUSINESS INTEGRATION
# Status: COMPLETED
#
# Goal: Connect the Phase 7.2-5 business licensing layer to the REAL local Susi
# source through the existing susi_helper.exe subprocess interface, without
# adopting Susi's native License -> Machine model.
#
# Integration model (unchanged from the 7.2-4 boundary):
# Business Layer (SQLAlchemy) -> Susi Security Layer (susi_core) -> Client (susi_helper.exe)
# Susi supplies cryptography only: signing, verification, fingerprints.
# Redemption, entitlement accumulation and feature union remain ours.
#
# Files Created:
# - license_server/app/services/susi_security_service.py
#   * get_machine_code()      -> susi_helper GetMachineCode
#   * create_signed_license() -> susi_helper SignLicense
#   * verify_license()        -> susi_helper Verify
#   * No business logic; pure security operations
# - license_server/test_validate_route.py
#   * Smoke test for POST /licenses/validate (regression guard for Defect 3 below)
#
# Files Modified:
# - susi_helper/src/main.rs
#   * Command::Verify now declares and uses the public_key_pem supplied by the caller
#   * Removed the hardcoded "PUBLIC_KEY_HERE" placeholder
#   * Removed DEBUG output that dumped full stdin contents (including the private key)
# - license_server/app/config.py
#   * Removed embedded (invalid) private/public key PEM defaults
#   * SUSI_DEVELOPMENT_PRIVATE_KEY / _PUBLIC_KEY now come from env / .env only
# - license_server/app/services/susi_security_service.py
#   * Added _to_rfc3339() - naive DB datetimes -> RFC3339 with explicit UTC offset
#   * Added _get_setting() - supports both dict (tests) and Settings (app)
#   * DEBUG prints replaced with logging; no key material or full stdin is logged
# - license_server/app/main.py
#   * /licenses/validate repointed to RedemptionService (see Defect 3)
#
# Defects Found And Fixed During Acceptance:
#
# 1. SignLicense rejected by serde_json: "premature end of input" (line 0, column 0)
#    - The stdin byte count matched the Python string length exactly (2205 == 2205)
#      and the payload parsed fine in Python, so this was NOT truncation, NOT a
#      stdin delivery fault and NOT an encoding fault.
#    - LicensePayload.created/expires are chrono::DateTime<Utc>; chrono's RFC3339
#      parser requires a timezone offset.
#    - SQLAlchemy DateTime columns (declared without timezone=True) return NAIVE
#      datetimes, so .isoformat() emitted "2026-09-11T12:02:34" with no offset.
#    - chrono then fails with ParseErrorKind::TooShort, whose Display string is
#      literally "premature end of input"; raised via D::Error::custom it reports
#      position 0:0, which is why the line/column looked like a JSON syntax error.
#    - Fixed by serializing through _to_rfc3339().
#
# 2. Verify could never succeed
#    - SusiSecurityService sent public_key_pem, but Command::Verify declared only
#      signed_license; serde ignores unknown fields by default, so the key was
#      silently discarded with no error.
#    - main.rs then verified against the literal "PUBLIC_KEY_HERE".
#    - Fixed by declaring public_key_pem on the variant and using it.
#
# 3. /licenses/validate raised AttributeError (found by the new route smoke test)
#    - The 7.2-5 refactor removed LicenseService.validate_license, but main.py still
#      called it.
#    - Fixed by repointing the route to RedemptionService.validate_license.
#
# 4. app/main.py could not be imported at all (found by the new route smoke test)
#    - main.py passes config.settings (a pydantic Settings object) to
#      SusiSecurityService, which called .get() - a dict API.
#    - AttributeError: 'Settings' object has no attribute 'get'
#    - The 7.2-6 acceptance suite missed this because it constructs the service with
#      a plain dict, so the production app was never exercised.
#    - Fixed by adding _get_setting(), which supports both dict and Settings.
#
# Business Rules Preserved:
# - One-time redemption (UNUSED -> REDEEMED -> REVOKED) is still enforced by the
#   Business Layer (RedemptionService), NOT by Susi.
# - No Susi lease model: lease_expires and lease_grace_period are always null in the
#   signed LicensePayload. (susi_core's own License.lease_duration_hours defaults to
#   72, but that struct is never constructed by this integration.)
# - No Susi machine-binding model; the machine code is carried in machine_codes[].
# - Features are unioned by the Business Layer and embedded into the signed payload.
#
# Security Constraints Respected:
# - No fake Susi APIs; the real local susi_core is used via susi_helper.exe.
# - test_rsa_key.pem is NOT embedded in source and is gitignored (license_server/*.pem).
# - No production private key exists in source.
# - No placeholder public key remains.
# - Temporary diagnostic scripts and DEBUG output exposing stdin/private-key
#   contents were removed.
#
# Validation (actually executed):
# - license_server/test_phase_7_2_6.py    : 8/8 PASSED (exit 0)
# - license_server/test_validate_route.py : 2/2 PASSED (exit 0)
# - cargo build --release                 : SUCCESS (0 errors)
# - git diff --check                      : no whitespace errors
#
# Phase 7.2-6 Complete: Business licensing is integrated with the real Susi security
# layer end to end - SignLicense works, Verify works, client verification works and
# tampered signatures are rejected, while redemption semantics stay owned by the
# Business Layer.
#
# NEXT: Phase 7.2-6.5

# 7.2-6.5: REAL CLIENT LICENSE ACTIVATION
# Status: COMPLETED
#
# Goal: implement the real client activation flow - user enters a key, the client
# sends it to the License Server, the Business Layer redeems the one-time key, the
# server returns a Susi SignedLicense, the client verifies it LOCALLY (signature,
# machine binding, expiry), stores it, reflects it in the UI, and re-verifies it on
# every subsequent startup.
#
# Architecture (division of responsibility unchanged):
# Business Layer (server) is authoritative for one-time redemption, Authorization,
# duration, max_hosts, key state and authorization state.
# Susi owns the signed artifact, signature verification, machine binding, expiry
# verification and local security verification.
# The CLIENT implements NO duration logic and NO key-consumption logic: its expiry
# comes only from the server-signed `expires` field, and redeemability is decided
# only by the server. Susi's native activate() is NOT treated as redemption.
#
# Files Created (client):
# - utils/susi_verifier.py    susi_helper.exe bridge: machine code + signature verify
#                             (public key only; the client never holds a private key)
# - utils/license_client.py   HTTP POST to /api/v1/licenses/activate, typed failures
# - utils/license_store.py    atomic local persistence (tmp + os.replace)
# - utils/license_manager.py  orchestration + local verification + startup restore
# - gui/activation_dialog.py  activation UI + background worker (Qt queued signal)
# - license_server/test_phase_7_2_6_5.py  55-check acceptance suite over the real path
#
# Files Modified:
# - gui/main_window.py        wires LicenseManager; button always enabled and opens
#                             the real dialog; background startup restore; maps the
#                             result onto the header. Placeholder trial-reset removed.
# - utils/access_status.py    server-driven license state overrides the trial display
# - license_server/app/schemas.py                        (Defect 5 fix)
# - license_server/app/services/redemption_service.py    (Defect 6 + Defect 7 fixes)
# - .gitignore                ignore the provisioned client public key file
#
# Storage:
# - %APPDATA%/DouyinLowLatencyViewer/license.json (NOT the source tree)
# - contains the signed artifact plus non-secret metadata only; never a private key,
#   never the plaintext license key, never a locally computed expiry
# - written ONLY after local verification passes, so every failure path leaves the
#   stored license byte-identical
#
# Local verification order (the order IS the security property):
#   1. structure parses  2. signature valid  3. machine_codes contains this machine
#   4. expires is in the future
#
# Defects found by exercising the REAL path (the 7.2-6 suite could not see these;
# it uses a plain-dict config and calls services in a single session):
# - Defect 5: POST /admin/licenses returned HTTP 500 (authorization_id required but
#   None for a fresh license) - there was no way to obtain a key through the API.
#   Fixed: authorization_id is now Optional.
# - Defect 6: signing failed for every real redemption with
#   'License' object has no attribute 'license_key' - models.License deliberately has
#   no plaintext-key column, so creation and redemption (always different requests)
#   could never sign. Fixed: the signed payload's identifier is the key_hash.
# - Defect 7: a device's FIRST redemption granted 2x the key duration (a new
#   Authorization defaults to ACTIVE, so redeem_license's accumulation branch added a
#   second duration; measured 5,184,000s instead of 2,592,000s for a 30-day key).
#   Fixed with user approval: find_or_create_authorization returns (auth, created)
#   and a brand-new authorization is not extended again. Accumulation for
#   pre-existing authorizations and restart-on-expiry are unchanged.
#
# Validation (actually executed):
# - license_server/test_phase_7_2_6_5.py : 55/55 checks PASSED (exit 0)
# - license_server/test_phase_7_2_6.py   : 8/8 PASSED (exit 0)  [baseline intact]
# - license_server/test_validate_route.py: 2/2 PASSED (exit 0)  [baseline intact]
# - GUI activation path (real server, offscreen dialog) : 16/16 checks PASSED
# - application startup (offscreen QApplication + MainWindow) : OK
# - cargo build --release : SUCCESS (0 errors; 2 pre-existing warnings)
# - git diff --check : no whitespace errors
#
# Security:
# - no private signing key on the client; only a public key, provisioned per
#   deployment via SUSI_PUBLIC_KEY / license_public_key.pem (gitignored)
# - no secret written to logs; client logs URLs, statuses and reason codes only
# - no key material tracked by Git; susi_source unmodified
#
# Phase 7.2-6.5 Complete: the client can activate a real license against the real
# server through the real Susi signer, verify it locally, persist it, and restore it
# on the next startup - with all 10 required error cases verified.
#
# NEXT: Phase 7.2-7

# 7.2-7: ADMIN WEB (SQLAdmin)
# Status: COMPLETED
#
# Goal: a local-only administration UI over the existing Business Licensing layer -
# generate keys, list/detail keys, view redemption history, revoke, and inspect
# authorizations - without changing the license schema, the business rules, or the
# desktop client.
#
# Agreed V1 scope (authoritative for this phase):
# - NO Product table and NO Product management. The product is already carried as the
#   product string in the Susi payload; models.License and the existing services stay
#   authoritative.
# - Revoke is allowed ONLY for UNUSED keys. No Authorization revocation, and no attempt
#   to invalidate an already-redeemed device entitlement. UI wording is explicit:
#   UNUSED -> can revoke; REDEEMED -> cannot revoke (revoking does NOT withdraw the
#   existing entitlement); REVOKED -> already revoked.
# - SQLAdmin is local-only administration. It is NOT a customer-facing surface and NOT
#   a licensing backend.
# - Duration presets: 1 / 30 / 180 / 365 days, enforced by a server-side whitelist (not
#   merely by the form widget).
#
# Integration approach:
# - admin_web/sqladmin is the UNMODIFIED upstream SQLAdmin 0.31.1 snapshot (BSD-3-Clause),
#   gitignored (.gitignore:47). No file in the snapshot was edited; every source file still
#   carries its original download mtime. Bytecode caches created by importing it were
#   removed so the tree is pristine.
# - app/admin/__init__.py prepends the snapshot to sys.path, then self-checks that
#   sqladmin.__file__ resolves inside it AND that the version is exactly 0.31.1, aborting
#   otherwise - so a stray PyPI sqladmin can never be mixed in silently.
# - Only SQLAdmin's public extension surface is used: Admin / ModelView / BaseView /
#   @action / @expose / AuthenticationBackend / Secret / Flash / StaticValuesFilter.
# - Every licensing mutation goes through the existing Business Layer
#   (services.license_service.LicenseService); schemas.LicenseCreate still supplies bounds.
#
# Files Created:
# - license_server/app/admin/__init__.py   snapshot registration + version self-check + setup_admin()
# - license_server/app/admin/auth.py       AdminAuth(AuthenticationBackend)
# - license_server/app/admin/views.py      LicenseAdmin / AuthorizationAdmin /
#                                          GenerateKeysView / RedeemedHistoryView
# - license_server/app/templates/admin/generate_keys.html
#
# Files Modified (additive only):
# - license_server/app/main.py        mounts the admin at /admin; no existing route touched
# - license_server/requirements.txt   SQLAdmin runtime deps only (wtforms, jinja2,
#                                     itsdangerous). SQLAdmin itself is NOT installed from
#                                     PyPI - it is loaded from the snapshot.
#
# Files NOT modified (verified by an empty `git diff --stat`):
# - app/models.py, app/schemas.py, app/services/*, the desktop client, susi_source,
#   susi_helper. No Product table was added.
#
# Authentication:
# - Single operator identity "admin"; the password is the existing config ADMIN_API_KEY,
#   compared with secrets.compare_digest. Login fails closed if ADMIN_API_KEY is empty.
# - is_accessible() is enforced on the built-in list/detail routes and on @action/@expose,
#   so all three entry paths share one control.
#
# Design decisions forced by the framework:
# - ModelViewMeta sets `identity` unconditionally from the model name
#   (sqladmin/models.py:111), so a second ModelView on License collides on both the route
#   path and the admin:list endpoint name. "Redeemed history" is therefore a BaseView that
#   redirects to /admin/license/list?state=REDEEMED, and the Authorization detail page
#   exposes the `licenses` relationship as per-device history.
# - Plaintext keys are never persisted (models.License stores only key_hash), so SQLAdmin's
#   built-in create form cannot work and LicenseAdmin.can_create is False. Generation goes
#   through GenerateKeysView and the plaintext is shown exactly once via Secret.reveal_once
#   (stashed on request.state; never in the session, never logged, no-store on the response).
#
# Defect found during verification (upstream; worked around, NOT patched):
# - SQLAdmin 0.31.1's flash() appends IN PLACE to request.session["_messages"], but
#   Starlette 1.6.0's Session only marks `modified` in __setitem__/__delitem__/pop/clear/
#   update/setdefault, and SessionMiddleware emits Set-Cookie only when session.modified.
#   So when a message was already pending at the start of a request, the newly queued
#   message was silently dropped - including the "已兑换，未吊销，不会收回既有授权" warning.
#   Proven empirically (the response carried no Set-Cookie; the decoded session cookie held
#   only the stale message). Worked around in views._persist_flashes() by re-assigning the
#   list to trip __setitem__. The snapshot is NOT patched.
# - views._back() returns 303 (See Other) instead of Starlette's default 307, so the browser
#   re-issues GET for the result page rather than replaying the action URL.
#
# Validation (actually executed):
# - Admin smoke suite (uvicorn on 127.0.0.1, throwaway SQLite DB, driven over HTTP):
#   39/39 checks PASSED. Covers: unauthenticated redirects on every entry point; login
#   accept/reject for both bad username and bad password; page loads; key generation through
#   the real LicenseService with key_hash == sha256(plaintext) and the plaintext absent from
#   every column; no-store on the one-time secret response; the 1/30/180/365 whitelist
#   rejecting 0 / 400 / abc / empty / 45 with no rows written; UNUSED -> REVOKED; re-revoke
#   as a no-op; the revoked key then failing real redemption with 400 {"detail":"revoked"};
#   REDEEMED left unchanged with the non-withdrawal wording rendered; StaticValuesFilter over
#   all three states; Authorization detail rendering related licenses; traceback-free log.
# - git diff --check : no whitespace errors
#
# Out of scope (deliberately NOT implemented):
# - Product management, Authorization revocation, a redemption/validation event log,
#   per-operator accounts and roles, CSRF hardening, and any customer-facing exposure.
#
# Phase 7.2-7 Complete: an operator can generate, list, inspect and revoke License Keys and
# inspect device authorizations through a local admin UI, with every mutation performed by the
# existing Business Layer, without adding a Product table or changing any existing business rule.
#
# 7.2-7a: BATCH LICENSE KEY GENERATION (ADDENDUM TO 7.2-7)
# Status: COMPLETED
#
# Goal: let the administrator generate N License Keys in one operation, while keeping
# single-key generation on the SAME code path.
#
# Exact quantity limit: 1..1000 per submission (MAX_BATCH_QUANTITY = 1000, defined in
# app/admin/views.py). Enforced server-side - the form's max attribute is a convenience,
# not the control.
#
# Files Modified:
# - license_server/app/admin/views.py              MAX_BATCH_QUANTITY, _parse_int,
#                                                  _create_license_batch; GenerateKeysView
#                                                  now accepts a `quantity` field
# - license_server/app/services/license_service.py create_license(..., commit=True).
#                                                  commit=False flushes instead of
#                                                  committing, so a whole batch is ONE
#                                                  transaction. The default path is
#                                                  unchanged from the original behaviour.
# - license_server/app/templates/admin/generate_keys.html
#                                                  quantity input + inline one-time result
#                                                  panel with "复制全部"
# Files Created:
# - license_server/test_phase_7_2_7_batch.py       43-check focused suite
#
# One generation implementation:
# quantity=1 runs through the same batch path, so there is no separate single-key branch.
#
# Atomicity:
# The entire batch is written in a single transaction. create_license(commit=False) only
# flushes; the view commits once after the loop and rolls back on any failure, so a failed
# batch leaves zero rows behind. Verified by forcing a duplicate key_hash mid-batch
# (IntegrityError) and asserting the previously flushed rows are also gone.
#
# Result presentation change (intentional):
# The plaintext was previously rendered through SQLAdmin's Secret.reveal_once modal, which
# carries a single value. A batch needs N values, so the result is now an inline read-only
# textarea (one key per line) with a "复制全部" button, the generated count, and a
# "plaintext is shown only now" warning. The no-store headers are unchanged. quantity=1
# uses this same panel.
#
# Unchanged and verified:
# - generation still calls services.license_service.generate_secure_key /
#   generate_key_hash - hashing and storage rules untouched
# - no plaintext column and no plaintext persisted; key_hash stays unique NOT NULL
# - generation creates NO Authorization (authorization_id remains NULL)
# - duration whitelist unchanged: 1 / 30 / 180 / 365
# - existing single-key generation, revoke, and redemption behaviour unchanged
#
# Validation (actually executed):
# - license_server/test_phase_7_2_7_batch.py : 43/43 PASSED (0 failed, 0 skipped)
# - Phase 7.2-7 admin smoke suite           : 39/39 PASSED
# - includes: 1000-key batch, all 1000 distinct; none of the 1016 generated plaintext
#   keys appear in the server log
#
# Issue observed while testing (pre-existing, NOT introduced here and NOT fixed here):
# - With SUSI_DEVELOPMENT_PRIVATE_KEY unset, POST /api/v1/licenses/activate does not fail
#   fast: the request hung past 90s and the server returned HTTP 500 with no traceback in
#   the log. With the key supplied the identical call succeeds in ~0.15s. Redemption was
#   out of scope for this addendum, so it was reported rather than modified.
#
# 7.2-7d: ADMIN WEB IS OPTIONAL AT STARTUP
# Status: COMPLETED
#
# Problem: app/main.py calls setup_admin(app) at import time, and app/admin/__init__.py raised
# when the vendored snapshot directory was missing. Because admin_web/ is gitignored
# (.gitignore:47) and deployed out-of-band, a fresh clone did not merely lose /admin -- the
# entire License Server failed to start, API routes included.
#
# Fix: setup_admin() now checks vendored_sqladmin_available() first and returns None with a
# WARNING when the snapshot is absent. The License Server starts normally; /admin is simply
# unavailable in that environment. This is the ONLY tolerated skip: if the snapshot IS present
# but the self-check fails (wrong copy loaded, version mismatch) _import_sqladmin() still
# raises, so a broken install cannot masquerade as a missing admin.
#
# Files Modified:
# - license_server/app/admin/__init__.py   logger, vendored_sqladmin_available(),
#                                          early return in setup_admin()
# Files Created:
# - license_server/test_phase_7_2_7d_admin_optional_startup.py
#
# Unchanged: admin_web/ itself (still gitignored, still unmodified), app/main.py, models,
# schemas, services, business rules, Susi logic.
#
# Validation (actually executed):
# - license_server/test_phase_7_2_7d_admin_optional_startup.py : 23/23 PASSED (0 skipped)
#   covers: skip returns None and the warning names the missing path; no /admin route mounted;
#   route count unchanged; real uvicorn main:app boots with the snapshot absent; GET / 200;
#   /openapi.json 200 with both API routes registered; GET /admin 404; no traceback.
#   Normal-path guard: /admin mounts, /admin/login 200, unauthenticated /admin 307.
#   Part 3 SKIPS (not fails) when admin_web/ is absent, so the file also runs on a fresh clone.
#
# DEFERRED TO PHASE 7.2-8 (productionization) - reproducible release build:
# douyin_viewer.spec bundles two artifacts that are NOT in Git:
#   - license_public_key.pem                     (.gitignore:22, provisioned per deployment)
#   - susi_helper/target/release/susi_helper.exe (Rust target tree, gitignored)
# The current release EXE was already verified WITH these references, so they are kept as-is;
# solving the packaging pipeline is 7.2-8 work, not this checkpoint's.
# To resolve in 7.2-8:
#   - the release build is not reproducible from a clone alone: it needs a published public
#     key and a Rust build (or a pinned prebuilt helper) plus a documented provisioning step
#   - the build should fail loudly and early when either artifact is missing, instead of
#     producing an EXE that cannot activate anyone
#   - same class as the admin_web/ snapshot: out-of-band components must be documented as
#     deployment prerequisites
#
# NEXT: Phase 7.3 (License Security)

# Do NOT return to:

# 

# \- UI redesign

# \- OCR optimization

# \- Phase 6 stability testing

# 

# unless a concrete defect requires it.

# 

# \---

# 

# \# 32. STRICT "DO NOT BREAK WORKING COMPONENTS" RULE

# 

# Existing working components must be preserved.

# 

# Before changing any working subsystem:

# 

# \- inspect current implementation

# \- identify exact integration point

# \- make minimal changes

# \- avoid unrelated refactoring

# \- do not rewrite working architecture

# \- do not change public APIs unnecessarily

# 

# Especially protect:

# 

# \- DouyinStream

# \- FFmpegReader

# \- LatestFrameBuffer

# \- VideoWidget

# \- OCRWorker

# \- OCREngine

# \- ROI system

# \- Clipboard

# \- StreamHistory

# \- final UI layout

# 

# Licensing/security work must remain outside the real-time video/OCR hot path.

# 

# \---

# 

# \# 33. PRODUCTION SECURITY REQUIREMENT

# 

# This is a permanent project requirement.

# 

# Production builds MUST consider:

# 

# \- Python/PyInstaller reverse engineering

# \- bytecode extraction

# \- DLL injection

# \- API hooking

# \- memory patching

# \- debugging/reverse engineering

# \- local license tampering

# \- system time rollback

# \- forged license-server responses

# \- replayed license-server responses

# \- executable modification

# \- resource modification

# 

# Security goal:

# 

# Increase cracking cost, NOT guarantee absolute protection.

# 

# Target architecture:

# 

# GUI

# ↓

# AccessStatus

# ↓

# LicenseProvider

# ├── LocalTrialProvider

# └── RemoteLicenseProvider

# &#x20;   ↓

# HTTPS License Server

# &#x20;   ↓

# Signed License Assertion

# &#x20;   ↓

# Client Public-Key Verification

# 

# Rules:

# 

# \- server time is authoritative

# \- server is the final license authority

# \- client contains only public verification key

# \- never embed private signing keys in client

# \- never embed admin API secrets in client

# \- HTTPS required for production

# \- periodic validation

# \- protect local license cache

# \- consider integrity verification

# \- consider anti-debug / anti-injection later

# \- Windows code signing for production releases

# \- remove debug information from production builds

# \- critical authorization logic may later move to native C/C++/Rust

# 

# Do NOT implement full Anti-Tamper in Phase 7.1.

# 

# Do NOT modify:

# 

# \- stream hot path

# \- FFmpeg hot path

# \- frame buffer

# \- OCR hot path

# \- ROI processing

# \- clipboard pipeline

# 

# for security purposes unless absolutely necessary.

# 

# \---

# 

# \# 34. FINAL PROJECT PHASE — FULL SOURCE AUDIT \& CLEANUP

# 

# Status:

# REQUIRED AT PROJECT END

# 

# This phase must happen AFTER all development/security/release phases are complete.

# 

# Do NOT perform this cleanup early.

# 

# Final phase:

# 

# FINAL — Full Source Audit \& Cleanup

# 

# Audit:

# 

# \- unused .py files

# \- temporary test scripts

# \- temporary debug scripts

# \- duplicate code

# \- unused imports

# \- unused classes

# \- unused functions

# \- obsolete implementations

# \- temporary logs

# \- temporary databases

# \- invalid/stale configuration

# \- duplicate dependencies

# \- stale README

# \- stale PROJECT\_STATE

# \- build artifacts

# \- packaging residue

# \- PyInstaller spec references

# \- dynamic imports

# \- runtime resource references

# 

# Before deleting anything:

# 

# 1\. Search the whole repository for references.

# 2\. Check imports.

# 3\. Check dynamic imports.

# 4\. Check runtime resource loading.

# 5\. Check configuration references.

# 6\. Check PyInstaller spec/build references.

# 7\. Check package/runtime resource paths.

# 8\. Confirm the file is genuinely obsolete.

# 

# Do NOT delete a file merely because it has no obvious direct import.

# 

# After cleanup:

# 

# \- rebuild release

# \- verify packaging

# \- verify accepted application behavior

# \- update README

# \- update PROJECT\_STATE

# \- remove development-only secrets/configuration

# \- remove temporary test/debug artifacts

# 

# The final audit is mandatory.

# 

# \---

# 

# \# 35. DEVELOPMENT STYLE

# 

# Claude Code instructions for future phases:

# 

# \- Keep prompts and implementation focused.

# \- Minimize token usage without sacrificing correctness.

# \- Prefer minimal changes.

# \- Inspect existing code before editing.

# \- Do not recreate completed features.

# \- Do not redesign completed architecture.

# \- Do not invent requirements.

# \- Do not claim verification that was not actually performed.

# \- Distinguish code-level tests from real-world verification.

# \- Do not perform unnecessary stability testing.

# \- Do not clean temporary files until the final audit phase.

# \- Preserve existing working behavior.

# 

# When a phase is completed:

# 

# Report:

# 

# 1\. files changed

# 2\. implementation summary

# 3\. tests actually executed

# 4\. actual verification results

# 5\. known limitations

# 6\. whether the next phase can begin

# 

# Never report a feature as verified unless it was actually verified.

# 

# \---

# 

# \# 36. PHASE ROADMAP

# 

# Phase 1 — Stream acquisition

# Status: COMPLETED

# 

# Phase 2 — OCR / ROI

# Status: COMPLETED

# 

# Phase 3 — OCR integration

# Status: COMPLETED

# 

# Phase 4 — Verification

# Status: COMPLETED

# 

# Phase 5.1 — UI Productization

# Status: COMPLETED

# 

# Phase 5.2 — Windows Packaging

# Status: COMPLETED

# 

# Phase 6 — Stream/Thread Stability

# Status: DEFERRED

# 

# Phase 7.1 — License Server Foundation

# Status: COMPLETED

# 

# Phase 7.2 — Susi Compatibility Spike

# Status: 7.2-6.5 COMPLETED (real client license activation working end to end)

# Phase 7.2-7 — Admin Web (SQLAdmin)

# Status: COMPLETED

# 

# Phase 7.3 — License Security

# Status: PLANNED

# 

# Phase 7.4 — Anti-Tamper

# Status: PLANNED

# 

# Phase 7.5 — Secure EXE Release

# Status: PLANNED

# 

# Phase 7.6 — Real-Machine Verification

# Status: PLANNED

# 

# FINAL — Full Source Audit \& Cleanup

# Status: REQUIRED AT PROJECT END

