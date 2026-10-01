# SMARTSERVE V10 IMPLEMENTATION REPORT

**Project:** SmartServe V9.3 → V10 "An Intelligent Service Lifecycle Platform"
**Repository:** `/home/user/SS-v9.3` · branch `arena/01a0f7d3-ss-v9-3` · baseline commit `7f4f21f` (tag `v9.3-baseline`)
**Report date:** 2026-10-01
**Companion documents:** `SMARTSERVE_V10_CHANGELOG.md`, `SMARTSERVE_V10_IMPLEMENTATION_PLAN.md`

---

## A. Executive summary

SmartServe V10 extends the existing V9.3 application instead of replacing it. A new `v10/`
package (6 modules, ~8 100 lines) adds a lifecycle layer that runs through the same database,
the same authentication, the same Socket.IO channel and the same booking records that V9.3 already
uses. The result is seven interconnected modules — Service Passport, Mission Engine, Service Black
Box + Certificate, AI Second Opinion, Smart Parts Intelligence, Outcome + Recovery, and the Student
Skill Passport — wrapped in a mobile-first app shell with its own catalogue, booking stepper,
profiles, verification centre, provider mission brief and operations console.

Nothing was removed: the V9.3 routes, tables, templates and APIs are all still present and were
verified by a dedicated regression suite. Demo, smoke and end-to-end suites pass
(36/36, 44/44, 25/25), and the app was exercised over real HTTP on `0.0.0.0:5000`.

---

## B. Objectives and scope

The brief asked for a V10 that:

1. delivers seven **interconnected** innovation modules (not isolated pages);
2. makes the whole lifecycle visible in the UI (UNDERSTAND → … → RECOVER);
3. moves booking to a dedicated page with a seven-step stepper;
4. expands the catalogue with machine-readable metadata that the matching engine can enforce;
5. reskins the product as a mobile-first app shell with bottom navigation, light/dark mode,
   purposeful animation, accessibility and PWA support;
6. makes phone numbers compulsory at registration, adds email/phone verification, and gives
   customers and providers real profile pages;
7. gives providers a rich mission brief before accepting and an active-job workspace after;
8. exposes new APIs under `/api/assets*`, `/api/missions*`, `/api/services/<id>/evidence`,
   `/api/second-opinion`, `/api/service/<id>/recovery`, `/api/service/<id>/mission-brief`,
   `/api/service-categories` — extending rather than duplicating existing routes;
9. keeps QR codes token-only, enforces ownership authorisation, and validates uploads;
10. preserves the existing negotiation/pricing model while distinguishing every price type;
11. extends the admin console to missions, recovery, categories, skills, risk and certifications;
12. leaves every existing feature working, with any pre-existing breakage documented honestly;
13. ships a changelog and this report.

All thirteen are delivered; the limitations that remain are environmental (no Gemini key, no
Razorpay key, no SMS gateway, no supplier parts API in this sandbox) and are listed in section R.

---

## C. Starting-point audit (what V9.3 already had)

| Area | V9.3 state found | V10 decision |
|------|------------------|--------------|
| Framework | Flask + Flask-SocketIO + SQLite (`database.py`), 3 966-line `app.py` | keep exactly; extend with a blueprint |
| Auth | session-based, email login, Google OAuth, hashed passwords, roles customer/provider/admin | keep; add compulsory phone + verification columns |
| Booking | single long `request_service.html` form with AI analysis, matching waves, offers, negotiation | keep for compatibility; add `/book/<service>` stepper that writes the same `service_requests` row |
| Matching | `matching.py` (waves, offers, TTL, haversine, PIN_TEST_MODE) | keep; gate candidates through `engine.provider_eligibility()` |
| AI | `ai_service.py` single Gemini call returning a fixed JSON blob | keep for legacy; add `v10/ai.py` with structured multi-task AI + labelled offline fallback |
| Chat/calls | `/api/chat/<id>`, Socket.IO rooms, `realtime.js` | reuse; the V10 drawer joins the same rooms |
| Payments | Razorpay order/verify, fails closed without keys | untouched |
| Trust features | SOS, trusted contacts, disputes, fraud signals, warranties, recurring bookings, favourites, portfolio, earnings, reviews | untouched; surfaced in the V10 profile/provider hub |
| UI | `base.html` + 23 templates, `style.css` (3 420 lines), `app-ui.css`, dark mode, i18n | keep; V10 shell is additive, legacy base only changed its bottom nav |
| Catalogue | 9 legacy services, free-text categories | extended with 82 services carrying risk/skill/certification metadata |
| Admin | operations console, KYC review, disputes, SOS, fraud scan | kept; new `/admin/v10` console for V10 entities |

