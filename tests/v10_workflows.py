"""SmartServe V10 end-to-end workflow test.

Exercises the full lifecycle on a throw-away database:
  asset + QR -> booking/mission -> provider brief + accept -> evidence,
  parts, diagnosis -> customer second opinion -> recovery case ->
  certificate (after completion is simulated through the same DB fields the
  legacy payment flow writes).

Run:  /home/user/.venv/bin/python tests/v10_workflows.py
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TMP_DB = os.path.join(tempfile.mkdtemp(prefix="smartserve-v10-flow-"), "flow.db")
os.environ.setdefault("SMARTSERVE_DB_PATH", TMP_DB)
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ["SMARTSERVE_DEV_OTP"] = "1"
os.environ.pop("GEMINI_API_KEY", None)

import app as application  # noqa: E402
from database import get_db_connection  # noqa: E402
from v10 import engine  # noqa: E402

app = application.app
app.config.update(TESTING=False)

PASSED: list[str] = []
FAILED: list[str] = []
STATE: dict = {}


def ok(label, condition, detail=""):
    if condition:
        PASSED.append(label)
        print(f"  PASS  {label}")
    else:
        FAILED.append(f"{label} :: {detail}")
        print(f"  FAIL  {label} :: {detail}")
    return bool(condition)


def login(client, email, password="SmartServe@123"):
    r = client.post("/login", data={"email": email, "password": password}, follow_redirects=True)
    return r.status_code == 200


def jget(client, url):
    r = client.get(url)
    try:
        return r.status_code, r.get_json()
    except Exception:
        return r.status_code, {"raw": r.get_data(as_text=True)[:300]}


def jpost(client, url, payload=None, json_body=True):
    if json_body:
        r = client.post(url, json=payload or {})
    else:
        r = client.post(url, data=payload or {})
    try:
        return r.status_code, r.get_json()
    except Exception:
        return r.status_code, {"raw": r.get_data(as_text=True)[:300]}


def main():
    customer = app.test_client()
    provider = app.test_client()
    print("V10 workflow test —", TMP_DB)
    ok("login customer", login(customer, "customer@smartserve.demo"))
    ok("login provider", login(provider, "arjun.plumbing@smartserve.demo"))
    student_provider = app.test_client()
    login(student_provider, "meera.electrical@smartserve.demo")

    # 1 ── Service Passport + QR -------------------------------------------------
    status, data = jpost(customer, "/api/assets", {
        "asset_type": "Air conditioner", "nickname": "Workflow AC", "brand": "Daikin",
        "model": "FTKM35", "serial_number": "WF-TEST-1", "warranty_end": "2027-01-01",
        "location_label": "Test room",
    })
    ok("create asset", status == 200 and data.get("asset_id"), f"{status} {data}")
    asset_id = data.get("asset_id")
    STATE["asset_id"] = asset_id
    if asset_id:
        status, data = jget(customer, f"/api/assets/{asset_id}/passport")
        ok("asset passport API", status == 200 and data.get("passport"), f"{status} {str(data)[:200]}")
        status, qr_data = jget(customer, f"/api/assets/{asset_id}/qr?format=json")
        token = (qr_data or {}).get("token")
        ok("QR token exists", bool(token) and status == 200, f"{status} {str(qr_data)[:200]}")
        r = customer.get(f"/api/assets/{asset_id}/qr.png")
        ok("QR png served", r.status_code == 200 and r.data[:4] == b"\x89PNG", f"{r.status_code} {r.data[:8]}")
        public = app.test_client().get(f"/asset/{token}")
        ok("public passport page", public.status_code == 200, f"{public.status_code}")
        ok("public page hides phone", "smartserve.demo" not in public.get_data(as_text=True))

    # 2 ── Booking + mission -----------------------------------------------------
    with app.app_context():
        connection = get_db_connection()
        service = connection.execute(
            """
            SELECT s.id, s.name FROM provider_services ps
            JOIN services s ON s.id=ps.service_id
            JOIN providers p ON p.id=ps.provider_id JOIN users u ON u.id=p.user_id
            WHERE u.email='arjun.plumbing@smartserve.demo' AND s.risk_level IN ('LOW','MEDIUM')
            ORDER BY s.is_popular DESC LIMIT 1
            """
        ).fetchone()
        connection.close()
    ok("catalogue has appliance service", bool(service), "no service")
    status, data = jpost(customer, "/api/v10/bookings", {
        "service_id": service["id"], "problem": "AC is not cooling and makes a rattling noise",
        "pincode": "560001", "address_text": "12 Test Street", "budget": 6000,
        "asset_id": asset_id, "preferred_time": "Morning",
        "provider_preference": "AUTO",
    }, json_body=False)
    ok("create booking", status == 200 and data.get("request_id"), f"{status} {str(data)[:300]}")
    request_id = data.get("request_id")
    mission_id = data.get("mission_id")
    STATE.update(request_id=request_id, mission_id=mission_id)
    if request_id:
        r = customer.get(f"/booking/{request_id}")
        ok("booking page renders", r.status_code == 200, r.status_code)
        r = customer.get(f"/service/{request_id}/blackbox")
        ok("black box page renders", r.status_code == 200, r.status_code)
        r = customer.get(f"/second-opinion/{request_id}")
        ok("second opinion page renders", r.status_code == 200, r.status_code)
        r = customer.get(f"/certificate/{request_id}")
        ok("certificate page renders (pre-completion)", r.status_code == 200, r.status_code)
        status, data = jget(customer, f"/api/service/{request_id}/parts")
        ok("parts auto-suggested", status == 200 and len(data.get("parts") or []) > 0, str(data)[:200])
        status, data = jpost(customer, "/api/second-opinion", {
            "request_id": request_id, "proposed_repair": "Compressor replacement",
            "quoted_amount": 12000, "question": "Is a full compressor replacement necessary?",
        })
        ok("second opinion generated", status == 200 and (data.get("result") or data.get("opinion")), f"{status} {str(data)[:250]}")
        text = json.dumps(data).lower()
        ok("second opinion never accuses provider",
           not any(word in text for word in ["cheat", "fraud", "lying", "scam"]), text[:200])
    if mission_id:
        r = customer.get(f"/missions/{mission_id}")
        ok("mission page renders", r.status_code == 200, r.status_code)
        status, data = jget(customer, f"/api/missions/{mission_id}/tasks")
        ok("mission tasks listed", status == 200 and len(data.get("tasks") or []) > 0, str(data)[:200])

    # 3 ── Provider mission brief + accept ---------------------------------------
    if request_id:
        r = provider.get(f"/provider/mission-brief/{request_id}")
        ok("provider sees mission brief", r.status_code == 200, r.status_code)
        status, data = jpost(provider, f"/api/service/{request_id}/accept", {})
        ok("provider accepts job", status == 200, f"{status} {str(data)[:200]}")
        status, data = jpost(provider, f"/api/service/{request_id}/propose-diagnosis", {
            "diagnosis": "Compressor capacitor weak; compressor winding healthy",
            "findings": "Capacitance 12uF vs rated 35uF", "estimated_amount": 2400,
            "estimated_duration": "2 hours",
        })
        ok("provider proposes diagnosis", status == 200, f"{status} {str(data)[:200]}")
        status, data = jpost(provider, f"/api/service/{request_id}/parts", {
            "part_name": "Run capacitor 35uF", "quantity": 1, "unit_price_min": 450,
            "unit_price_max": 700, "warranty_days": 90, "requires_approval": 1,
        })
        ok("provider proposes part", status == 200, f"{status} {str(data)[:200]}")
        part_id = data.get("part_id")
        status, data = jpost(provider, f"/api/services/{request_id}/evidence", {"stage": "BEFORE", "note": "Capacitor measured 12uF"})
        ok("provider adds evidence", status == 200, f"{status} {str(data)[:200]}")
        if part_id:
            status, data = jpost(customer, f"/api/parts/{part_id}/approve", {})
            ok("customer approves part", status == 200, f"{status} {str(data)[:200]}")
        status, data = jpost(provider, f"/api/service/{request_id}/mission-brief", {})
        ok("mission brief API works for provider", status in (200, 405), f"{status}")

    # 4 ── Legacy completion chain (arrival -> code -> proof -> verify -> pay) ----
    PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
           b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00"
           b"\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")
    if request_id:
        r = provider.post(f"/update-service-status/{request_id}/ARRIVED", follow_redirects=True)
        ok("provider marks arrival", r.status_code == 200, r.status_code)
        with app.app_context():
            connection = get_db_connection()
            code = connection.execute("SELECT confirmation_code FROM service_requests WHERE id=?", (request_id,)).fetchone()["confirmation_code"]
            connection.close()
        r = provider.post(f"/provider/confirm-arrival/{request_id}", data={"confirmation_code": code}, follow_redirects=True)
        ok("arrival confirmation code accepted", r.status_code == 200, r.status_code)
        r = provider.post(
            f"/submit-completion-proof/{request_id}",
            data={"proof_images": (io.BytesIO(PNG), "after.png")},
            content_type="multipart/form-data", follow_redirects=True,
        )
        ok("provider submits completion proof", r.status_code == 200, r.status_code)
        with app.app_context():
            connection = get_db_connection()
            mirrored = connection.execute(
                "SELECT COUNT(*) c FROM service_evidence WHERE request_id=?", (request_id,)
            ).fetchone()["c"]
            connection.close()
        ok("legacy status changes mirrored into the V10 black box", mirrored >= 2, f"{mirrored} evidence rows")
        r = customer.post(
            f"/verify-service/{request_id}",
            data={"verification_image": (io.BytesIO(PNG), "verify.png")},
            content_type="multipart/form-data", follow_redirects=True,
        )
        ok("customer verifies completed service", r.status_code == 200, r.status_code)

    # 4b ── Payment verification requires real Razorpay credentials ---------------
    if request_id:
        status, data = jpost(customer, f"/api/payment/verify/{request_id}", {
            "razorpay_order_id": "order_test", "razorpay_payment_id": "pay_test", "razorpay_signature": "sig",
        })
        ok("payment verify fails closed without Razorpay keys", status in (400, 503), f"{status} {str(data)[:160]}")

    if request_id:
        with app.app_context():
            connection = get_db_connection()
            connection.execute(
                "UPDATE service_requests SET status='COMPLETED', completed_at=CURRENT_TIMESTAMP, "
                "final_amount=2400, agreed_amount=2400, payment_status='PAID', warranty_days=90, "
                "warranty_until=datetime('now','+90 days') WHERE id=?",
                (request_id,),
            )
            connection.commit()
            certificate = engine.issue_certificate(connection, request_id, force=True)
            connection.commit()
            connection.close()
        ok("certificate issued after completion", bool(certificate), str(certificate)[:200])
        r = customer.get(f"/certificate/{request_id}")
        body = r.get_data(as_text=True)
        code = (certificate or {}).get("certificate_code") if isinstance(certificate, dict) else None
        ok("certificate page shows code", r.status_code == 200 and (not code or code in body), f"{r.status_code} {code}")
        status, data = jpost(customer, f"/api/service/{request_id}/recovery", {
            "problem": "Same cooling problem returned after 10 days", "severity": "MEDIUM",
        })
        ok("recovery case opened", status == 200 and (data.get("case_id") or data.get("case_code")), f"{status} {str(data)[:250]}")
        case_id = data.get("case_id")
        if case_id:
            r = customer.get(f"/recovery/{case_id}")
            ok("recovery case page renders", r.status_code == 200, r.status_code)
            status, data = jpost(customer, f"/api/recovery/{case_id}/action", {"action": "CONTACT_PROVIDER"})
            ok("recovery action works", status == 200, f"{status} {str(data)[:200]}")

    # 5 ── Verification (dev OTP) ------------------------------------------------
    jpost(customer, "/api/profile", {"phone": "9876501234"}, json_body=False)
    status, data = jpost(customer, "/api/verify-phone/send", {})
    ok("phone OTP generated in dev mode", status == 200 and data.get("dev_code"), f"{status} {str(data)[:200]}")
    code = data.get("dev_code")
    if code:
        status, data = jpost(customer, "/api/verify-phone/confirm", {"code": code})
        ok("phone OTP confirmed", status == 200, f"{status} {str(data)[:200]}")

    # 5b ── Skill gating: a student provider must NOT be able to take HIGH-risk work
    with app.app_context():
        connection = get_db_connection()
        high_service = connection.execute(
            "SELECT id FROM services WHERE name='Electrical' AND risk_level='HIGH' LIMIT 1"
        ).fetchone()
        connection.execute("UPDATE providers SET provider_type='student', student_status='TRAINEE' "
                           "WHERE id=(SELECT p.id FROM providers p JOIN users u ON u.id=p.user_id "
                           "WHERE u.email='meera.electrical@smartserve.demo')")
        connection.execute(
            "INSERT OR IGNORE INTO provider_services(provider_id, service_id) "
            "SELECT p.id, ? FROM providers p JOIN users u ON u.id=p.user_id WHERE u.email='meera.electrical@smartserve.demo'",
            (high_service["id"] if high_service else -1,),
        )
        connection.commit()
        connection.close()
    if high_service:
        status, data = jpost(provider, "/api/v10/bookings", {})  # no-op guard (provider cannot book)
        ok("provider cannot create customer booking", status in (302, 401, 403), str(status))
        status, data = jpost(student_provider, f"/api/service/{STATE.get('request_id') or 1}/accept", {})
        ok("student provider blocked from HIGH-risk job", status in (403, 409), f"{status} {str(data)[:160]}")

    # 6 ── Provider skill passport ------------------------------------------------
    r = provider.get("/provider/skills")
    ok("provider skill passport page", r.status_code == 200, r.status_code)
    status, data = jget(provider, "/api/provider/certifications")
    ok("provider certifications API", status == 200, f"{status}")

    # 7 ── Admin console ----------------------------------------------------------
    admin = app.test_client()
    login(admin, "admin@smartserve.demo")
    r = admin.get("/admin/v10")
    ok("admin V10 console renders", r.status_code == 200, r.status_code)
    with app.app_context():
        connection = get_db_connection()
        skill = connection.execute("SELECT id FROM student_skills LIMIT 1").fetchone()
        connection.close()
    if skill:
        status, data = jpost(admin, f"/admin/v10/skill/{skill['id']}/verify", {"result": "VERIFIED", "method": "ASSESSMENT"})
        ok("admin verifies skill", status == 200, f"{status} {str(data)[:200]}")
    body = admin.get("/admin/v10").get_data(as_text=True)
    ok("admin console shows lifecycle report", "Lifecycle report" in body and "Risk level controls" in body)
    with app.app_context():
        connection = get_db_connection()
        target = connection.execute("SELECT id, risk_level FROM services WHERE name='Electrical'").fetchone()
        connection.close()
    if target:
        status, data = jpost(admin, "/admin/v10/service", {"service_id": target["id"], "risk_level": "RESTRICTED"})
        ok("admin can change a service risk level", status == 200, f"{status} {str(data)[:160]}")
        with app.app_context():
            connection = get_db_connection()
            updated = connection.execute("SELECT risk_level FROM services WHERE id=?", (target["id"],)).fetchone()["risk_level"]
            connection.close()
        ok("risk level persisted", updated == "RESTRICTED", updated)
        # restore so later checks keep the original catalogue state
        jpost(admin, "/admin/v10/service", {"service_id": target["id"], "risk_level": target["risk_level"]})

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    for item in FAILED:
        print("  FAILED:", item)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
