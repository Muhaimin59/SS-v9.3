"""SmartServe V10 smoke test.

Runs the application with the Flask test client against a temporary database
and walks every V10 page/API plus the legacy critical paths. Prints a compact
PASS/FAIL report. Usage:

    SMARTSERVE_DEV_OTP=1 /home/user/.venv/bin/python tests/v10_smoke_test.py
"""
from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TMP_DB = os.path.join(tempfile.mkdtemp(prefix="smartserve-v10-test-"), "test.db")
os.environ.setdefault("SMARTSERVE_DB_PATH", TMP_DB)
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ.setdefault("SMARTSERVE_DEV_OTP", "1")
os.environ.pop("GEMINI_API_KEY", None)

import app as application  # noqa: E402  (import after env is set)

app = application.app
app.config.update(TESTING=False, WTF_CSRF_ENABLED=False)

RESULTS: list[tuple[str, str, str]] = []


def record(kind, target, ok, note=""):
    RESULTS.append((kind, target, "PASS" if ok else "FAIL", note))
    if not ok:
        print(f"  FAIL {target}: {note}")


def check(client, kind, target, method="GET", expect=(200, 302), **kwargs):
    try:
        response = getattr(client, method.lower())(target, **kwargs)
    except Exception as exc:  # noqa: BLE001
        record(kind, target, False, f"exception {type(exc).__name__}: {exc}")
        return None
    ok = response.status_code in expect
    note = ""
    if not ok:
        note = f"status {response.status_code}"
        try:
            note += " | " + response.get_data(as_text=True)[:400].replace("\n", " ")
        except Exception:
            pass
    record(kind, target, ok, note)
    return response


def login(client, email, password="SmartServe@123"):
    response = client.post("/login", data={"email": email, "password": password}, follow_redirects=False)
    return response.status_code in (200, 302)


def main():
    print("SmartServe V10 smoke test")
    print("database:", TMP_DB)
    customer = app.test_client()
    provider = app.test_client()
    admin = app.test_client()

    record("auth", "customer login", login(customer, "customer@smartserve.demo"))
    record("auth", "provider login", login(provider, "meera.electrical@smartserve.demo"))
    record("auth", "admin login", login(admin, "admin@smartserve.demo"))

    # ---- V10 customer pages -------------------------------------------------
    for path in ["/app", "/categories", "/search?q=ac", "/bookings", "/missions",
                 "/passports", "/recovery", "/messages", "/profile", "/verify", "/offline"]:
        check(customer, "page", path)

    # ---- catalogue ----------------------------------------------------------
    for key in ["home_repair", "appliance_electronics", "cleaning_pest", "beauty_personal_care"]:
        check(customer, "page", f"/category/{key}")

    # discover a service to book
    with app.app_context():
        from database import get_db_connection
        connection = get_db_connection()
        service = connection.execute(
            "SELECT id, name FROM services WHERE is_active=1 ORDER BY is_popular DESC, id LIMIT 1"
        ).fetchone()
        connection.close()
    if service:
        check(customer, "page", f"/book/{service['id']}")
    else:
        record("page", "/book/<service>", False, "no seeded service")

    # ---- APIs ---------------------------------------------------------------
    check(customer, "api", "/api/v10/status")
    check(customer, "api", "/api/search?q=ac")
    check(customer, "api", "/api/service-categories")
    check(customer, "api", "/api/assets")
    check(customer, "api", "/api/missions", "POST", json={"problem": "smoke test mission"})
    check(customer, "api", "/api/verify-email/send", "POST", json={})
    check(customer, "api", "/api/profile", "POST", json={"phone": "9812345670"})
    check(customer, "api", "/api/verify-phone/send", "POST", json={})
    check(customer, "api", "/api/verify-phone/confirm", "POST", json={"code": "000000"}, expect=(400,))

    # ---- provider pages -----------------------------------------------------
    for path in ["/pro", "/provider/skills", "/messages"]:
        check(provider, "page", path)
    check(provider, "api", "/api/provider/certifications")

    # ---- admin pages --------------------------------------------------------
    check(admin, "page", "/admin/v10")
    check(admin, "api", "/api/v10/status")

    # ---- PWA ----------------------------------------------------------------
    check(customer, "pwa", "/manifest.webmanifest")
    check(customer, "pwa", "/sw.js")

    total = len(RESULTS)
    failed = [item for item in RESULTS if item[2] == "FAIL"]
    print(f"\n{total - len(failed)}/{total} checks passed")
    for kind, target, status, note in failed:
        print(f"  - [{kind}] {target}: {note}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
