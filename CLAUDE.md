# Claude Project Memory - Douyin Low Latency Viewer

## Read Before Coding

Before modifying code:
- read this file
- read relevant PROJECT_STATE.md sections
- inspect existing implementation
- do not assume missing functionality

## Current Phase

Phase 8 (OCR v2 / GUI redesign) is COMPLETED and self-verified.

The next phase is release packaging (PyInstaller build, then installer).

Do not start a new feature phase unless explicitly requested.

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

Current OCR behavior (OCR v2, Phase 8):
- the user must select the game explicitly; OCRWorker rejects None/auto
  (`_require_explicit_game`). `auto` survives only as an internal fallback and
  is never a legitimate user path.
- room codes are validated per position against a code format: every position is
  a CharClass, LETTER (A-Z) or DIGIT (0-9) only. No punctuation, no other
  characters. The per-position character class is the single source of truth
  shared by strict validation, candidate extraction and per-position correction.
- per-position character correction IS allowed, and is deliberately
  conservative: the mapping table is finite and explicit
  (ocr/correction.py), anything not listed is judged uncorrectable, the number
  of corrected positions is capped (MAX_CORRECTIONS), and correction is never
  global -- a global O->0 / I->1 would corrupt valid codes such as VALORANT's
  "IOI123". Prefer no result over a possibly wrong room code.
- a frame yields either a strictly-validated room code or None. Text that failed
  validation is never returned to callers.
- temporal consistency: the same code must appear min_agreement times (default 2)
  within the observation window (default 3) before it is emitted, so the first
  result lands after ~2 intervals (~600ms at the 300ms interval). On ambiguity
  the resolver prefers no result over guessing.
- when one frame has several OCR readings (one per PSM mode), they are arbitrated
  among already-validated candidates only: fewest corrections first, then
  fewest case folds. Arbitration never turns a valid code into an invalid one.
- when a reading clearly indicates the wrong game is selected, the frame returns
  None and the evidence is surfaced via last_conflict. The layer only reports;
  it never switches the game automatically.
- no spaces/newlines in final copied text
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

Phase 8's UI redesign is COMPLETE and frozen as the productized dark,
video-first interface. All colours live in gui/theme.py's `Palette` class and the
QSS consumes them as `{TOKEN}` placeholders; shared chrome lives in
gui/widgets.py.

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

Susi IS integrated at the machine-code and signature-verification layer, and
only there:
- utils/susi_verifier.py asks susi_helper.exe for the machine code and verifies a
  SignedLicense with the PUBLIC key. susi_helper is a Rust binary shipped as data
  by the spec, not an importable module.
- the client never holds a private key; the private key stays on the license server.
- license_server/app/services/susi_security_service.py covers the server side.

Susi must NOT automatically replace the project's entitlement model.

The self-built License Key -> device Authorization model under "Licensing
Business Rules" below remains authoritative and is not delegated to Susi.

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
