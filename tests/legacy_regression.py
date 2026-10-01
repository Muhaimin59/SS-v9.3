"""SmartServe legacy (V9.3) regression check.

Confirms the pre-existing customer/provider/admin experience still renders and
still behaves after the V10 layer was added. Uses a throw-away database.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP_DB = os.path.join(tempfile.mkdtemp(prefix="smartserve-legacy-"), "legacy.db")
os.environ.setdefault("SMARTSERVE_DB_PATH", TMP_DB)
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ.pop("GEMINI_API_KEY", None)

import app as application  # noqa: E402

app = application.app
RESULTS = []


def record(label, ok, note=""):
    RESULTS.append((label, bool(ok), note))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" :: {note}" if note and not ok else ""))


def get(client, url, expect=(200, 302), label=None):
    try:
        r = client.get(url)
        ok = r.status_code in expect
        record(label or f"GET {url}", ok, f"status {r.status_code}")
        return r
    except Exception as exc:  # noqa: BLE001
        record(label or f"GET {url}", False, f"{type(exc).__name__}: {exc}")
        return None


def login(c, email, password="SmartServe@123"):
    return c.post("/login", data={"email": email, "password": password}).status_code in (200, 302)


def main():
    anon = app.test_client()
    customer = app.test_client()
    provider = app.test_client()
    admin = app.test_client()

    print("Legacy regression —", TMP_DB)
    for url in ["/", "/login", "/register", "/health", "/api/ai/status"]:
        get(anon, url)
    get(anon, "/request-service", expect=(200, 302))

    record("legacy demo login", login(customer, "customer@smartserve.demo"))
    record("provider login", login(provider, "meera.electrical@smartserve.demo") or login(provider, "arjun.plumbing@smartserve.demo"))
    record("admin login", login(admin, "admin@smartserve.demo"))

    for url in ["/customer/dashboard", "/request-service", "/provider/profile"]:
        client = customer if url != "/provider/profile" else provider
        get(client, url, expect=(200, 302), label=f"legacy {url}")

    # legacy booking creation through /api/ai/analyze-and-create
    r = customer.post("/api/ai/analyze-and-create", data={"description": "My kitchen tap is leaking badly", "service_id": 1, "pincode": "560001"})
    payload = {}
    try:
        payload = r.get_json() or {}
    except Exception:
        pass
    record("legacy AI analyse-and-create", r.status_code in (200, 302), f"{r.status_code} {str(payload)[:160]}")

    # legacy assets: provider list, marketplace hub, payments/earnings pages
    for url in ["/provider/1", "/provider/1/reviews", "/provider/dashboard",
                "/provider/earnings", "/provider/portfolio", "/provider/reviews-center", "/find-providers/1"]:
        get(provider, url, expect=(200, 302), label=f"legacy {url}")
    for url in ["/admin/operations", "/admin/fraud-scan"]:
        get(admin, url, expect=(200, 302), label=f"legacy {url}")

    # legacy matching APIs must still respond
    r = customer.get("/api/matching/status/1")
    record("legacy /api/matching/status responds", r.status_code in (200, 400, 403, 404), f"status {r.status_code}")
    r = customer.get("/logout")
    record("legacy logout", r.status_code in (200, 302), f"status {r.status_code}")
    r = customer.get("/api/customer/request-status")
    record("legacy /api/customer/request-status responds", r.status_code in (200, 401), f"status {r.status_code}")

    failed = [item for item in RESULTS if not item[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} legacy checks passed")
    for label, _ok, note in failed:
        print("  FAILED:", label, note)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
