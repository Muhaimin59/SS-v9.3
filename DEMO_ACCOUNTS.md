# SmartServe demo accounts

These accounts are seeded automatically when the database is initialized. They are for local/demo testing only.

## Customer
- Email: `customer@smartserve.demo`
- Password: `SmartServe@123`

## Provider accounts
All provider accounts use password `SmartServe@123`.

- `arjun.plumbing@smartserve.demo` — Plumbing
- `meera.electrical@smartserve.demo` — Electrical
- `ravi.carpentry@smartserve.demo` — Carpentry
- `neha.cleaning@smartserve.demo` — Cleaning
- `vikram.appliance@smartserve.demo` — Appliance Repair
- `sana.mobile@smartserve.demo` — Mobile Repair
- `kiran.laptop@smartserve.demo` — Laptop Repair
- `dev.it@smartserve.demo` — Basic IT Support

Run `python3 demo_setup.py` for a repeatable local demo: it places the demo customer and Arjun Plumbing in PIN 585401 and sets Arjun online. The helper does not delete existing data. Replace demo coordinates with real provider locations in production.

## SmartServe V10 demo extras (seeded automatically, clearly marked DEMO)

- The demo customer `customer@smartserve.demo` owns three **DEMO** Service Passports
  (labelled `DEMO DATA` in the UI) with QR tokens, service history and a care plan.
- `meera.electrical@smartserve.demo` is seeded as a **student/trainee** provider to
  demonstrate skill gating: her Skill Passport (`/provider/skills`) shows verified,
  supervised and restricted skills, and the matching engine refuses HIGH-risk work
  until an administrator verifies the skill in `/admin/v10`.
- Every other demo provider now offers real V10 catalogue services inside their
  category so the booking → mission → mission-brief → accept flow can be demonstrated.
- Demo certifications created by the seeder are labelled `DEMO SEED — sample credential
  (not a real licence)`. Nothing seeded is presented as a real credential.
- Development OTP mode (`SMARTSERVE_DEV_OTP=1`) shows the phone code on the
  verification screen. It must be `0` in production.
