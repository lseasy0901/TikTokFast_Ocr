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

# Phase 7.1 completed.

# 

# Next:

# 

# Phase 7.2 — Susi Compatibility Spike

# 

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

# Status: NEXT

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