Audit artefacts: table inventory (46 tables after migration), route inventory (V9.3 routes +
76 V10 routes), template inventory, and the deferred-feature list recorded in the changelog.

---

## D. Architecture — extension, not rewrite

```
app.py (V9.3, +84 lines of hooks)
 ├─ register_v10(app)            ← v10/__init__.py
 │    └─ Blueprint "v10"         ← v10/routes.py  (76 routes: pages + JSON APIs + PWA)
 │         ├─ v10/engine.py      ← domain logic (passports, missions, black box, recovery, skills)
 │         ├─ v10/ai.py          ← Gemini structured layer + deterministic offline analyser
 │         ├─ v10/catalog.py     ← catalogue, asset types, mission templates, legacy metadata
 │         └─ v10/schema.py      ← additive migrations + idempotent seeds
 ├─ database.py init → run_v10_migrations()
 ├─ _log_event()  → engine.on_request_event()   (mirror into the black box)
 ├─ verify_payment() → engine.on_request_completed() (certificate + warranty + passport update)
 └─ create-request paths → engine.attach_legacy_request_to_mission()
```

Design rules followed throughout:

* **Additive only.** New tables/columns; no drops, no renames, no destructive migration.
* **Reuse before create.** One `service_requests` row is still the unit of work; missions wrap it.
  Chat, calls, matching, negotiation, payments, SOS and reviews are reused as-is.
* **Fail closed on trust.** Payments, KYC, OTP and skill gating never claim success they cannot prove.
* **Label uncertainty.** Every AI or estimated output carries `data_source` and a visible label.

---

## E. The seven modules

### E1. Service Passport
Every asset gets an `asset_uid` (`AST-XXXXXXXX`), a health score, warranty state derived from
`warranty_end`, a care plan and a service timeline built from `asset_service_history` plus linked
service requests. `/asset/<token>` is the public view: it shows asset type, brand, health, an
anonymised owner label, service summary and care plan — never phone numbers, addresses, prices of
past repairs or the customer's identity.

### E2. Mission Engine
`engine.create_mission()` plans the work (Gemini when configured, deterministic planner otherwise)
into `mission_tasks` with order, dependencies, required skill, risk, estimated cost and provider.
Tasks become bookable as soon as their dependency is complete; the mission status is recomputed from
its tasks. Legacy bookings are wrapped into a single-task mission automatically, so old and new
records share one timeline.

### E3. Service Black Box + Certificate
`service_evidence` stores every stage: AI assessment, customer description, provider diagnosis,
before/after photos, parts, money events, verification and completion proof. The black box page
renders it as a single auditable record. When a service completes and payment is verified,
`engine.issue_certificate()` writes a `service_certificates` row with the problem, diagnosis, work,
parts, all price types, verification flags, warranty and `data_source`, and updates the passport.

### E4. AI Second Opinion
Neutral decision support built from the asset history, previous repairs, parts, the provider's
diagnosis, the quoted amount and the customer's question. The output separates
*possible explanation*, *supporting evidence*, *information still required*, *alternative
possibilities*, *recommended next step* and *safety notes*, and ends with a disclaimer. The prompt
and the offline analyser both forbid accusatory language; the workflow test asserts that the
generated text contains no accusation words.

### E5. Smart Parts Intelligence
`service_parts` records part name, number, compatibility, quantity, price band, supply mode
(provider/customer/marketplace), approval status, warranty days, status and `data_source`
(`ESTIMATE`, `PROVIDER`, `DEMO`). Customer approval is explicit (`/api/parts/<id>/approve`), and
estimated items are labelled "estimate — availability and price are indicative, not a live supplier
quote".

### E6. Outcome + Recovery
`service_outcomes` starts a 90-day monitoring window when a service completes. Recurrence creates a
`service_recovery_cases` row that links the new complaint to the original request, asset, parts,
evidence and warranty status, and offers neutral recovery options (revisit request, return to
provider, open operations review, close). The passport records the case so the asset's real history
is never hidden.

### E7. Student Skill Passport
`student_skills` tracks skill level, supervised/verified job counts, verification method and status
(`TRAINING → SUPERVISED → VERIFIED`, `RESTRICTED` for unsafe scopes). `provider_certifications`
holds credentials with review status. `engine.provider_eligibility()` is called by the matching
layer and by the mission-brief view, so an unverified student simply cannot be offered or accept
high-risk work.

---

## F. Lifecycle mapping

