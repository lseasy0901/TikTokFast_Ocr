# Claude Project Memory - Douyin Low Latency Viewer

## Read Before Coding

Before modifying code:
- read this file
- read relevant PROJECT_STATE.md sections
- inspect existing implementation
- do not assume missing functionality

## Current Phase

Phase 7.1 is COMPLETED. Phase 7.2 (Susi Compatibility Spike) has NOT started.

Do not implement Phase 7.2 unless explicitly requested.

## Architecture Rules

The project uses:
- direct Douyin stream extraction
- FFmpeg for stream decoding
- latest-frame architecture
- PySide6 GUI
- OpenCV/Tesseract OCR

Hard restrictions:
- no Selenium
- no Playwright
- no browser screenshots
- no OBS
- no OpenCV VideoCapture

Video prioritizes low latency. Dropping old frames is preferred over accumulating latency.

## OCR Rules

OCR must never block or slow the video pipeline.

OCR operates on the selected ROI from the raw frame.

Current OCR behavior:
- English letters and digits only
- no punctuation
- no spaces/newlines in final copied text
- no character guessing/replacement heuristics
- blank/unrecognized results must not overwrite clipboard
- OCR interval is currently 300ms
- user-controlled start/stop via the existing OCR button

Do not silently change these rules.

## ROI Rules

There is one ROI only.

ROI selection:
- "选择识别区域" starts/restarts selection
- Enter confirms
- Esc cancels
- cancel during reselection restores the previous ROI
- confirmed ROI remains visually overlaid on live video
- overlay is visual only
- OCR uses raw-frame pixels, not the overlay

## UI Rules

The UI is currently frozen as a productized dark navy/blue video-first interface.

Do not invent:
- chat
- viewer count
- thumbnails
- playback controls
- unrelated features

Do not perform UI redesign unless explicitly requested.

## Licensing Business Rules

Each License Key is a one-time redeemable time/feature entitlement credential.

After successful redemption:
- the key permanently becomes REDEEMED
- the key can never be redeemed again
- its entitlement is added to the target device's Authorization

If the target device already has an active Authorization:
- duration is accumulated
- features are unioned

If the target Authorization is expired:
- expiration restarts from authoritative server time + key duration

Different unused keys may be redeemed on different devices,
creating different Authorizations.

This business model is authoritative.
Do NOT replace it with Susi's native License→Machine model.

Phase 7.1's self-built license server is a reference implementation, not the final licensing backend.

## Susi Rules

Susi is only a future compatibility candidate.

Phase 7.2 is:

Susi Compatibility Spike

Susi has NOT been integrated yet.

Susi must NOT automatically replace the project's entitlement model.

Do NOT integrate Susi during this documentation checkpoint.

The goal of Phase 7.2 is to determine whether Susi can support the required entitlement model with minimal adaptation.

## Git Workflow

Start a phase from a clean working tree.

Do not require a commit for every tiny change.

After a phase or meaningful approved checkpoint:
- inspect git diff
- verify scope
- commit the approved result

Use extra checkpoints before high-risk experiments when useful.

Never overwrite unrelated user changes.

## Scope Lock

Every implementation phase must explicitly define:
- IN SCOPE
- OUT OF SCOPE
- Acceptance Criteria
- Stop Condition

Do not perform cross-phase work.

## Verification Rules

Use targeted verification appropriate to the changed area.

Never claim a test, runtime check, build, or verification was performed unless it actually was.

Static inspection must be described as static inspection.

## Token Efficiency

Prefer:
- focused inspection
- minimal edits
- targeted tests
- concise reports

Do not repeatedly reread the entire repository when the relevant files are already known.

Do not perform unrelated cleanup.
