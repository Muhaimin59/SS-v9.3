# SmartServe V10 — "An Intelligent Service Lifecycle Platform"

**Upgrade path:** SmartServe V9.3 (`7f4f21f`, tag `v9.3-baseline`) → V10 (`v10/V10_VERSION`).
**Strategy:** additive extension. No table dropped, no column removed, no existing route deleted,
no existing template replaced. The V9.3 application keeps working exactly as before; V10 adds a
new package, a new page layer and a small number of hooks into the existing flow.

---

## 1. What V10 adds

### 1.1 Seven interconnected innovation modules

| # | Module | Where it lives | What the user gets |
|---|--------|----------------|--------------------|
| 1 | **Service Passport** | `/passports`, `/passport/<id>`, `/passport/<id>/print`, `/asset/<token>` | A permanent digital identity for every asset: unique Asset ID, secure-token QR sticker, warranty state, care plan, parts history and a printable sticker page. |
| 2 | **Mission Engine** | `/missions`, `/missions/<id>`, `service_missions`, `mission_tasks` | One problem becomes an ordered mission of tasks with dependencies, per-task providers, statuses (PENDING/BLOCKED/READY/ASSIGNED/IN_PROGRESS/COMPLETED/CANCELLED) and a progress rail. |
| 3 | **Service Black Box + Service Certificate** | `/service/<id>/blackbox`, `/certificate/<id>` | Every event, photo, reading, part and money movement for a service in one evidence record, followed by an automatically issued certificate. |
| 4 | **AI Second Opinion** | `/second-opinion/<id>`, `POST /api/second-opinion` | Neutral decision support: possible explanations, supporting evidence, missing information, alternatives and a recommended next step. Never accuses a professional. |
| 5 | **Smart Parts Intelligence** | parts panel on the booking page, `service_parts` | Suggested parts with compatibility, price range, supply mode, warranty and approval state. Anything not provider-confirmed is labelled **estimate**, never fake stock. |
| 6 | **Outcome + Recovery Engine** | `/recovery`, `/recovery/<case>` | A completed service is monitored for 90 days. A recurring fault opens a recovery case linked to the original job, parts, evidence and warranty state. |
| 7 | **Student Skill Passport** | `/provider/skills`, `/admin/v10` | Skills progress TRAINING → SUPERVISED → VERIFIED → RESTRICTED, with certifications, verification requests and matching-engine enforcement. |

### 1.2 Lifecycle visible in the UI

`UNDERSTAND → IDENTIFY → DIAGNOSE → PLAN → MATCH → COORDINATE → SERVICE → VERIFY → RECORD → MONITOR → RECOVER`

Each booking carries a `lifecycle_stage`; the booking workspace renders a lifecycle rail, the
mission page renders task progress, and the black box renders the recorded stages.

### 1.3 Dedicated booking journey

`/book/<service>` is a separate seven-step page (PROBLEM → ASSET → DETAILS → PROVIDER → TIME → PRICE → CONFIRM)
with AI analysis, voice input, media capture, asset selection/creation, saved addresses, provider
preference, scheduling, budget and review. The home page is a lifecycle dashboard, not a form.

### 1.4 Expanded catalogue with enforceable metadata

9 categories, 82 catalogue services (86 service rows in the database including legacy entries),
36 asset types, 5 mission templates. Every service carries: category, subcategory, required skills,
required certification, risk level (LOW/MEDIUM/HIGH/RESTRICTED), allowed provider types, required
equipment, possible parts, estimated duration, pricing model and indicative price band.

### 1.5 Mobile-first app shell, PWA, accessibility

Phone-shaped shell on every viewport, sticky header, bottom navigation, sheets and toasts,
persisted light/dark theme (system preference respected on first visit), short purposeful
animations, `prefers-reduced-motion` support, safe-area insets, no fixed widths that can overflow
at 320 px, installable PWA with offline fallback.

---

## 2. Files changed / added

### 2.1 Modified existing files (all additive)

