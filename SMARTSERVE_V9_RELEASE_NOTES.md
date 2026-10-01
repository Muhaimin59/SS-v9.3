# SmartServe V9 Release Notes — 2026-09-21

## Completion-proof lifecycle fix
- Successful provider completion-proof uploads now return to the Live Service page.
- The Live Service page keeps the persisted proof gallery visible after upload.
- Provider UI explicitly shows `Completion proof submitted` and `Waiting for customer verification` while in `AWAITING_VERIFICATION`.
- Socket.IO request updates no longer force a full-page reload on the Live Service route, preventing the file input from being recreated and making the uploaded filename appear to disappear.
- Live Service listens for request-update events and refreshes its status/proof panel in place.
- Other pages retain their existing automatic reload behavior.

## Gemini resilience
- Added transient 408/429/5xx/unavailable/overload/timeout detection.
- Configured primary -> fallback -> optional second fallback model chain.
- `GEMINI_FALLBACK_MODEL_2` is now supported.

## Verification
- `app.py` Python compilation: passed.
- `ai_service.py` Python compilation: passed.
- `static/js/realtime.js` Node syntax check: passed.
- No `.env` or credential files were added to the release ZIP.

Recommended local configuration:
```env
GEMINI_MODEL=gemini-3.6-flash
GEMINI_FALLBACK_MODEL=gemini-2.5-flash
GEMINI_FALLBACK_MODEL_2=
SMARTSERVE_DB_PATH=./smartserve.db
SMARTSERVE_UPLOAD_FOLDER=./uploads
```
