# Claude Project Memory - Douyin Low Latency Viewer

## Read Before Coding

- Always read PROJECT_STATE.md before starting any task
- Follow the current phase only - do not skip ahead
- Prefer minimal changes over refactoring
- Do not refactor working code without explicit approval
- Do not change architecture or tech stack without approval
- Run relevant tests before declaring completion
- Keep reports concise and focused
- Update PROJECT_STATE.md only after tasks are actually completed
- Never implement future phases early

## OCR Rules (Strict)

Final output must contain only [A-Za-z0-9]:
- No punctuation, spaces, or word segmentation
- Never replace 1 ↔ I (preserve exact characters)
- Never infer or restore missing characters
- Never apply semantic correction
- Only filter to preserve valid letters and digits

## Project Roadmap

1. OCR accuracy ✓ (Phase 4.1 completed)
2. OCR benchmark
3. OCR stability/performance
4. ROI final verification
5. UI productization
6. Stream/thread stability
7. FFmpeg + Tesseract runtime
8. PyInstaller EXE
9. Clean Windows testing
10. License Key system
11. 2-hour device trial
12. Website/payment integration