| File | Change |
|------|--------|
| `app.py` | +84 lines: register the V10 blueprint, `_log_event` mirror hook into the V10 black box, V10 completion hook in `verify_payment`, phone required (10–15 digits, env-configurable) with the raw number stored, `_dashboard_for_role()` routes V10 roles to `/app` and `/pro`, legacy bookings attach to a mission (`attach_legacy_request_to_mission`), and the legacy AI endpoint now falls back to the labelled offline analyser instead of returning 502 when Gemini is unavailable. |
| `database.py` | `init_database()` calls `run_v10_migrations()` inside a try/except so a migration problem can never stop the app from starting. |
| `templates/base.html` | Bottom navigation now points at the V10 destinations (customer: Home/Bookings/Missions/Messages/Profile, provider: Home/Requests/Active Jobs/Messages/Profile, admin: Ops/V10). Nothing else changed. |
| `templates/register.html` | Compulsory phone field with client-side validation and an explanation of why it is required. |
| `requirements.txt` | `qrcode[pil]`, `Pillow` (QR generation + upload handling). |
| `.env.example` | V10 env block (dev OTP, SMS provider, phone length, token lifetimes). |
| `DEMO_ACCOUNTS.md` | V10 demo notes (DEMO passports, student provider, seeded catalog links, dev OTP warning). |

### 2.2 New files

```
v10/__init__.py          V10_VERSION, register(), migrate()
v10/catalog.py   1029 ln  categories, 82 services, 36 asset types, 5 mission templates, legacy metadata map
v10/schema.py     944 ln  22 new tables, additive columns, idempotent seeders
v10/ai.py         757 ln  structured Gemini layer + deterministic offline analyser, mission planner,
                          parts suggester, second opinion, recovery classifier, maintenance planner
v10/engine.py    2106 ln  lifecycle domain logic: passports, QR, missions, black box, certificates,
                          second opinion, parts, outcomes, recovery, skills, eligibility, hooks
v10/routes.py    3249 ln  the "v10" blueprint — 76 routes (pages + JSON APIs + PWA)

templates/v10/            27 templates (app shell, home, book, bookings, booking, mission, missions,
                          assets, asset, asset_public, asset_print, blackbox, certificate,
                          second_opinion, recovery, recovery_case, profile, verify, pro, mission_brief,
                          skills, messages, categories, category, search, admin, offline)

static/css/v10.css  523 ln  design system: tokens, light/dark, layout, components, animations, print
static/js/v10.js    330 ln  theme, toasts, v10Api, sheets, live search, voice, stepper, chat bridge, SW
static/sw.js         59 ln  conservative service worker (static cache-first, pages network-first)
static/manifest.webmanifest PWA manifest with shortcuts
static/icons/icon-192.png, icon-512.png, icon-maskable-512.png  branded app icons

tests/v10_smoke_test.py   page/API sweep (36 checks)
tests/v10_workflows.py    end-to-end lifecycle (44 checks)
tests/legacy_regression.py V9.3 regression sweep (25 checks)
```

---

## 3. Database changes (additive only)

**22 new tables:** `assets`, `asset_qr_tokens`, `asset_service_history`, `asset_maintenance_schedule`,
`service_missions`, `mission_tasks`, `service_evidence`, `service_certificates`,
`second_opinion_requests`, `second_opinion_results`, `service_parts`, `service_outcomes`,
`service_recovery_cases`, `student_skills`, `student_skill_verifications`, `provider_certifications`,
`service_categories`, `phone_otp_codes`, `user_addresses`, `notification_preferences`,
`service_catalog_feedback`, plus indices.

**Columns added** (never removed):

* `service_requests`: `asset_id`, `mission_id`, `mission_task_id`, `lifecycle_stage`,
  `ai_structured_json`, `budget_customer`, `parts_cost`, `service_fee`, `provider_diagnosis`,
  `provider_work_performed`, `before_evidence_paths`, `recovery_case_id`, `second_opinion_status`,
  `catalog_metadata_json`, `safety_flags`
* `users`: `phone_verified`, `phone_verified_at`, `email_verification_sent_at`, `address_line`,
  `city`, `state`, `notification_prefs_json`, `profile_completed`, `last_login_at`, `verification_note`
* `providers`: `provider_type`, `student_status`, `service_radius_km`, `languages`, `equipment`,
  `response_minutes`, `kyc_verified_at`, `verification_score`, `headline`, `availability_json`
* `services`: `category_key`, `subcategory`, `required_skills`, `required_certification`,
  `risk_level`, `allowed_provider_types`, `required_equipment`, `possible_parts`,
  `estimated_duration`, `pricing_model`, `keywords`, `asset_types`, `is_popular`, `is_active`,
  `min_price_hint`, `max_price_hint`
* `service_warranties`: `asset_id`, `certificate_id`, `terms_source`

**Seeding** is idempotent and runs on every start: 9 categories, 82 catalogue services,
36 asset types, 3 DEMO passports for the demo customer, a DEMO student skill passport for
`meera.electrical`, and DEMO catalogue links for the demo providers. Demo rows are labelled
`DEMO` / `DEMO SEED` and never mixed into real accounts.