| Stage | Implementation |
|-------|----------------|
| UNDERSTAND | free-text problem, voice input, photos, AI hero card on home |
| IDENTIFY | catalogue + smart search + AI service identification |
| DIAGNOSE | AI assessment (`ai_structured_json`), later the provider's diagnosis |
| PLAN | `mission_tasks` plan with dependencies and required skills |
| MATCH | existing matching wave + `provider_eligibility()` skill gate |
| COORDINATE | mission progress, shared chat, clarification questions, packages |
| SERVICE | arrival code → IN_PROGRESS → work → parts → evidence |
| VERIFY | completion proof + customer verification photo |
| RECORD | black box timeline + Service Certificate + passport history |
| MONITOR | `service_outcomes` 90-day window, maintenance schedule reminders |
| RECOVER | recovery case with warranty context and neutral options |

---

## G. Catalogue and skill gating

82 services across 9 categories (Home Repair, Appliance & Electronics, Cleaning & Pest Control,
Beauty & Personal Care, Home Improvement, Moving & Support, Vehicle Services, Education & Digital,
Pet Services) with 36 asset types. Each service declares risk level, required skills, required
certification, allowed provider types, equipment, possible parts, duration, pricing model and price
band. Skill-relevant examples: CCTV/smart-home, welding, gas appliances, pest control (licence),
waterproofing, two-wheeler and car mechanics, disinfection and interior design are
`professional`-only and carry certification requirements.

`provider_eligibility()` rules (all covered by tests):

* LICENSED service (`required_certification` set) → a **verified certification** is mandatory.
* `RESTRICTED` risk → verified skill row or verified certification.
* Student/trainee provider → each capability needs its own verified skill.
* HIGH risk, no certification → approved/KYC-verified professional, otherwise refused with a
  plain-language reason.
* LOW/MEDIUM risk → any provider offering the service.

Demo data is coherent with these rules: `meera.electrical` is deliberately a trainee so the demo can
show a real refusal and the path to unlock it (`/provider/skills` → request verification →
`/admin/v10` → verify).

---

## H. Booking journey

`/book/<service>` is a seven-step page: PROBLEM (description, media, voice, optional AI analysis),
ASSET (existing passport / create inline / none), DETAILS (PIN code, address, saved addresses),
PROVIDER (CHOOSE or AUTO match), TIME (now or scheduled), PRICE (customer budget, parts expectation
and the explicit pricing ladder), CONFIRM (summary and consent). Submission posts multipart
`FormData` to `/api/v10/bookings`, which creates the mission, its tasks, the first service request,
suggested parts and the lifecycle record in one transaction. The home page contains no booking form —
the BOOK NOW buttons navigate to the catalogue.

---

## I. Mission engine detail

`mission_tasks.status` ∈ PENDING / BLOCKED / READY / ASSIGNED / IN_PROGRESS / COMPLETED / CANCELLED.
`depends_on_task_id` drives readiness; `engine.refresh_mission_state()` recomputes mission progress.
A task with no booking yet can be booked directly (`/api/missions/<id>/tasks/<task>/book`), which
pre-fills the booking page. Providers see the mission context in the brief: what other trades are
involved, which task is theirs, and that only their task will be assigned to them.

---

## J. Service Passport, QR and security

* QR payload is a single random URL-safe token from `asset_qr_tokens`. No name, phone, address,
  asset details or history is encoded — a QR photo leaks nothing.
* Tokens can be rotated (`POST /api/assets/<id>/qr {"rotate": true}`); rotation revokes the old token
  so a lost sticker becomes useless.
* Every passport API checks ownership; providers only get access when they are assigned to a service
  for that asset (`service_requests.asset_id AND provider_id = me`), administrators excepted.
* Public pages expose type, brand, health, an alias and a service summary only.
* Uploads go through `_save_upload()` / `_upload_many()`: extension allow-list, size cap, generated
  file names, per-request limits. Client-supplied paths are never trusted.

---

## K. Black box and certificates

Timeline sources: `service_requests` fields, `service_events`, `service_evidence`, `service_parts`,
`service_certificates`. The `_log_event` hook in V9.3 ensures nothing that happens in a legacy flow
is lost — the workflow test verifies that legacy status changes appear as V10 black-box entries.
Certificate content: certificate code, customer/provider display names, asset, problem, AI assessment
+ provider diagnosis + work performed, parts with their `data_source`, the full price ladder
(estimate, budget, negotiated, parts, fee, final), verification flags, evidence count, warranty
window and data source. It is printable to PDF from the browser.

---

## L. AI layer

