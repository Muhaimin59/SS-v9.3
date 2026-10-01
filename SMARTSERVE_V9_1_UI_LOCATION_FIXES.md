# SmartServe V9.1 — UI Language, GPS Accuracy & Provider Profile Fixes

Base: user-provided `v9(3).zip`.

## Fixed

### 1. Full UI language switching
- Added a global client-side localization layer for English, Hindi, Kannada, Tamil, Telugu and Marathi.
- The saved language preference is now applied immediately instead of only changing `<html lang>`.
- The language setting is available from the global account menu for customer, provider and admin accounts.
- Dynamic UI fragments inserted by Socket.IO/fetch are translated automatically when their application-owned text matches the localization dictionary.
- Input placeholders, titles and ARIA labels are localized where application-owned.
- User/provider-entered names, descriptions and chat content are not translated automatically.

### 2. More accurate customer location for provider discovery
- Before provider matching and provider-list refreshes, the latest recent browser GPS position is copied from the authenticated user's GPS record into the service request.
- Provider discovery therefore uses current GPS when available rather than relying only on the PIN-code centroid.
- The searching map uses the current customer GPS coordinate when available and falls back to the request PIN coordinate only when current GPS is unavailable.

### 3. Live tracking GPS improvements
- High-accuracy browser GPS requests use a shorter cache age so stale browser positions are less likely to be reused.
- Accuracy circles are no longer silently capped at 800 m; the UI can represent a larger real reported accuracy area.
- Double-clicking a live participant marker recenters the map on that participant's latest real coordinates at a close zoom level.
- Existing server-side GPS, Socket.IO and OSRM routing remain intact.

### 4. Provider profile visibility
- Fixed the profile hero/avatar clipping caused by the hero's `overflow:hidden` combined with the negative avatar offset.
- Added dedicated profile-page spacing and z-index handling below the sticky navigation.
- Adjusted desktop/mobile avatar positioning so the complete profile header remains visible.

## Validation
- `python -m py_compile app.py ai_service.py database.py matching.py` passed.
- `node --check static/js/i18n.js` passed.
- `node --check static/js/realtime.js` passed.
- Runtime HTTP testing requires the project's Python dependencies to be installed in the execution environment.