**Migrations and how to reset:** migrations are additive and automatic. To start clean, delete the
SQLite file and restart (`SMARTSERVE_DB_PATH` controls its location).

---

## 4. API changes

All new endpoints live under the existing app; no V9.3 endpoint was renamed or removed.
Full list: `GET /api/v10/status`.

**Lifecycle / booking**
`POST /api/v10/bookings` (mission + first service request in one step) · `POST /api/ai/analyze-v10`
(problem → analysis, mission plan, parts) · `GET /api/missions` `POST /api/missions` ·
`GET /api/missions/<id>` `GET /api/missions/<id>/tasks` · `POST /api/missions/<id>/tasks/<task>/book`

**Passports**
`GET|POST /api/assets` · `GET|PUT|PATCH /api/assets/<id>` · `GET /api/assets/<id>/passport` ·
`GET /api/assets/<id>/history` · `GET|POST /api/assets/<id>/qr` (PNG or `?format=json`) ·
`GET /api/assets/<id>/qr.png` · `POST /api/assets/<id>/maintenance/<plan>`

**Evidence, black box, certificates**
`GET /api/service/<id>/blackbox` · `GET|POST /api/services/<id>/evidence` · `GET /api/certificate/<code>`

**Second opinion** `POST /api/second-opinion` · `GET /api/second-opinion/<request_id>`

**Parts** `GET|POST /api/service/<id>/parts` · `POST /api/parts/<id>/approve`

**Outcome & recovery** `GET|POST /api/service/<id>/recovery` · `POST /api/recovery/<case>/action`

**Provider** `GET /api/service/<id>/mission-brief` · `POST /api/service/<id>/accept|decline|clarify`
· `POST /api/service/<id>/propose-diagnosis` · `POST /api/service/<id>/propose-package`
· `GET|POST /api/provider/certifications` · `POST /api/provider/skills/<skill>/request-verification`

**Account** `GET|PUT|POST /api/profile` · `POST /api/profile/photo` · `GET|POST|DELETE /api/addresses`
· `POST /api/preferences` · `POST /api/verify-email/send` · `GET /verify-email/<token>`
· `POST /api/verify-phone/send|confirm` · `GET /api/verification-status`

**Discovery / admin** `GET /api/search` · `GET /api/service-categories` · `GET /admin/v10`
· `POST /admin/v10/skill/<id>/verify` · `POST /admin/v10/certification/<id>/verify|reject`
· `POST /admin/v10/category` · `POST /admin/v10/service`

**Extended (not duplicated):** the legacy `provider-response`, status, proof, verification,
payment, chat, negotiation, matching, SOS, dispute and earnings endpoints are untouched; V10 hooks
mirror their events into the black box.

---

## 5. UI changes

* New app shell `templates/v10/base.html`: sticky header with back/context/title, account menu,
  toast stack, bottom navigation, shared chat drawer (reuses `/api/chat/<id>` and the existing
  Socket.IO connection), install banner, PWA links.
* New pages (27 templates) covering the seven modules, profile/verification, provider hub,
  mission brief, admin console, categories/search and offline fallback.
* Design system `static/css/v10.css`: tokens, dark mode, cards, chips, lifecycle rail, stepper,
  sheets, skeletons, timelines, QR/passport/certificate styling, print styles.
* `static/js/v10.js`: theme persistence, `v10Api`, toasts, sheets, live search suggestions,
  voice input, booking stepper, quiz-free validation, service-worker registration.
* Legacy pages inherit the new bottom navigation; legacy templates are otherwise unchanged.

---

## 6. Environment variables (new)

| Variable | Default | Purpose |
|----------|---------|---------|
| `SMARTSERVE_DEV_OTP` | `0` | `1` = development only: show the phone OTP on screen instead of sending SMS. Must be `0` in production. |
| `SMARTSERVE_PHONE_MAX_DIGITS` | `15` | Longest accepted phone number (minimum is always 10). |
| `SMS_PROVIDER_URL` / `SMS_PROVIDER_KEY` / `SMS_SENDER_ID` | empty | HTTP JSON SMS gateway used for phone verification and alerts. |
| `SMARTSERVE_OTP_TTL_MINUTES` | `10` | Phone OTP lifetime. |
| `SMARTSERVE_EMAIL_TOKEN_MINUTES` | `30` | Email verification link lifetime (signed, single-use). |

Existing variables (`FLASK_SECRET_KEY`, `GEMINI_*`, `RAZORPAY_*`, `SMTP_*`, `APP_BASE_URL`, …)
keep working unchanged.

