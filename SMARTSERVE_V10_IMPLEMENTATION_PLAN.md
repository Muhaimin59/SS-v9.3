# SmartServe V10 — internal implementation plan and decision log

This is the working plan that drove the V9.3 → V10 upgrade, recorded with the decisions taken
during implementation. It exists so the next engineer can see *why* the code looks the way it does,
what was deliberately deferred, and how each step was verified.

---

## 1. Ground rules (non-negotiable)

1. **Extend, never rebuild.** The V9.3 app keeps its architecture, routes, tables, templates and
   deployment. V10 is an additive package plus a small number of hooks.
2. **Never break a working feature.** No table/column drops, no route removals, no template
   replacement. Backward compatibility first: a failure in a V10 feature must not affect booking,
   payments, chat, matching, SOS or admin.
3. **Never present fake data as real.** AI, geolocation, payments, verifications, parts availability
   and seed data are labelled and fail closed where trust is involved.
4. **Reuse before create.** If a table or endpoint exists, extend it instead of duplicating.
5. **Test every module after building it**, then re-run the whole suite.

---

## 2. Audit phase (completed before writing code)

* Read `app.py` route inventory, `database.py` schema, `matching.py`, `ai_service.py`, every
  template, `style.css`/`app-ui.css`, `ui.js`/`realtime.js`, deploy files.
* Classified the existing surface: working / degraded-without-keys / duplicate / legacy-but-used.
* Recorded the constraints that shaped the design:
  * `service_requests` is the unit of work and is referenced by chat, payments, tracking,
    negotiation, reviews, disputes and warranties → V10 must wrap it, not replace it.
  * `_log_event()` is the single funnel for service events → one hook there captures everything.
  * `ai_service.py` exposes one unstructured call → the structured V10 layer must be separate and
    must degrade to a labelled offline analyser.
  * Payment verification is signature-checked and fails closed → the demo cannot "fake" payment;
    certificate issuance had to be tested through the same fields the payment flow writes.
* Produced the module → table → route → template map that became section E/F of the report.

**Baseline protection:** the pre-work commit was tagged `v9.3-baseline`, and the sandbox venv
(`/home/user/.venv`) was created so the verified environment is reproducible.

---

## 3. Build order and verification per phase

| Phase | Deliverable | Verification |
|-------|-------------|--------------|
| 1 | `v10/catalog.py` (categories, services, asset types, mission templates, legacy map) | count/py-compile checks; catalogue query sanity |
| 2 | `v10/schema.py` (22 tables, columns, seeders) | fresh-DB migration run; idempotency re-run; table/column counts |
| 3 | `v10/ai.py` (Gemini + offline analyser) | forced-offline runs; label assertions in the workflow test |
| 4 | `v10/engine.py` (passports, missions, black box, certificates, parts, recovery, skills, hooks) | direct engine calls in the workflow test |
| 5 | `v10/routes.py` (76 routes) | smoke sweep for pages/APIs, then the workflow suite |
| 6 | Templates + `base.html` shell + nav | live server sweep; Jinja render checks on every page |
| 7 | `v10.css` / `v10.js` / PWA files | `node --check`, HTTP 200 for CSS/JS/SW/manifest/icons, offline page |
| 8 | `app.py` hooks + `database.py` migration call + registration phone | legacy regression suite + workflow suite |
| 9 | Docs, changelog, report, demo notes | re-run all suites on the final tree |

Every phase ended with the same ritual: run the app, walk the flow, read the Flask log for
tracebacks/DB errors, and confirm the V9.3 paths still render.

---

## 4. Key decisions

1. **Blueprint, not a fork.** `v10/` registers a Flask blueprint; no route in `app.py` was moved.
2. **One event funnel.** Extending `_log_event()` mirrors legacy activity into the black box in a
   single place instead of sprinkling hooks through 15 call sites.
3. **Legacy booking bridge.** Rather than forcing users through the new stepper, any V9.3 booking
   gets wrapped in a one-task mission (`attach_legacy_request_to_mission`) so old and new records
   share the mission timeline.
4. **QR = token only.** A random token in `asset_qr_tokens`, rotatable, resolving to a page that
   applies its own authorisation rules.
5. **Fail-closed trust surfaces.** Razorpay, OTP and skill gating refuse rather than pretend; the
   development OTP path is env-gated and visibly marked.
6. **Label every inference.** `data_source` travels with AI/parts/seed data and is rendered in the
   UI, so a demo can never be mistaken for a real diagnosis or a real supplier quote.
7. **Skill gating with a human escape hatch.** Professionals are gated by certification/licence and
   risk; students are gated per skill; the admin console can verify a skill to lift a restriction.
8. **Demo coherence.** Demo providers were linked to real catalogue services inside their category,
   and one demo provider is intentionally a trainee so gating can be demonstrated end to end.
9. **Legacy AI resilience.** The old AI booking endpoint used to return 502 when Gemini was
   unreachable; it now falls back to the labelled offline analyser so a booking is never lost.
10. **No new front-end framework.** Server-rendered templates + one CSS file + one JS file keep the
    app consistent with V9.3 and deployable on the existing Render/Gunicorn setup.

---

## 5. Risks identified and how they were handled

| Risk | Handling |
|------|----------|
| Migration failure blocks startup | `run_v10_migrations()` runs in try/except inside `init_database()` and only prints a warning; the column adder is idempotent. |
| Re-running seeds duplicates data | All seeders use `INSERT … ON CONFLICT DO UPDATE` / existence checks. |
| V10 template error breaks a legacy page | V10 templates are separate; the legacy `base.html` change is limited to navigation links. |
| A V10 crash breaks booking | V10 hooks are defensive (try/except) and the legacy paths remain the default. |
| Fake-looking demo data | DEMO labels in UI, `data_source` columns, and "DEMO SEED — not a real licence" issuers. |
| Silent price changes | Parts require explicit approval; certificate prints each price type with its provenance. |
| Over-scoped admin work | V10 console covers V10 entities only; disputes/fraud/SOS stay in the V9.3 console. |

---

## 6. Test plan (as executed)

1. **Smoke** (`tests/v10_smoke_test.py`): every V10 page for every role, key APIs, PWA assets.
2. **Workflow** (`tests/v10_workflows.py`): the complete lifecycle including the real legacy
   completion chain and negative tests (student blocked, public page privacy, neutral language,
   payment fails closed without keys).
3. **Legacy regression** (`tests/legacy_regression.py`): V9.3 pages and APIs across customer,
   provider and admin roles, including the legacy AI booking endpoint.
4. **Manual live run**: Flask server on `0.0.0.0:5000`, real HTTP flow from booking to provider
   acceptance, all pages rendered, log checked for tracebacks.

Results are recorded in `SMARTSERVE_V10_CHANGELOG.md` §8 and the implementation report §S.

---

## 7. Deliberately deferred (with rationale)

* **Live parts supplier integration** — no supplier API available; the schema and UI already carry
  `supply_mode`, `availability` and `data_source` so it can be plugged in without migration.
* **Native Android/iOS packaging** — the PWA (manifest + service worker + offline page) covers the
  installable requirement without adding a build pipeline.
* **Payment escrow changes** — payment behaviour is untouched by design; V10 only records the price
  ladder and the parts that feed the final amount.
* **Camera-based QR scanning inside the app** — an external camera/scanner app opens the token URL;
  adding a JS scanner would mean shipping another vendor bundle, so it stayed out of scope.
* **Full i18n of every new string** — the existing language preference is honoured and stored; the
  new V10 copy is English-only for now and is listed here rather than half-translated.