`v10/ai.py` provides `analyze_problem`, `decompose_mission`, `suggest_parts`, `second_opinion`,
`classify_recovery` and `maintenance_suggestions`. Each function tries Gemini (structured JSON
contract) and falls back to a deterministic rules engine, returning `data_source`
(`AI` / `RULE_BASED`), `confidence`, `model_used` and a `disclaimer`. The UI prints the label next to
every AI output, and offline results are described as "preliminary classification, not a diagnosis".
`SAFETY_KEYWORDS` force safety notes for gas, electrical, fire and structural complaints.

---

## M. Parts intelligence

Suggested when a booking is created (from the service's `possible_parts` and the problem text),
proposed by providers after diagnosis, approved by the customer, and recorded with warranty days.
`parts_summary` distinguishes estimated vs approved cost and pending approvals so the booking page
and certificate can show them separately. Nothing is ever shown as "in stock".

---

## N. Outcome and recovery

Outcomes are created on completion (`on_request_completed`) with `monitored_until = +90 days`.
`recovery_candidates()` lists completable services with their warranty state and any open case.
`POST /api/service/<id>/recovery` classifies the recurrence (same fault / related / new), opens a
case, links evidence and notifies the original provider through the existing chat/notification path.
Recovery actions are recorded in the black box, and an active warranty is displayed with a neutral
statement of what it may cover — never an accusation.

---

## O. Student skill passport

Provider page `/provider/skills` shows progress stages (training → supervised → verification →
feedback → assessment → higher eligibility), eligible vs restricted skills with reasons, an overall
level, certifications and verification history, plus a request-verification action. `/admin/v10`
lists the verification queue and lets an administrator verify, restrict or reject a skill, approve or
reject certifications, and create categories.

---

## P. Accounts: phone, verification, profiles

* **Registration:** phone number compulsory (10–15 digits, `SMARTSERVE_PHONE_MAX_DIGITS`), validated
  server-side and in the browser; the number is stored for safety tooling. Existing friendly errors
  are preserved.
* **Verification centre** (`/verify`): email verification via `itsdangerous` signed single-use token
  (30 minutes, resend cooldown 120 s, degrades to a secure link if SMTP is down), and phone
  verification via a 6-digit OTP (SHA-256 hashed, 10-minute TTL, 5 attempts) sent through the
  configured SMS gateway. `SMARTSERVE_DEV_OTP=1` shows the code on screen and is loudly labelled;
  without it and without SMS credentials, verification fails closed.
* **Customer profile** (`/profile`): photo upload/preview/replace/remove, personal details, language,
  saved addresses (default + delete), notification preferences, favourites, recent bookings,
  warranties, recovery cases, trust contact, verification shortcuts and security/sign-out.
* **Provider profile:** the existing rich V9.3 editor (KYC, portfolio, services, radius) is kept and
  joined by the V10 skill passport and availability/response metadata; provider badge data continues
  to come from `provider_badges`/KYC review.

---

## Q. Provider experience

`/pro` is the provider home: KPI row (completed jobs, net earnings, rating, skill level), skill
restriction alerts, new requests, active jobs and recent history. New requests open a **mission
brief** containing the problem text and photos, the asset and its history, the AI assessment with
its data-source label, mission context and the provider's task, budget and schedule, safety notes,
required equipment/certification, expected parts with estimates, and the message count — followed by
Accept / Ask a question / Propose diagnosis / Propose package / Decline. After acceptance, the brief
becomes the **job workspace**: lifecycle rail, status and payment card, mission task list, parts with
approval state, evidence capture (diagnosis/before/work/after), second-opinion link, arrival code,
completion proof and certificate.

---

## R. Admin, pricing, security, and limitations

**Admin `/admin/v10`:** counts for assets, missions, active missions, certificates, open recovery,
second opinions, pending skills, pending certifications and parts; mission table, recovery cases,
skill verification queue with actions, certification review, and category/risk/service editing.
Disputes, fraud signals, SOS and payments remain in the V9.3 operations console, unchanged.

**Pricing:** V9.3 negotiation is untouched. V10 keeps every price visible and distinct — AI range,
customer budget, provider estimate/quote, negotiated amount, parts cost, SmartServe fee and final
approved amount — and the certificate states that the final amount was customer-approved and never
changed silently. Parts require explicit approval before they enter the total.

**Security:** token-only QR, ownership authorisation on every asset/evidence/parts/recovery endpoint,
role-gated provider and admin routes, upload validation, server-side recalculation of money and
ownership (no client trust), secrets via environment variables only.

**Limitations (environment and product):**
no Gemini key, no Razorpay key, no SMS gateway and no supplier parts API in this deployment, so AI
surfaces report `RULE_BASED`, `verify_payment` refuses to mark anything paid (503), phone
verification requires dev OTP or SMS credentials, and parts availability is estimated. Pincode
geocoding depends on the public postal service with a PIN-area fallback and no fake GPS. Demo
passports/certifications are labelled DEMO. Pre-existing V9.3 quirks are documented rather than
silently "fixed" (see changelog §9).

---

## S. Verification, evidence, and the coordinator demo flow

### S1. How the work was verified

* `tests/v10_smoke_test.py` — 36 checks, all pass (pages, APIs, PWA, admin, role gating).
* `tests/v10_workflows.py` — 44 checks, all pass; includes the real legacy completion chain
  (ARRIVED → confirmation code → completion proof → customer verification photo → payment gate),
  certificate issuance, recovery opening/action, OTP verification, skill gating refusal and
  privacy assertions on the public passport page.
* `tests/legacy_regression.py` — 25 checks, all pass (V9.3 pages and APIs still work).
* Live server run on `0.0.0.0:5000`, exercised with real HTTP requests for both roles, plus
  `/sw.js`, `/manifest.webmanifest`, `/health`.

Reproduce:

```bash
python tests/v10_smoke_test.py && python tests/v10_workflows.py && python tests/legacy_regression.py
```

### S2. Coordinator demo flow (~12 minutes)

Demo accounts (password `SmartServe@123` for all): `customer@smartserve.demo`,
`arjun.plumbing@smartserve.demo`, `meera.electrical@smartserve.demo`, `admin@smartserve.demo`.
Development OTP: start with `SMARTSERVE_DEV_OTP=1` to show phone verification without SMS.

1. **Home (`/app`)** — sign in as the customer. Point out the lifecycle dashboard: greeting,
   search, AI hero card, active missions, live bookings, passport carousel, categories and
   recommended providers. Note that booking is a separate page.
2. **Catalogue (`/categories`)** — open *Appliance & Electronics*, show risk/skill/certification
   chips, then open a service to land on `/book/<service>`.
3. **Booking stepper (`/book/...`)** — describe a problem ("AC is not cooling and rattles"),
   optionally analyse with AI (shows the `RULE_BASED` label in this environment), pick the DEMO
   passport, enter PIN 560001, choose AUTO match, set a budget, review the price ladder and confirm.
   The booking page opens with the lifecycle rail.
4. **Mission (`/missions`)** — show the planned tasks, dependency state and progress.
5. **Provider side** — in a second window sign in as `arjun.plumbing`, open `/pro`, click the new
   request to see the **mission brief** (problem, photos, asset history, AI assessment, budget,
   safety, expected parts with estimates, mission context), then Accept.
6. **Job workspace** — as the provider: propose a diagnosis, propose a part, add before evidence,
   mark arrival. As the customer: approve the part, read the **Second Opinion** page (neutral
   wording), and use the arrival code flow.
7. **Completion** — provider submits completion proof; customer uploads a verification photo; the
   certificate page shows the full record. If Razorpay is not configured, explain that payment
   verification fails closed by design (no fake payments) and show the certificate for the completed
   service created by the seeder/tests.
8. **Service Passport (`/passports`)** — show the DEMO asset, printable QR sticker, health score,
   timeline, parts history and care plan; open `/asset/<token>` in a private window to prove the
   public view exposes no personal data.
9. **Recovery (`/recovery`)** — report that the same problem returned; show the case linking to the
   original black box, parts and warranty status with neutral options.
10. **Skill gating** — as `meera.electrical` open `/provider/skills`: her HIGH-risk electrical skill
    is restricted and the matching engine refuses such jobs. Then as `admin@smartserve.demo` open
    `/admin/v10` and verify the skill; the restriction is lifted on the next match.
11. **Verification centre (`/verify`)** — show phone verification (dev OTP visible and labelled) and
    email verification with its resend cooldown and SMTP-down fallback link.
12. **Profile (`/profile`)** — photo, addresses, language, notifications, favourites, warranties,
    recovery cases and trusted contact in one place. Close on the PWA prompt and the offline page.

### S3. Where to look in the code

| Question | File |
|----------|------|
| How is the catalogue defined? | `v10/catalog.py` |
| What tables/columns were added? | `v10/schema.py` (`create_v10_tables`, `add_v10_columns`) |
| How does the lifecycle logic work? | `v10/engine.py` |
| Where is the AI + fallback? | `v10/ai.py` |
| Where are the pages and APIs? | `v10/routes.py` |
| How did V9.3 change? | `git diff app.py database.py templates/base.html templates/register.html` |