---

## 7. Setup

```bash
pip install -r requirements.txt          # Flask, Flask-SocketIO, razorpay, Authlib,
                                         # google-genai, qrcode[pil], Pillow
cp .env.example .env                     # set FLASK_SECRET_KEY, GEMINI_API_KEY, SMTP_*, RAZORPAY_*
python app.py                            # http://0.0.0.0:5000
```

Production stays as it was: `gunicorn --workers 1 --threads 100 --timeout 120 app:app`
(`Procfile`), health check `/health`, persistent disk `/var/data` (`render.yaml`).
The V10 migration runs automatically on the first boot of the new code.

Run the test suites:

```bash
python tests/v10_smoke_test.py        # pages + APIs on a throw-away database
python tests/v10_workflows.py         # full lifecycle end to end
python tests/legacy_regression.py     # V9.3 regression sweep
```

---

## 8. Test results (recorded 2026-10-01, sandbox environment)

| Suite | Result |
|-------|--------|
| `tests/v10_smoke_test.py` | **36 / 36 passed** — customer/provider/admin pages, catalogue, search, booking page, assets, missions, verification APIs, PWA manifest + service worker, admin console. |
| `tests/v10_workflows.py` | **47 / 47 passed** — asset + QR → public passport → booking → mission → provider brief → accept → diagnosis/part/evidence → part approval → arrival code → completion proof → customer verification → payment gate → certificate → recovery case → recovery action → OTP verification → skill passport → admin skill verification, lifecycle report numbers and an admin risk-level change. Includes explicit checks that the second opinion never uses accusatory language, that public passport pages expose no phone/email, and that a student provider is refused HIGH-risk work. |
| `tests/legacy_regression.py` | **25 / 25 passed** — `/`, `/login`, `/register`, `/health`, `/request-service`, customer dashboard, legacy AI analyse-and-create, provider dashboard/profile/portfolio/earnings/reviews, provider public profile, admin operations, fraud scan, matching status, chat/status APIs, logout. |
| Live HTTP run | Home, `/categories`, `/search`, `/book/1`, `/bookings`, `/missions`, `/passports`, `/recovery`, `/messages`, `/profile`, `/verify`, `/pro`, `/provider/skills`, `/admin/v10`, `/sw.js`, `/manifest.webmanifest` all serve correctly; booking → provider accept verified over real HTTP with the Flask server on `0.0.0.0:5000`. |

Recorded database state after migration: 46 tables, 86 services, 9 categories, 3 DEMO passports,
76 V10 routes.

---

## 9. Known limitations (honest list)

1. **Gemini is not configured in this sandbox.** Every AI surface therefore returns its
   deterministic offline analysis, labelled `RULE_BASED` / "Preliminary assessment (offline rules)".
   With `GEMINI_API_KEY` set, the same code paths call Gemini and label the result `AI`. No AI result
   is ever presented as a confirmed diagnosis. As a safety net, the legacy AI booking endpoint now
   falls back to the labelled offline analyser instead of failing with HTTP 502.
2. **Razorpay keys are not configured here.** Payment verification returns HTTP 503 and refuses to
   mark anything paid (unchanged V9.3 behaviour, kept deliberately — no fake payments). Certificate
   issuance after completion was verified by writing the same fields the payment flow writes.
3. **No live parts-supplier API exists.** Parts intelligence is labelled `estimate` unless a
   provider confirmed the part; price bands and compatibility are indicative, and availability is
   shown as estimated/simulated rather than as real stock.
4. **Phone/SMS:** without SMS provider credentials, phone verification can only be exercised with
   `SMARTSERVE_DEV_OTP=1`, which shows the code on screen. Verification fails closed otherwise; it
   never silently marks a number verified.
5. **Email verification** requires SMTP. Without it, the API returns a one-time link in the response
   (clearly marked) so the flow can still be completed and tested.
6. **Demo data:** DEMO passports, DEMO certifications and the student demo provider are seed data for
   the `*.demo` accounts and are labelled as such everywhere in the UI.
7. **Pincode geocoding** uses the existing public postal lookup; when it is unreachable the app
   falls back to a PIN-code service area (as before) and prints a warning — no fake GPS.
8. **Pre-existing V9.3 issues were not "fixed"** and are recorded as found: the AI endpoint required
   the `google-genai` package to be installed (now handled with the labelled fallback), and the
   `/api/matching/status/<id>` endpoint returns 403 for requests the caller does not own (correct
   authorisation, not a defect).
