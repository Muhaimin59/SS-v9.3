"""SmartServe V10 — Service Lifecycle Engine.

The engine is the single place where the seven V10 modules talk to each other.
Nothing here replaces V9.3 behaviour: every function only *adds* rows to the new
V10 tables (or fills new columns on existing rows).

Lifecycle: UNDERSTAND → IDENTIFY → DIAGNOSE → PLAN → MATCH → COORDINATE →
SERVICE → VERIFY → RECORD → MONITOR → RECOVER
"""

from __future__ import annotations

import json
import secrets
import string
from datetime import datetime, timedelta

from . import ai as ai_layer
from . import catalog

LIFECYCLE_STAGES = [
    "UNDERSTAND", "IDENTIFY", "DIAGNOSE", "PLAN", "MATCH",
    "COORDINATE", "SERVICE", "VERIFY", "RECORD", "MONITOR", "RECOVER",
]

LIFECYCLE_LABELS = {
    "UNDERSTAND": "Problem understood",
    "IDENTIFY": "Asset identified",
    "DIAGNOSE": "Diagnosis",
    "PLAN": "Mission planned",
    "MATCH": "Provider matching",
    "COORDINATE": "Coordination",
    "SERVICE": "Service in progress",
    "VERIFY": "Verification",
    "RECORD": "Recorded in passport",
    "MONITOR": "Outcome monitoring",
    "RECOVER": "Recovery",
}

MISSION_TASK_STATUSES = ("PENDING", "READY", "ASSIGNED", "IN_PROGRESS", "BLOCKED", "COMPLETED", "FAILED", "CANCELLED")

ACK_REQUIRED_STATUSES = ("ASSIGNED", "ACCEPTED", "ARRIVED", "IN_PROGRESS", "AWAITING_VERIFICATION")

DEMO_TAG = "DEMO"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def now():
    return datetime.utcnow()


def now_str():
    return now().strftime("%Y-%m-%d %H:%M:%S")


def dumps(value):
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return json.dumps(str(value))


def loads(value, default=None):
    if value is None or value == "":
        return default if default is not None else {}
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default if default is not None else {}


def loads_list(value):
    result = loads(value, [])
    if isinstance(result, dict):
        return [result]
    return result if isinstance(result, list) else []


def _code(prefix, length=6, alphabet=string.ascii_uppercase + string.digits):
    return f"{prefix}{''.join(secrets.choice(alphabet) for _ in range(length))}"


def asset_uid():
    return "AST-" + _code("", 8)


def mission_code():
    return "SM-" + _code("", 6)


def recovery_code():
    return "RC-" + _code("", 6)


def certificate_code():
    return f"SS-{now().year}-{secrets.randbelow(90000) + 10000}"


def mask_phone(phone):
    if not phone:
        return None
    digits = "".join(ch for ch in str(phone) if ch.isdigit())
    if len(digits) >= 4:
        return "•" * (len(digits) - 4) + digits[-4:]
    return "Available"


# ---------------------------------------------------------------------------
# MODULE 1 — SERVICE PASSPORT
# ---------------------------------------------------------------------------

def get_asset(connection, asset_id):
    return connection.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()


def get_asset_by_uid(connection, uid):
    return connection.execute("SELECT * FROM assets WHERE asset_uid=?", (uid,)).fetchone()


def list_assets(connection, customer_id, include_archived=False):
    clause = "" if include_archived else "AND COALESCE(status,'ACTIVE')<>'ARCHIVED'"
    return connection.execute(
        f"SELECT * FROM assets WHERE customer_id=? {clause} ORDER BY created_at DESC", (customer_id,)
    ).fetchall()


def create_asset(connection, customer_id, payload, request_id=None, actor_user_id=None):
    """Create an asset + QR token + preventive-maintenance plan."""
    asset_type = (payload.get("asset_type") or "Other asset").strip()
    category = payload.get("category")
    if not category:
        match = next((t for t in catalog.ASSET_TYPES if t[0].lower() == asset_type.lower()), None)
        category = match[1] if match else "Home Repair & Maintenance"
    uid = asset_uid()
    connection.execute(
        """
        INSERT INTO assets(
            asset_uid, customer_id, asset_type, category, subcategory, nickname, brand, model,
            serial_number, purchase_date, installation_date, warranty_start, warranty_end,
            warranty_provider, vendor_name, health_score, health_note, status, location_label,
            photo_path, notes, data_source
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            uid, customer_id, asset_type, category, payload.get("subcategory"), payload.get("nickname"),
            payload.get("brand"), payload.get("model"), payload.get("serial_number"),
            payload.get("purchase_date"), payload.get("installation_date"), payload.get("warranty_start"),
            payload.get("warranty_end"), payload.get("warranty_provider"), payload.get("vendor_name"),
            int(payload.get("health_score") or 80), payload.get("health_note"), "ACTIVE",
            payload.get("location_label"), payload.get("photo_path"), payload.get("notes"),
            payload.get("data_source") or "USER",
        ),
    )
    asset_id = connection.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    issue_qr_token(connection, asset_id)
    ensure_maintenance_plan(connection, asset_id)
    log_evidence(
        connection, request_id=request_id, asset_id=asset_id, stage="IDENTIFY", kind="ASSET",
        note=f"Service Passport created for {asset_type} ({uid}).",
        actor_user_id=actor_user_id, actor_role="customer",
    )
    return asset_id


def issue_qr_token(connection, asset_id, revoke_existing=False):
    if revoke_existing:
        connection.execute(
            "UPDATE asset_qr_tokens SET status='REVOKED', revoked_at=? WHERE asset_id=? AND status='ACTIVE'",
            (now_str(), asset_id),
        )
    existing = connection.execute(
        "SELECT * FROM asset_qr_tokens WHERE asset_id=? AND status='ACTIVE' ORDER BY id DESC LIMIT 1",
        (asset_id,),
    ).fetchone()
    if existing and not revoke_existing:
        return dict(existing)
    token = secrets.token_urlsafe(24)
    connection.execute("INSERT INTO asset_qr_tokens(asset_id, token) VALUES(?,?)", (asset_id, token))
    row = connection.execute("SELECT * FROM asset_qr_tokens WHERE id=last_insert_rowid()").fetchone()
    return dict(row)


def active_qr_token(connection, asset_id):
    row = connection.execute(
        "SELECT * FROM asset_qr_tokens WHERE asset_id=? AND status='ACTIVE' ORDER BY id DESC LIMIT 1",
        (asset_id,),
    ).fetchone()
    if row:
        return dict(row)
    return issue_qr_token(connection, asset_id)


def resolve_public_token(connection, token):
    """QR scan → asset (public passport lookup, no private data)."""
    if not token:
        return None
    row = connection.execute(
        """
        SELECT a.*, t.id AS token_id, t.token AS token_value
        FROM asset_qr_tokens t JOIN assets a ON a.id=t.asset_id
        WHERE t.token=? AND t.status='ACTIVE'
        """,
        (token,),
    ).fetchone()
    if not row:
        return None
    connection.execute(
        "UPDATE asset_qr_tokens SET scan_count=COALESCE(scan_count,0)+1, last_scanned_at=? WHERE id=?",
        (now_str(), row["token_id"]),
    )
    return row


def warranty_state(asset):
    """Return ACTIVE / EXPIRING / EXPIRED / UNKNOWN for an asset warranty."""
    if not asset:
        return {"status": "UNKNOWN", "until": None, "days_remaining": None}
    until_raw = asset["warranty_end"] if "warranty_end" in asset.keys() else None
    if not until_raw:
        return {"status": "UNKNOWN", "until": None, "days_remaining": None}
    until = _parse_date(until_raw)
    if not until:
        return {"status": "UNKNOWN", "until": until_raw, "days_remaining": None}
    days = (until - now()).days
    if days < 0:
        return {"status": "EXPIRED", "until": until_raw, "days_remaining": 0}
    if days <= 30:
        return {"status": "EXPIRING", "until": until_raw, "days_remaining": days}
    return {"status": "ACTIVE", "until": until_raw, "days_remaining": days}


def _parse_date(value):
    if not value:
        return None
    text = str(value).strip().replace("Z", "")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def asset_history(connection, asset_id, limit=60):
    rows = connection.execute(
        """
        SELECT h.*, u.name AS provider_name,
               (SELECT COALESCE(AVG(r.rating),0) FROM reviews r WHERE r.provider_id=h.provider_id) AS provider_rating
        FROM asset_service_history h
        LEFT JOIN providers p ON p.id=h.provider_id
        LEFT JOIN users u ON u.id=p.user_id
        WHERE h.asset_id=?
        ORDER BY datetime(h.service_date) DESC, h.id DESC
        LIMIT ?
        """,
        (asset_id, limit),
    ).fetchall()
    return rows


def asset_certificates(connection, asset_id):
    return connection.execute(
        "SELECT * FROM service_certificates WHERE asset_id=? ORDER BY datetime(issued_at) DESC", (asset_id,)
    ).fetchall()


def asset_parts(connection, asset_id):
    """Parts history for the passport (from black-box records)."""
    rows = connection.execute(
        "SELECT * FROM service_parts WHERE asset_id=? ORDER BY datetime(created_at) DESC LIMIT 40", (asset_id,)
    ).fetchall()
    history_parts = []
    for row in asset_history(connection, asset_id):
        for part in loads_list(row["parts_json"]):
            if isinstance(part, dict):
                history_parts.append(part)
            elif part:
                history_parts.append({"part_name": str(part), "data_source": row["data_source"]})
    return {"recorded": rows, "history": history_parts}


def maintenance_plan(connection, asset_id):
    return connection.execute(
        "SELECT * FROM asset_maintenance_schedule WHERE asset_id=? ORDER BY CASE WHEN status='UPCOMING' THEN 0 ELSE 1 END, due_date",
        (asset_id,),
    ).fetchall()


def ensure_maintenance_plan(connection, asset_id):
    asset = get_asset(connection, asset_id)
    if not asset:
        return
    existing = connection.execute(
        "SELECT COUNT(*) c FROM asset_maintenance_schedule WHERE asset_id=? AND status='UPCOMING'", (asset_id,)
    ).fetchone()["c"]
    if existing:
        return
    plan = ai_layer.maintenance_suggestions(asset["asset_type"], asset["warranty_end"])
    due = (now() + timedelta(days=plan["interval_days"])).strftime("%Y-%m-%d")
    connection.execute(
        """
        INSERT INTO asset_maintenance_schedule(asset_id,title,description,due_date,interval_days,source)
        VALUES(?,?,?,?,?,?)
        """,
        (asset_id, plan["title"], plan["description"], due, plan["interval_days"], "SYSTEM"),
    )


def passport(connection, asset, include_public_only=False):
    """Full Service Passport payload."""
    asset_id = int(asset["id"])
    history = asset_history(connection, asset_id)
    certificates = asset_certificates(connection, asset_id)
    parts = asset_parts(connection, asset_id)
    maintenance = maintenance_plan(connection, asset_id)
    qr = active_qr_token(connection, asset_id)
    warranty = warranty_state(asset)

    timeline = []
    for row in history:
        timeline.append({
            "kind": "service",
            "date": row["service_date"],
            "title": row["service_name"] or "Service",
            "problem": row["problem"],
            "diagnosis": row["diagnosis"],
            "work": row["work_performed"],
            "amount": row["amount"],
            "provider_id": row["provider_id"],
            "provider_name": row["provider_name"] if "provider_name" in row.keys() else None,
            "request_id": row["request_id"],
            "certificate_id": row["certificate_id"],
            "outcome_status": row["outcome_status"],
            "data_source": row["data_source"],
        })
    for row in connection.execute(
        "SELECT * FROM service_evidence WHERE asset_id=? AND kind IN ('NOTE','DIAGNOSIS') ORDER BY created_at DESC LIMIT 20",
        (asset_id,),
    ).fetchall():
        timeline.append({
            "kind": "note",
            "date": row["created_at"],
            "title": row["stage"].replace("_", " ").title(),
            "problem": None,
            "diagnosis": row["note"],
            "work": None,
            "amount": row["amount"],
            "provider_id": None,
            "request_id": row["request_id"],
            "certificate_id": None,
            "outcome_status": None,
            "data_source": row["data_source"],
        })
    timeline.sort(key=lambda item: str(item["date"] or ""), reverse=True)

    payload = {
        "asset": dict(asset),
        "warranty": warranty,
        "qr_token": qr["token"] if qr else None,
        "qr_status": qr["status"] if qr else "NONE",
        "health_score": int(asset["health_score"] or 0),
        "history": [dict(row) for row in history],
        "certificates": [dict(row) for row in certificates],
        "parts": {
            "recorded": [dict(row) for row in parts["recorded"]],
            "history": parts["history"],
        },
        "maintenance": [dict(row) for row in maintenance],
        "timeline": timeline,
        "lifecycle": lifecycle_state_for_asset(connection, asset_id),
        "data_source": asset["data_source"] or "USER",
    }

    if include_public_only:
        owner = connection.execute(
            "SELECT name FROM users WHERE id=?", (asset["customer_id"],)
        ).fetchone()
        payload["asset"] = {
            key: asset[key]
            for key in (
                "asset_uid", "asset_type", "category", "nickname", "brand", "model",
                "purchase_date", "installation_date", "warranty_end", "health_score",
                "status", "location_label", "data_source",
            )
            if key in asset.keys()
        }
        payload["owner_display"] = (owner["name"].split(" ")[0] + " •") if owner else "SmartServe customer"
        payload["history"] = [
            {key: row[key] for key in ("service_name", "problem", "work_performed", "service_date", "amount", "warranty_until", "evidence_verified")}
            for row in history
        ]
        payload["certificates"] = [
            {key: row[key] for key in ("certificate_code", "service_name", "final_amount", "issued_at", "warranty_days")}
            for row in certificates
        ]
        payload["maintenance"] = [
            {key: row[key] for key in ("title", "description", "due_date", "status")} for row in maintenance
        ]
    return payload


def lifecycle_state_for_asset(connection, asset_id):
    row = connection.execute(
        """
        SELECT sr.id, sr.status, sr.lifecycle_stage, s.name AS service_name, sr.created_at
        FROM service_requests sr JOIN services s ON s.id=sr.service_id
        WHERE sr.asset_id=? ORDER BY sr.id DESC LIMIT 1
        """,
        (asset_id,),
    ).fetchone()
    if not row:
        return {"stage": "IDENTIFY", "label": LIFECYCLE_LABELS["IDENTIFY"], "request_id": None}
    stage = (row["lifecycle_stage"] or "").upper()
    if row["status"] in ("AWAITING_PAYMENT", "AWAITING_VERIFICATION"):
        stage = "VERIFY"
    elif row["status"] in ("ACCEPTED", "ARRIVED", "ASSIGNED"):
        stage = stage or "COORDINATE"
    elif row["status"] == "IN_PROGRESS":
        stage = "SERVICE"
    elif row["status"] == "COMPLETED":
        stage = stage or "MONITOR"
    if stage not in LIFECYCLE_LABELS:
        stage = "MATCH"
    return {
        "stage": stage,
        "label": LIFECYCLE_LABELS.get(stage, stage.title()),
        "request_id": row["id"],
        "service_name": row["service_name"],
    }


def recompute_health(connection, asset_id):
    """Health = base (85) − severity of recent repeat problems + verified fixes."""
    asset = get_asset(connection, asset_id)
    if not asset:
        return 0
    history = asset_history(connection, asset_id, limit=20)
    score = 88
    recurring = 0
    previous_problems = []
    for row in history:
        problem = (row["problem"] or "").lower()
        if any(word in problem for word in ("fail", "not working", "no cooling", "leak", "spark", "dead", "broken")):
            score -= 7
        else:
            score -= 3
        if any(word in problem for word in ("again", "returned", "recurring", "still")):
            recurring += 1
        for previous in previous_problems:
            if previous and previous[:18] in problem:
                recurring += 1
                break
        previous_problems.append(problem)
    score -= min(18, recurring * 6)
    active_warranty = warranty_state(asset)["status"] in ("ACTIVE", "EXPIRING")
    if active_warranty:
        score += 4
    if any((row["evidence_verified"] or 0) for row in history):
        score += 4
    maintenance_due = connection.execute(
        "SELECT COUNT(*) c FROM asset_maintenance_schedule WHERE asset_id=? AND status='UPCOMING' AND due_date < date('now')",
        (asset_id,),
    ).fetchone()["c"]
    if maintenance_due:
        score -= min(10, maintenance_due * 5)
    score = max(20, min(100, int(score)))
    connection.execute(
        "UPDATE assets SET health_score=?, updated_at=? WHERE id=?", (score, now_str(), asset_id)
    )
    return score


# ---------------------------------------------------------------------------
# MODULE 2 — MISSION ENGINE
# ---------------------------------------------------------------------------

def create_mission(
    connection,
    customer_id,
    problem,
    service_name=None,
    service_id=None,
    asset_id=None,
    description=None,
    media=None,
    voice_note_path=None,
    address_text=None,
    pincode=None,
    latitude=None,
    longitude=None,
    preferred_time=None,
    scheduled_at=None,
    budget=None,
    created_by_user_id=None,
    force_offline=False,
):
    """UNDERSTAND → IDENTIFY → DIAGNOSE → PLAN in one call."""
    asset = get_asset(connection, asset_id) if asset_id else None
    asset_dict = dict(asset) if asset else {}
    history = [dict(row) for row in asset_history(connection, asset_id, limit=6)] if asset_id else []

    if force_offline:
        analysis = ai_layer.offline_analysis(service_name, problem, asset=asset_dict)
    else:
        analysis = ai_layer.analyze_problem(
            service_name, problem, asset=asset_dict, history=history,
            image_path=media[0] if media else None,
        )

    plan = ai_layer.decompose_mission(problem, service_name=service_name, asset=asset_dict, analysis=analysis)
    code = mission_code()

    risk = plan.get("risk_level") or analysis.get("risk_level") or catalog.RISK_MEDIUM
    safety_notes = analysis.get("safety_notes") or []

    connection.execute(
        """
        INSERT INTO service_missions(
            mission_code, customer_id, asset_id, title, problem, description, media_json,
            voice_note_path, ai_analysis_json, ai_confidence, required_categories, risk_level,
            safety_notes, priority, status, lifecycle_stage, estimated_total, actual_total,
            currency, address_text, pincode, latitude, longitude, preferred_time, scheduled_at,
            budget_customer, data_source
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            code, customer_id, asset_id, plan["title"][:200], problem[:2000], description,
            dumps(media or []), voice_note_path, dumps(analysis), analysis.get("confidence"),
            dumps(plan.get("required_services") or []), risk, dumps(safety_notes),
            "HIGH" if risk in (catalog.RISK_HIGH, catalog.RISK_RESTRICTED) else "NORMAL",
            "PLANNED", "PLAN",
            analysis.get("estimated_price_range", {}).get("max"), 0, "INR",
            address_text, pincode, latitude, longitude, preferred_time, scheduled_at, budget,
            analysis.get("data_source", "AI"),
        ),
    )
    mission_id = connection.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    order_to_task_id = {}
    for task in plan["tasks"]:
        service_row = _service_by_name(connection, task.get("service_name") or service_name)
        depends_on_order = int(task.get("depends_on_order") or 0)
        depends_on_task_id = order_to_task_id.get(depends_on_order) if depends_on_order else None
        status = "READY" if not depends_on_task_id else "PENDING"
        connection.execute(
            """
            INSERT INTO mission_tasks(
                mission_id, task_order, title, description, service_id, service_name,
                required_skill, risk_level, depends_on_task_id, status, ai_confidence,
                estimated_cost, provider_brief, data_source
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                mission_id, task["task_order"], task["title"][:160], task.get("description"),
                service_row["id"] if service_row else service_id,
                task.get("service_name") or service_name,
                (task.get("service_name") or service_name), risk, depends_on_task_id, status,
                analysis.get("confidence"), task.get("estimated_cost"),
                dumps({
                    "ai_causes": analysis.get("possible_causes"),
                    "safety": safety_notes,
                    "expected": task.get("description"),
                }),
                plan.get("data_source", "AI"),
            ),
        )
        order_to_task_id[task["task_order"]] = connection.execute(
            "SELECT last_insert_rowid() AS id"
        ).fetchone()["id"]

    log_evidence(
        connection, mission_id=mission_id, asset_id=asset_id, stage="UNDERSTAND", kind="TEXT",
        note=f"Customer problem recorded: {problem[:400]}",
        actor_user_id=created_by_user_id, actor_role="customer",
    )
    if media:
        for path in media:
            log_evidence(
                connection, mission_id=mission_id, asset_id=asset_id, stage="UNDERSTAND", kind="PHOTO",
                path=path, note="Customer uploaded problem evidence.",
                actor_user_id=created_by_user_id, actor_role="customer",
            )
    log_evidence(
        connection, mission_id=mission_id, asset_id=asset_id, stage="DIAGNOSE", kind="AI",
        note=dumps({
            "source": analysis.get("data_source"),
            "possible_causes": analysis.get("possible_causes"),
            "confidence": analysis.get("confidence"),
            "recommended_next_step": analysis.get("recommended_next_step"),
        }),
        actor_user_id=None, actor_role="system", data_source=analysis.get("data_source", "AI"),
    )
    log_evidence(
        connection, mission_id=mission_id, asset_id=asset_id, stage="PLAN", kind="PLAN",
        note=f"Mission {code} planned with {len(plan['tasks'])} tasks.",
        actor_role="system",
    )
    if asset_id:
        connection.execute(
            "UPDATE assets SET updated_at=? WHERE id=?", (now_str(), asset_id)
        )
    return mission_id, code, analysis, plan


def _service_by_name(connection, name):
    if not name:
        return None
    return connection.execute(
        "SELECT id, name FROM services WHERE LOWER(name)=LOWER(?) LIMIT 1", (str(name).strip(),)
    ).fetchone()


def get_mission(connection, mission_id):
    return connection.execute("SELECT * FROM service_missions WHERE id=?", (mission_id,)).fetchone()


def missions_for_customer(connection, customer_id, status=None, limit=100):
    clause = ""
    params = [customer_id]
    if status == "ACTIVE":
        clause = "AND m.status NOT IN ('COMPLETED','CANCELLED')"
    elif status == "COMPLETED":
        clause = "AND m.status='COMPLETED'"
    params.append(limit)
    return connection.execute(
        f"""
        SELECT m.*, a.asset_type, a.brand, a.model, a.asset_uid,
               (SELECT COUNT(*) FROM mission_tasks t WHERE t.mission_id=m.id) AS task_count,
               (SELECT COUNT(*) FROM mission_tasks t WHERE t.mission_id=m.id AND t.status='COMPLETED') AS tasks_done
        FROM service_missions m
        LEFT JOIN assets a ON a.id=m.asset_id
        WHERE m.customer_id=? {clause}
        ORDER BY m.id DESC LIMIT ?
        """,
        params,
    ).fetchall()


def mission_tasks(connection, mission_id):
    return connection.execute(
        """
        SELECT t.*, s.icon AS service_icon, u.name AS provider_name,
               sr.status AS request_status, sr.payment_status, sr.agreed_amount,
               sr.customer_verification_path, sr.completion_proof_paths
        FROM mission_tasks t
        LEFT JOIN services s ON s.id=t.service_id
        LEFT JOIN providers p ON p.id=t.provider_id
        LEFT JOIN users u ON u.id=p.user_id
        LEFT JOIN service_requests sr ON sr.id=t.request_id
        WHERE t.mission_id=? ORDER BY t.task_order
        """,
        (mission_id,),
    ).fetchall()


def mission_progress(connection, mission_id):
    row = connection.execute(
        """
        SELECT COUNT(*) total, SUM(CASE WHEN status='COMPLETED' THEN 1 ELSE 0 END) done
        FROM mission_tasks WHERE mission_id=?
        """,
        (mission_id,),
    ).fetchone()
    total = int(row["total"] or 0)
    done = int(row["done"] or 0)
    percent = int(round((done / total) * 100)) if total else 0
    return {"total": total, "completed": done, "percent": percent}


def refresh_mission_state(connection, mission_id, actor_user_id=None):
    """Recompute task readiness, mission progress, costs and lifecycle stage."""
    mission = get_mission(connection, mission_id)
    if not mission:
        return None
    tasks = connection.execute(
        "SELECT * FROM mission_tasks WHERE mission_id=? ORDER BY task_order", (mission_id,)
    ).fetchall()
    by_order = {int(t["task_order"]): t for t in tasks}
    for task in tasks:
        if task["status"] in ("COMPLETED", "CANCELLED", "FAILED"):
            continue
        depends = task["depends_on_task_id"]
        if depends:
            parent = connection.execute(
                "SELECT status FROM mission_tasks WHERE id=?", (depends,)
            ).fetchone()
            if parent and parent["status"] != "COMPLETED":
                connection.execute(
                    "UPDATE mission_tasks SET status='BLOCKED' WHERE id=? AND status IN ('READY','PENDING')",
                    (task["id"],),
                )
                continue
        if task["status"] in ("PENDING", "BLOCKED"):
            connection.execute("UPDATE mission_tasks SET status='READY' WHERE id=?", (task["id"],))

    rows = connection.execute(
        "SELECT status, actual_cost, estimated_cost FROM mission_tasks WHERE mission_id=?", (mission_id,)
    ).fetchall()
    statuses = [row["status"] for row in rows]
    actual = sum(float(row["actual_cost"] or 0) for row in rows)
    estimated = sum(float(row["estimated_cost"] or 0) for row in rows)

    if statuses and all(status == "COMPLETED" for status in statuses):
        mission_status = "COMPLETED"
        stage = "MONITOR"
    elif any(status in ("IN_PROGRESS", "ASSIGNED") for status in statuses):
        mission_status = "IN_PROGRESS"
        stage = "SERVICE"
    elif any(status in ("COMPLETED",) for status in statuses):
        mission_status = "IN_PROGRESS"
        stage = "COORDINATE"
    else:
        mission_status = mission["status"] if mission["status"] not in ("COMPLETED",) else "IN_PROGRESS"
        if mission_status not in ("PLANNED", "IN_PROGRESS", "ON_HOLD", "CANCELLED"):
            mission_status = "PLANNED"
        stage = mission["lifecycle_stage"] if mission["lifecycle_stage"] in LIFECYCLE_LABELS else "PLAN"

    connection.execute(
        """
        UPDATE service_missions SET status=?, lifecycle_stage=?, actual_total=?, estimated_total=?,
            updated_at=?, completed_at=CASE WHEN ?='COMPLETED' THEN COALESCE(completed_at, ?) ELSE completed_at END
        WHERE id=?
        """,
        (mission_status, stage, actual, estimated or mission["estimated_total"], now_str(),
         mission_status, now_str(), mission_id),
    )
    # Keep the linked booking's lifecycle stage in sync for the timeline UI.
    for task in tasks:
        if task["request_id"]:
            connection.execute(
                "UPDATE service_requests SET lifecycle_stage=? WHERE id=? AND lifecycle_stage IS NOT ?",
                (stage, task["request_id"], stage),
            )
    return {"status": mission_status, "stage": stage, "progress": mission_progress(connection, mission_id)}


def attach_request_to_task(connection, task_id, request_id):
    connection.execute(
        "UPDATE mission_tasks SET request_id=?, status=CASE WHEN status='READY' THEN 'ASSIGNED' ELSE status END WHERE id=?",
        (request_id, task_id),
    )
    connection.execute(
        "UPDATE service_requests SET mission_id=(SELECT mission_id FROM mission_tasks WHERE id=?), "
        "mission_task_id=? WHERE id=?",
        (task_id, task_id, request_id),
    )
    log_evidence(
        connection, request_id=request_id, task_id=task_id,
        mission_id=connection.execute("SELECT mission_id FROM mission_tasks WHERE id=?", (task_id,)).fetchone()["mission_id"],
        stage="MATCH", kind="TASK", note="Service request linked to mission task.",
        actor_role="customer",
    )


def set_task_provider(connection, task_id, provider_id):
    connection.execute("UPDATE mission_tasks SET provider_id=? WHERE id=?", (provider_id, task_id))


def sync_task_from_request(connection, request_id):
    """Mirror the booking status into its mission task (and refresh the mission)."""
    row = connection.execute(
        "SELECT id, mission_id, mission_task_id, status, provider_id, agreed_amount FROM service_requests WHERE id=?",
        (request_id,),
    ).fetchone()
    if not row or not row["mission_task_id"]:
        return None
    mapping = {
        "ACCEPTED": "ASSIGNED",
        "ARRIVED": "ASSIGNED",
        "IN_PROGRESS": "IN_PROGRESS",
        "AWAITING_VERIFICATION": "IN_PROGRESS",
        "AWAITING_PAYMENT": "IN_PROGRESS",
        "COMPLETED": "COMPLETED",
        "CANCELLED": "CANCELLED",
        "REJECTED": "CANCELLED",
        "EXPIRED": "CANCELLED",
    }
    desired = mapping.get((row["status"] or "").upper())
    if not desired:
        return None
    connection.execute(
        """
        UPDATE mission_tasks SET status=?, provider_id=COALESCE(?, provider_id),
            actual_cost=CASE WHEN ?='COMPLETED' THEN COALESCE(?, actual_cost) ELSE actual_cost END,
            started_at=CASE WHEN ?='IN_PROGRESS' THEN COALESCE(started_at, ?) ELSE started_at END,
            completed_at=CASE WHEN ?='COMPLETED' THEN COALESCE(completed_at, ?) ELSE completed_at END
        WHERE id=?
        """,
        (desired, row["provider_id"], desired, row["agreed_amount"], desired, now_str(),
         desired, now_str(), row["mission_task_id"]),
    )
    refresh_mission_state(connection, int(row["mission_id"]))
    return desired


# ---------------------------------------------------------------------------
# MODULE 3 — SERVICE BLACK BOX
# ---------------------------------------------------------------------------

def log_evidence(
    connection, request_id=None, mission_id=None, task_id=None, asset_id=None,
    stage="EVENT", kind="NOTE", path=None, note=None, actor_user_id=None,
    actor_role=None, amount=None, data_source="REAL",
):
    connection.execute(
        """
        INSERT INTO service_evidence(
            request_id, mission_id, task_id, asset_id, stage, kind, path, note,
            actor_user_id, actor_role, amount, data_source
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (request_id, mission_id, task_id, asset_id, stage, kind, path, note,
         actor_user_id, actor_role, amount, data_source),
    )
    return connection.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


def evidence_for_request(connection, request_id):
    return connection.execute(
        "SELECT * FROM service_evidence WHERE request_id=? ORDER BY datetime(created_at), id", (request_id,)
    ).fetchall()


def black_box_timeline(connection, request_id):
    """Complete, timestamped lifecycle record for one service request.

    Combines the V10 evidence table with the V9.3 request row and
    ``service_events`` so no historical service ever appears empty.
    """
    request_row = connection.execute(
        """
        SELECT sr.*, s.name AS service_name, cu.name AS customer_name, pu.name AS provider_name,
               a.asset_type, a.brand, a.model, a.asset_uid
        FROM service_requests sr
        JOIN services s ON s.id=sr.service_id
        JOIN users cu ON cu.id=sr.customer_id
        LEFT JOIN providers p ON p.id=sr.provider_id
        LEFT JOIN users pu ON pu.id=p.user_id
        LEFT JOIN assets a ON a.id=sr.asset_id
        WHERE sr.id=?
        """,
        (request_id,),
    ).fetchone()
    if not request_row:
        return None

    stages = []

    def add(stage, label, timestamp, detail=None, kind="EVENT", data_source="REAL", actor=None, path=None, icon=None):
        stages.append({
            "stage": stage,
            "label": label,
            "timestamp": timestamp,
            "detail": detail,
            "kind": kind,
            "data_source": data_source,
            "actor": actor,
            "path": path,
            "icon": icon or _stage_icon(stage),
        })

    add("REQUEST", "Customer request created", request_row["created_at"],
        request_row["description"], actor=request_row["customer_name"], path=request_row["image_path"])
    if request_row["ai_problem"] or request_row["ai_structured_json"]:
        structured = loads(request_row["ai_structured_json"], {})
        add("AI_ANALYSIS", "AI analysis",
            request_row["created_at"],
            {
                "problem": request_row["ai_problem"],
                "possible_cause": request_row["ai_possible_cause"],
                "difficulty": request_row["ai_difficulty"],
                "estimated_price": request_row["estimated_price"],
                "safety_note": request_row["ai_safety_note"],
                "source": structured.get("data_source") if structured else "AI",
                "confidence": structured.get("confidence") if structured else None,
            },
            kind="AI", data_source=(structured.get("data_source") if structured else "AI") or "AI")
    if request_row["asset_uid"]:
        add("ASSET", "Asset identified",
            request_row["created_at"],
            f"{request_row['asset_type']} · {request_row['brand'] or ''} {request_row['model'] or ''} ({request_row['asset_uid']})",
            kind="ASSET")
    if request_row["provider_id"]:
        add("PROVIDER_ACCEPTED", "Provider assigned", request_row["accepted_at"],
            request_row["provider_name"], actor=request_row["provider_name"])
        add("ARRIVED", "Provider arrival", request_row["arrived_at"],
            request_row["provider_arrival_note"])
        add("CONFIRMATION", "Customer confirmation code verified", request_row["confirmation_verified_at"], None)
        add("SERVICE_STARTED", "Work started", request_row["service_started_at"], None)
    if request_row["provider_diagnosis"]:
        add("DIAGNOSIS", "Provider diagnosis", request_row["service_started_at"] or request_row["accepted_at"],
            request_row["provider_diagnosis"], kind="DIAGNOSIS", actor="provider")
    if request_row["provider_work_performed"]:
        add("WORK", "Work performed", request_row["completed_at"] or request_row["service_started_at"],
            request_row["provider_work_performed"], kind="WORK", actor="provider")

    for negotiation in connection.execute(
        "SELECT * FROM price_negotiations WHERE request_id=? ORDER BY datetime(created_at)", (request_id,)
    ).fetchall():
        add("PRICE", f"Price {negotiation['status'].lower()} — ₹{float(negotiation['proposed_amount'] or 0):,.0f}",
            negotiation["created_at"], negotiation["message"], kind="PRICE", actor="participant")

    if request_row["upfront_min"] or request_row["agreed_amount"]:
        add("ESTIMATE", "Price estimate shared",
            request_row["created_at"],
            f"Initial range ₹{float(request_row['upfront_min'] or 0):,.0f}–₹{float(request_row['upfront_max'] or 0):,.0f}"
            if request_row["upfront_min"] else None, kind="PRICE")
    if request_row["agreed_amount"]:
        add("PRICE_APPROVED", f"Final amount ₹{float(request_row['agreed_amount'] or 0):,.0f}",
            request_row["verified_at"] or request_row["completed_at"], None, kind="PRICE")

    proof = loads_list(request_row["completion_proof_paths"])
    for path in proof:
        add("AFTER_EVIDENCE", "After-service evidence uploaded", request_row["completed_at"], None, kind="PHOTO", path=path)
    before = loads_list(request_row["before_evidence_paths"])
    for path in before:
        add("BEFORE_EVIDENCE", "Before-service evidence uploaded", request_row["service_started_at"], None, kind="PHOTO", path=path)
    if request_row["customer_verification_path"]:
        add("CUSTOMER_VERIFIED", "Customer verified the work", request_row["verified_at"], None,
            kind="SIGNATURE", path=request_row["customer_verification_path"])
    if request_row["paid_at"]:
        add("PAYMENT", "Payment completed", request_row["paid_at"], None, kind="PAYMENT")
    if request_row["completed_at"]:
        add("COMPLETED", "Service completed", request_row["completed_at"], None, kind="SUCCESS")
    if request_row["cancelled_at"]:
        add("CANCELLED", "Service cancelled", request_row["cancelled_at"], request_row["cancellation_reason"], kind="WARN")

    for row in evidence_for_request(connection, request_id):
        add(row["stage"], row["stage"].replace("_", " ").title(),
            row["created_at"], row["note"], kind=row["kind"], data_source=row["data_source"],
            actor=row["actor_role"], path=row["path"])

    for event in connection.execute(
        "SELECT * FROM service_events WHERE request_id=? ORDER BY datetime(created_at), id", (request_id,)
    ).fetchall():
        add("EVENT", event["event_type"].replace("_", " ").title(), event["created_at"],
            event["message"], kind="EVENT")

    stages.sort(key=lambda item: str(item["timestamp"] or "9999"))

    certificate = connection.execute(
        "SELECT * FROM service_certificates WHERE request_id=? ORDER BY id DESC LIMIT 1", (request_id,)
    ).fetchone()
    parts = connection.execute(
        "SELECT * FROM service_parts WHERE request_id=? ORDER BY id", (request_id,)
    ).fetchall()
    mission = None
    if request_row["mission_id"]:
        mission = get_mission(connection, int(request_row["mission_id"]))

    return {
        "request": dict(request_row),
        "stages": stages,
        "certificate": dict(certificate) if certificate else None,
        "parts": [dict(row) for row in parts],
        "mission": dict(mission) if mission else None,
        "before_verified": bool(proof and (before or request_row["image_path"])),
        "after_verified": bool(proof),
        "customer_confirmed": bool(request_row["customer_verification_path"]),
    }


def _stage_icon(stage):
    return {
        "REQUEST": "📝", "AI_ANALYSIS": "🤖", "ASSET": "🧾", "PROVIDER_ACCEPTED": "🤝",
        "ARRIVED": "📍", "DIAGNOSIS": "🔍", "PRICE": "₹", "PRICE_APPROVED": "✅",
        "BEFORE_EVIDENCE": "📷", "WORK": "🛠️", "AFTER_EVIDENCE": "📸", "CUSTOMER_VERIFIED": "☑️",
        "PAYMENT": "💳", "COMPLETED": "🎉", "CANCELLED": "⛔", "CONFIRMATION": "🔐",
        "UNDERSTAND": "🧠", "PLAN": "🗺️", "MATCH": "🎯", "EVENT": "•",
    }.get(stage, "•")


# ---------------------------------------------------------------------------
# MODULE 3b — SERVICE CERTIFICATE
# ---------------------------------------------------------------------------

def issue_certificate(connection, request_id, force=False):
    """Create (or refresh) the Service Certificate for a request. Idempotent."""
    existing = connection.execute(
        "SELECT * FROM service_certificates WHERE request_id=? ORDER BY id DESC LIMIT 1", (request_id,)
    ).fetchone()
    if existing and not force:
        return dict(existing)

    row = connection.execute(
        """
        SELECT sr.*, s.name AS service_name, a.asset_type, a.brand, a.model, a.asset_uid
        FROM service_requests sr
        JOIN services s ON s.id=sr.service_id
        LEFT JOIN assets a ON a.id=sr.asset_id
        WHERE sr.id=?
        """,
        (request_id,),
    ).fetchone()
    if not row:
        return None
    if not force and row["status"] != "COMPLETED":
        return None

    mission_id = row["mission_id"]
    task_id = row["mission_task_id"]
    asset_id = row["asset_id"]
    parts = connection.execute(
        "SELECT * FROM service_parts WHERE request_id=? ORDER BY id", (request_id,)
    ).fetchall()
    parts_json = dumps([dict(part) for part in parts])
    parts_cost = sum(
        float(part["unit_price_max"] or part["unit_price_min"] or 0) * int(part["quantity"] or 1)
        for part in parts if part["status"] in ("USED", "APPROVED", "PROVIDED")
    )
    evidence_count = connection.execute(
        "SELECT COUNT(*) c FROM service_evidence WHERE request_id=?", (request_id,)
    ).fetchone()["c"]
    warranty = connection.execute(
        "SELECT * FROM service_warranties WHERE request_id=?", (request_id,)
    ).fetchone()
    warranty_days = int(warranty["warranty_days"] if warranty else (row["warranty_days"] or 0))
    warranty_until = warranty["warranty_until"] if warranty else row["warranty_until"]

    analysis = loads(row["ai_structured_json"], {})
    final_amount = row["final_amount"] if row["final_amount"] is not None else row["agreed_amount"]

    code = existing["certificate_code"] if existing else certificate_code()
    payload = (
        code, request_id, mission_id, task_id, asset_id, row["customer_id"], row["provider_id"],
        row["service_name"], row["description"], row["ai_problem"] or analysis.get("problem"),
        row["provider_diagnosis"], row["provider_work_performed"], parts_json,
        _to_float(row["estimated_price"]) or _to_float(row["upfront_max"]),
        parts_cost, _to_float(row["platform_fee"]) or 0, _to_float(final_amount),
        _to_float(row["budget_customer"]), _to_float(row["agreed_amount"]),
        1 if (row["image_path"] or loads_list(row["before_evidence_paths"])) else 0,
        1 if loads_list(row["completion_proof_paths"]) else 0,
        1 if row["customer_verification_path"] else 0,
        int(evidence_count or 0), warranty_days, warranty_until,
        "DEMO" if (analysis.get("data_source") == "DEMO") else "REAL",
    )
    if existing:
        connection.execute(
            """
            UPDATE service_certificates SET
                mission_id=?, task_id=?, asset_id=?, provider_id=?, service_name=?, problem=?,
                ai_assessment=?, provider_diagnosis=?, work_performed=?, parts_json=?,
                original_estimate=?, parts_cost=?, service_fee=?, final_amount=?, customer_budget=?,
                negotiated_amount=?, before_verified=?, after_verified=?, customer_confirmed=?,
                evidence_count=?, warranty_days=?, warranty_until=?
            WHERE id=?
            """,
            (*payload[2:], existing["id"]),
        )
        certificate_id = existing["id"]
    else:
        connection.execute(
            """
            INSERT INTO service_certificates(
                certificate_code, request_id, mission_id, task_id, asset_id, customer_id, provider_id,
                service_name, problem, ai_assessment, provider_diagnosis, work_performed, parts_json,
                original_estimate, parts_cost, service_fee, final_amount, customer_budget,
                negotiated_amount, before_verified, after_verified, customer_confirmed, evidence_count,
                warranty_days, warranty_until, data_source
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            payload,
        )
        certificate_id = connection.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    if warranty:
        connection.execute(
            "UPDATE service_warranties SET asset_id=COALESCE(asset_id,?), certificate_id=? WHERE id=?",
            (asset_id, certificate_id, warranty["id"]),
        )
    return dict(connection.execute(
        "SELECT * FROM service_certificates WHERE id=?", (certificate_id,)
    ).fetchone())


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def certificate_by_code(connection, code):
    return connection.execute(
        "SELECT * FROM service_certificates WHERE certificate_code=?", (code,)
    ).fetchone()


# ---------------------------------------------------------------------------
# MODULE 4 — SECOND OPINION
# ---------------------------------------------------------------------------

def create_second_opinion(connection, customer_id, request_id=None, mission_id=None, task_id=None,
                          proposed_repair=None, quoted_amount=None, provider_diagnosis=None, question=None):
    request_row = None
    if request_id:
        request_row = connection.execute(
            "SELECT * FROM service_requests WHERE id=? AND customer_id=?", (request_id, customer_id)
        ).fetchone()
        if not request_row:
            return None, "Service not found for this customer."
    asset_id = request_row["asset_id"] if request_row else None
    connection.execute(
        """
        INSERT INTO second_opinion_requests(
            request_id, mission_id, task_id, asset_id, customer_id, proposed_repair,
            quoted_amount, provider_diagnosis, customer_question, status
        ) VALUES(?,?,?,?,?,?,?,?,?, 'PENDING')
        """,
        (request_id, mission_id, task_id, asset_id, customer_id, proposed_repair,
         _to_float(quoted_amount), provider_diagnosis, question),
    )
    opinion_id = connection.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    if request_id:
        connection.execute(
            "UPDATE service_requests SET second_opinion_status='REQUESTED' WHERE id=?", (request_id,)
        )
        log_evidence(
            connection, request_id=request_id, asset_id=asset_id, stage="SECOND_OPINION", kind="REQUEST",
            note=f"Customer requested a SmartServe second opinion on: {(proposed_repair or 'proposed repair')[:300]}",
            actor_user_id=customer_id, actor_role="customer",
        )
    return opinion_id, None


def run_second_opinion(connection, opinion_id):
    row = connection.execute(
        "SELECT * FROM second_opinion_requests WHERE id=?", (opinion_id,)
    ).fetchone()
    if not row:
        return None
    request_row = None
    if row["request_id"]:
        request_row = connection.execute(
            "SELECT * FROM service_requests WHERE id=?", (row["request_id"],)
        ).fetchone()
    asset = get_asset(connection, row["asset_id"]) if row["asset_id"] else None
    history = [dict(item) for item in asset_history(connection, row["asset_id"], limit=6)] if row["asset_id"] else []
    previous_parts = []
    for item in connection.execute(
        "SELECT part_name FROM service_parts WHERE asset_id=? ORDER BY id DESC LIMIT 10", (row["asset_id"],)
    ).fetchall() if row["asset_id"] else []:
        previous_parts.append(item["part_name"])

    warranty = connection.execute(
        "SELECT warranty_until, CASE WHEN datetime(warranty_until) >= datetime('now') THEN 'ACTIVE' ELSE 'EXPIRED' END AS status FROM service_warranties WHERE request_id=?",
        (row["request_id"],),
    ).fetchone() if row["request_id"] else None

    context = {
        "service_name": request_row["provider_diagnosis"] and None or (
            connection.execute("SELECT name FROM services WHERE id=?", (request_row["service_id"],)).fetchone()["name"]
            if request_row else None
        ),
        "problem": (request_row["description"] if request_row else row["customer_question"]),
        "proposal": row["proposed_repair"] or (request_row["provider_diagnosis"] if request_row else ""),
        "quoted_amount": row["quoted_amount"],
        "provider_diagnosis": row["provider_diagnosis"] or (request_row["provider_diagnosis"] if request_row else None),
        "asset": dict(asset) if asset else {},
        "history": history,
        "previous_parts": previous_parts,
        "warranty_status": warranty["status"] if warranty else "UNKNOWN",
        "evidence": (
            f"{connection.execute('SELECT COUNT(*) c FROM service_evidence WHERE request_id=?', (row['request_id'],)).fetchone()['c']} evidence records"
            if row["request_id"] else None
        ),
        "ai_analysis": loads(request_row["ai_structured_json"], {}) if request_row else {},
    }
    result = ai_layer.second_opinion(context)

    connection.execute(
        """
        INSERT INTO second_opinion_results(
            opinion_request_id, summary, explanation, supporting_evidence, information_required,
            alternatives, recommended_step, confidence, safety_notes, evidence_used_json,
            model_used, data_source, disclaimer
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(opinion_request_id) DO UPDATE SET
            summary=excluded.summary, explanation=excluded.explanation,
            supporting_evidence=excluded.supporting_evidence, information_required=excluded.information_required,
            alternatives=excluded.alternatives, recommended_step=excluded.recommended_step,
            confidence=excluded.confidence, safety_notes=excluded.safety_notes,
            evidence_used_json=excluded.evidence_used_json, model_used=excluded.model_used,
            data_source=excluded.data_source, disclaimer=excluded.disclaimer
        """,
        (
            opinion_id, result.get("summary"), result.get("explanation"),
            dumps(result.get("supporting_evidence") or []), dumps(result.get("information_required") or []),
            dumps(result.get("alternatives") or []), result.get("recommended_step"),
            result.get("confidence"), dumps(result.get("safety_notes") or []),
            dumps({
                "history_count": len(history),
                "previous_parts": previous_parts,
                "warranty": context["warranty_status"],
                "evidence": context["evidence"],
            }),
            result.get("model_used"), result.get("data_source", "RULE_BASED"), result.get("disclaimer"),
        ),
    )
    connection.execute(
        "UPDATE second_opinion_requests SET status='COMPLETED' WHERE id=?", (opinion_id,)
    )
    if row["request_id"]:
        connection.execute(
            "UPDATE service_requests SET second_opinion_status='COMPLETED' WHERE id=?", (row["request_id"],)
        )
        log_evidence(
            connection, request_id=row["request_id"], asset_id=row["asset_id"], stage="SECOND_OPINION",
            kind="RESULT",
            note=(f"Second opinion ({result.get('data_source')}, confidence {result.get('confidence')}): "
                  f"{result.get('recommended_step')}"),
            actor_role="system", data_source=result.get("data_source", "RULE_BASED"),
        )
    return result


def second_opinion_for_request(connection, request_id):
    row = connection.execute(
        """
        SELECT r.*, o.proposed_repair, o.quoted_amount, o.created_at AS requested_at, o.status AS request_status
        FROM second_opinion_requests o
        LEFT JOIN second_opinion_results r ON r.opinion_request_id=o.id
        WHERE o.request_id=? ORDER BY o.id DESC LIMIT 1
        """,
        (request_id,),
    ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# MODULE 5 — PARTS INTELLIGENCE
# ---------------------------------------------------------------------------

def suggest_parts_for_request(connection, request_id, force=False):
    """Create part suggestions for a request when they do not exist yet."""
    existing = connection.execute(
        "SELECT COUNT(*) c FROM service_parts WHERE request_id=?", (request_id,)
    ).fetchone()["c"]
    if existing and not force:
        return connection.execute(
            "SELECT * FROM service_parts WHERE request_id=? ORDER BY id", (request_id,)
        ).fetchall()
    row = connection.execute(
        """
        SELECT sr.*, s.name AS service_name, a.brand, a.model, a.asset_type
        FROM service_requests sr JOIN services s ON s.id=sr.service_id
        LEFT JOIN assets a ON a.id=sr.asset_id WHERE sr.id=?
        """,
        (request_id,),
    ).fetchone()
    if not row:
        return []
    analysis = loads(row["ai_structured_json"], {})
    asset = {"model": row["model"], "brand": row["brand"], "asset_type": row["asset_type"]}
    suggestions = ai_layer.suggest_parts(row["service_name"], row["description"], analysis, asset)
    for part in suggestions:
        connection.execute(
            """
            INSERT INTO service_parts(
                request_id, mission_id, task_id, asset_id, part_name, part_number, compatibility,
                quantity, unit_price_min, unit_price_max, supply_mode, availability, availability_note,
                supplier_note, approval_status, warranty_days, status, data_source
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                request_id, row["mission_id"], row["mission_task_id"], row["asset_id"],
                part["part_name"], part["part_number"], part["compatibility"], part["quantity"],
                part["unit_price_min"], part["unit_price_max"], part["supply_mode"],
                part["availability"], part["availability_note"], part["supplier_note"],
                part["approval_status"], part["warranty_days"], "SUGGESTED", part["data_source"],
            ),
        )
    if suggestions:
        log_evidence(
            connection, request_id=request_id, asset_id=row["asset_id"], mission_id=row["mission_id"],
            task_id=row["mission_task_id"], stage="PARTS", kind="LIST",
            note=f"{len(suggestions)} likely part(s) identified (estimated availability).",
            actor_role="system", data_source="ESTIMATE",
        )
    return connection.execute(
        "SELECT * FROM service_parts WHERE request_id=? ORDER BY id", (request_id,)
    ).fetchall()


def parts_for_request(connection, request_id):
    return connection.execute(
        "SELECT * FROM service_parts WHERE request_id=? ORDER BY id", (request_id,)
    ).fetchall()


def parts_summary(parts):
    if not parts:
        return {"count": 0, "estimated_cost": 0, "approved_cost": 0, "pending_approval": 0}
    estimated = 0.0
    approved = 0.0
    pending = 0
    for part in parts:
        unit = float(part["unit_price_max"] or part["unit_price_min"] or 0)
        line = unit * int(part["quantity"] or 1)
        estimated += line
        if part["approval_status"] in ("APPROVED", "NOT_REQUIRED") and part["status"] in ("PROVIDED", "USED", "APPROVED"):
            approved += line
        if part["approval_status"] == "PENDING":
            pending += 1
    return {
        "count": len(parts),
        "estimated_cost": round(estimated, 2),
        "approved_cost": round(approved, 2),
        "pending_approval": pending,
    }


# ---------------------------------------------------------------------------
# MODULE 6 — OUTCOME + RECOVERY
# ---------------------------------------------------------------------------

def register_outcome(connection, request_id):
    row = connection.execute(
        """
        SELECT sr.*, a.warranty_end AS asset_warranty_end
        FROM service_requests sr LEFT JOIN assets a ON a.id=sr.asset_id WHERE sr.id=?
        """,
        (request_id,),
    ).fetchone()
    if not row:
        return None
    warranty = connection.execute(
        "SELECT * FROM service_warranties WHERE request_id=?", (request_id,)
    ).fetchone()
    warranty_until = warranty["warranty_until"] if warranty else row["warranty_until"]
    status = "ACTIVE" if warranty_until and (str(warranty_until) >= now_str()) else ("EXPIRED" if warranty_until else "NONE")
    monitored_until = (now() + timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S")
    connection.execute(
        """
        INSERT INTO service_outcomes(
            request_id, asset_id, customer_id, provider_id, outcome_status, warranty_status,
            warranty_until, monitored_until, last_checked_at, updated_at
        ) VALUES(?,?,?,?, 'MONITORING', ?, ?, ?, ?, ?)
        ON CONFLICT(request_id) DO UPDATE SET
            outcome_status='MONITORING', warranty_status=excluded.warranty_status,
            warranty_until=excluded.warranty_until, monitored_until=excluded.monitored_until,
            last_checked_at=excluded.last_checked_at, updated_at=excluded.updated_at
        """,
        (request_id, row["asset_id"], row["customer_id"], row["provider_id"], status,
         warranty_until, monitored_until, now_str(), now_str()),
    )
    return {"warranty_status": status, "warranty_until": warranty_until, "monitored_until": monitored_until}


def recovery_candidates(connection, customer_id, limit=25):
    """Completed services that can be reopened as a recovery case."""
    return connection.execute(
        """
        SELECT sr.id, sr.description, sr.status, sr.completed_at, sr.asset_id, sr.provider_id,
               sr.agreed_amount, sr.final_amount, sr.warranty_until, sr.warranty_days,
               s.name AS service_name, cu.name AS provider_name,
               a.asset_type, a.brand, a.model, a.asset_uid,
               CASE WHEN sr.warranty_until IS NOT NULL AND sr.warranty_until >= datetime('now') THEN 'ACTIVE'
                    WHEN sr.warranty_until IS NOT NULL THEN 'EXPIRED' ELSE 'NONE' END AS warranty_status,
               (SELECT id FROM service_recovery_cases c WHERE c.original_request_id=sr.id AND c.status='OPEN' LIMIT 1) AS open_case_id
        FROM service_requests sr
        JOIN services s ON s.id=sr.service_id
        LEFT JOIN providers p ON p.id=sr.provider_id
        LEFT JOIN users cu ON cu.id=p.user_id
        LEFT JOIN assets a ON a.id=sr.asset_id
        WHERE sr.customer_id=? AND sr.status='COMPLETED'
        ORDER BY datetime(sr.completed_at) DESC LIMIT ?
        """,
        (customer_id, limit),
    ).fetchall()


def create_recovery_case(connection, customer_id, original_request_id, reported_problem,
                         evidence_paths=None, severity="MEDIUM", actor_user_id=None):
    original = connection.execute(
        """
        SELECT sr.*, s.name AS service_name, a.asset_uid, a.asset_type
        FROM service_requests sr JOIN services s ON s.id=sr.service_id
        LEFT JOIN assets a ON a.id=sr.asset_id
        WHERE sr.id=? AND sr.customer_id=?
        """,
        (original_request_id, customer_id),
    ).fetchone()
    if not original:
        return None, "Original service not found."

    warranty = connection.execute(
        "SELECT * FROM service_warranties WHERE request_id=?", (original_request_id,)
    ).fetchone()
    warranty_until = warranty["warranty_until"] if warranty else original["warranty_until"]
    if warranty_until:
        warranty_status = "ACTIVE" if str(warranty_until) >= now_str() else "EXPIRED"
    else:
        warranty_status = "NONE"

    classification = ai_layer.classify_recovery(reported_problem, original["description"], original["service_name"])
    code = recovery_code()
    connection.execute(
        """
        INSERT INTO service_recovery_cases(
            case_code, customer_id, original_request_id, asset_id, provider_id, reported_problem,
            recurrence_category, severity, warranty_status, warranty_until, match_confidence, match_reason, status
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'OPEN')
        """,
        (code, customer_id, original_request_id, original["asset_id"], original["provider_id"],
         reported_problem, classification["category"], severity, warranty_status, warranty_until,
         classification["confidence"], classification["reason"]),
    )
    case_id = connection.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    for path in evidence_paths or []:
        log_evidence(
            connection, request_id=original_request_id, asset_id=original["asset_id"],
            stage="RECOVERY_EVIDENCE", kind="PHOTO", path=path,
            note=f"Recovery evidence for case {code}.", actor_user_id=actor_user_id, actor_role="customer",
        )
    log_evidence(
        connection, request_id=original_request_id, asset_id=original["asset_id"],
        stage="RECOVERY", kind="CASE",
        note=f"Recovery case {code} opened. {classification['reason']} Warranty: {warranty_status}.",
        actor_user_id=actor_user_id, actor_role="customer",
    )
    connection.execute(
        """
        UPDATE service_outcomes SET outcome_status='RECURRENCE_REPORTED', recurrence_count=recurrence_count+1,
            updated_at=? WHERE request_id=?
        """,
        (now_str(), original_request_id),
    )
    if original["asset_id"]:
        connection.execute(
            "UPDATE assets SET health_score=MAX(20, COALESCE(health_score,80)-8), health_note=?, updated_at=? WHERE id=?",
            (f"Recurrence reported on {now().strftime('%d %b %Y')}", now_str(), original["asset_id"]),
        )
    return case_id, None


def recovery_case_detail(connection, case_id):
    case = connection.execute(
        """
        SELECT c.*, a.asset_type, a.brand, a.model, a.asset_uid, a.warranty_end AS asset_warranty_end
        FROM service_recovery_cases c LEFT JOIN assets a ON a.id=c.asset_id WHERE c.id=?
        """,
        (case_id,),
    ).fetchone()
    if not case:
        return None
    original = connection.execute(
        """
        SELECT sr.*, s.name AS service_name, pu.name AS provider_name, cu.name AS customer_name
        FROM service_requests sr JOIN services s ON s.id=sr.service_id
        LEFT JOIN providers p ON p.id=sr.provider_id LEFT JOIN users pu ON pu.id=p.user_id
        LEFT JOIN users cu ON cu.id=sr.customer_id
        WHERE sr.id=?
        """,
        (case["original_request_id"],),
    ).fetchone()
    certificate = connection.execute(
        "SELECT * FROM service_certificates WHERE request_id=? ORDER BY id DESC LIMIT 1",
        (case["original_request_id"],),
    ).fetchone()
    parts = connection.execute(
        "SELECT * FROM service_parts WHERE request_id=? ORDER BY id", (case["original_request_id"],)
    ).fetchall()
    evidence = connection.execute(
        "SELECT * FROM service_evidence WHERE request_id=? ORDER BY datetime(created_at) DESC",
        (case["original_request_id"],),
    ).fetchall()
    return {
        "case": dict(case),
        "original": dict(original) if original else None,
        "certificate": dict(certificate) if certificate else None,
        "parts": [dict(row) for row in parts],
        "evidence": [dict(row) for row in evidence],
        "options": recovery_options(case, original),
    }


def recovery_options(case, original):
    options = [
        {"key": "CONTACT_PROVIDER", "label": "Contact original provider",
         "description": "Share the new evidence with the professional who performed the service.",
         "available": True},
        {"key": "NEW_PROVIDER", "label": "Request another provider",
         "description": "Create a new SmartServe booking for the same asset and problem.",
         "available": True},
        {"key": "SECOND_OPINION", "label": "Request second opinion",
         "description": "Get neutral SmartServe decision support before approving another repair.",
         "available": True},
        {"key": "OPEN_DISPUTE", "label": "Open dispute",
         "description": "Ask the SmartServe operations team to review the case.",
         "available": True},
    ]
    if case and case["warranty_status"] != "ACTIVE":
        for option in options:
            if option["key"] == "CONTACT_PROVIDER":
                option["description"] = (
                    "Warranty has expired for this service, so a revisit may be chargeable."
                    if case["warranty_status"] == "EXPIRED" else
                    "No warranty was recorded for this service."
                )
    return options


def resolve_recovery_case(connection, case_id, action=None, resolution=None, new_request_id=None):
    connection.execute(
        """
        UPDATE service_recovery_cases SET status='RESOLVED', chosen_action=COALESCE(?, chosen_action),
            resolution=COALESCE(?, resolution), new_request_id=COALESCE(?, new_request_id),
            resolved_at=?, updated_at=? WHERE id=?
        """,
        (action, resolution, new_request_id, now_str(), now_str(), case_id),
    )


# ---------------------------------------------------------------------------
# MODULE 7 — STUDENT SKILL PASSPORT + ELIGIBILITY
# ---------------------------------------------------------------------------

def default_skill_levels(service_rows):
    """A new provider starts at TRAINING level for the services they offer."""
    out = []
    for service in service_rows:
        out.append({
            "skill_key": _slug(service["name"]),
            "skill_label": service["name"],
            "category": service["category"] if "category" in service.keys() else None,
            "level": 10,
            "status": "TRAINING",
        })
    return out


def _slug(text):
    return "".join(ch if ch.isalnum() else "_" for ch in str(text).lower()).strip("_")


def sync_student_skills(connection, provider_id, service_rows=None):
    if service_rows is None:
        service_rows = connection.execute(
            "SELECT s.id, s.name, s.category FROM provider_services ps JOIN services s ON s.id=ps.service_id WHERE ps.provider_id=?",
            (provider_id,),
        ).fetchall()
    for service in service_rows:
        connection.execute(
            """
            INSERT INTO student_skills(provider_id, skill_key, skill_label, category, level, status)
            VALUES(?,?,?,?,10,'TRAINING')
            ON CONFLICT(provider_id, skill_key) DO UPDATE SET
                skill_label=excluded.skill_label,
                category=COALESCE(excluded.category, student_skills.category)
            """,
            (provider_id, _slug(service["name"]), service["name"],
             service["category"] if "category" in service.keys() else None),
        )
    return connection.execute(
        "SELECT * FROM student_skills WHERE provider_id=? ORDER BY category, level DESC", (provider_id,)
    ).fetchall()


def student_skill_passport(connection, provider_id):
    provider = connection.execute(
        """
        SELECT p.*, u.name, u.email, u.phone, u.profile_photo_path, u.email_verified, u.phone_verified
        FROM providers p JOIN users u ON u.id=p.user_id WHERE p.id=?
        """,
        (provider_id,),
    ).fetchone()
    if not provider:
        return None
    services = connection.execute(
        "SELECT s.id, s.name, s.category, s.risk_level, s.required_certification FROM provider_services ps JOIN services s ON s.id=ps.service_id WHERE ps.provider_id=?",
        (provider_id,),
    ).fetchall()
    skills = sync_student_skills(connection, provider_id, services)
    certifications = connection.execute(
        "SELECT * FROM provider_certifications WHERE provider_id=? ORDER BY created_at DESC", (provider_id,)
    ).fetchall()
    verifications = connection.execute(
        "SELECT * FROM student_skill_verifications WHERE provider_id=? ORDER BY created_at DESC LIMIT 50",
        (provider_id,),
    ).fetchall()
    completed = connection.execute(
        "SELECT COUNT(*) c FROM service_requests WHERE provider_id=? AND status='COMPLETED'", (provider_id,)
    ).fetchone()["c"]
    restricted = []
    eligible = []
    for skill in skills:
        row = dict(skill)
        for service in services:
            if _slug(service["name"]) == skill["skill_key"]:
                row["risk_level"] = service["risk_level"]
                row["required_certification"] = service["required_certification"]
                if service["risk_level"] in (catalog.RISK_HIGH, catalog.RISK_RESTRICTED) and skill["status"] not in ("VERIFIED",):
                    row["blocked_reason"] = (
                        "Requires verified skill level or a professional certification before SmartServe can send this job."
                    )
                break
        (eligible if not row.get("blocked_reason") else restricted).append(row)

    certified_names = {c["name"] for c in certifications if c["status"] == "VERIFIED"}
    total_level = sum(int(s["level"] or 0) for s in skills) / max(1, len(skills))
    return {
        "provider": dict(provider),
        "skills": [dict(s) for s in skills],
        "eligible": eligible,
        "restricted": restricted,
        "certifications": [dict(c) for c in certifications],
        "verifications": [dict(v) for v in verifications],
        "verified_certifications": sorted(certified_names),
        "completed_jobs": int(completed),
        "overall_level": round(total_level),
        "progress_stages": [
            {"key": "TRAINING", "label": "Training", "done": True},
            {"key": "SUPERVISED", "label": "Supervised service",
             "done": any(s["status"] in ("SUPERVISED", "VERIFIED") for s in skills)},
            {"key": "VERIFICATION", "label": "Verification",
             "done": any(s["status"] == "VERIFIED" for s in skills)},
            {"key": "FEEDBACK", "label": "Customer feedback", "done": int(completed) > 0},
            {"key": "ASSESSMENT", "label": "Skill assessment", "done": bool(certifications)},
            {"key": "ELIGIBILITY", "label": "Higher eligibility",
             "done": all(s["status"] == "VERIFIED" for s in skills) and bool(skills)},
        ],
    }


def provider_eligibility(connection, provider_id, service_row):
    """Matching gate used by the V10-aware matching layer.

    Returns ``(allowed: bool, reason: str|None, requirements: list)``.

    * LOW/MEDIUM risk services: the provider only needs to offer the service.
    * HIGH/RESTRICTED: the provider needs a verified skill row or a verified
      certification matching ``services.required_certification``.
    """
    if not service_row:
        return True, None, []
    risk = (service_row["risk_level"] if "risk_level" in service_row.keys() else None) or catalog.RISK_MEDIUM
    name = service_row["name"]
    requirements = []
    certification = None
    if "required_certification" in service_row.keys():
        certification = service_row["required_certification"]
    if certification:
        requirements.append(certification)

    provider = connection.execute("SELECT * FROM providers WHERE id=?", (provider_id,)).fetchone()
    is_student = bool(provider) and (
        (provider["provider_type"] if "provider_type" in provider.keys() else None) == "student"
        or (provider["student_status"] if "student_status" in provider.keys() else None) in ("TRAINEE", "TRAINING", "SUPERVISED")
    )
    approved = bool(provider) and (
        bool(provider["approved"]) if "approved" in provider.keys() else False
    )
    if provider and "ekyc_status" in provider.keys() and provider["ekyc_status"] == "VERIFIED":
        approved = True
    if provider and "verification_score" in provider.keys() and (provider["verification_score"] or 0) >= 60:
        approved = True

    # A verified skill row is the strongest signal and always passes.
    skill = connection.execute(
        "SELECT * FROM student_skills WHERE provider_id=? AND skill_key=?", (provider_id, _slug(name))
    ).fetchone()
    if skill and skill["status"] == "VERIFIED":
        return True, None, requirements

    # A licensed service (declared certification) always needs the credential.
    if certification:
        match = connection.execute(
            """
            SELECT id FROM provider_certifications
            WHERE provider_id=? AND status='VERIFIED' AND (
                LOWER(name)=LOWER(?) OR LOWER(COALESCE(credential_id,''))=LOWER(?)
                OR LOWER(?) LIKE '%' || LOWER(name) || '%'
            ) LIMIT 1
            """,
            (provider_id, certification, certification, certification),
        ).fetchone()
        if match:
            return True, None, requirements
        reason = (
            f"This service requires the verified credential '{certification}' before SmartServe can send it to you."
        )
        return False, reason, requirements

    # Students and trainees must be verified skill-by-skill.
    if is_student:
        reason = (
            "Your Skill Passport marks this capability as not yet verified. "
            "Request a verification from the Skill Passport page to unlock it."
        )
        return False, reason, requirements

    # Low/medium risk work is open to any provider offering the service.
    if risk in (catalog.RISK_LOW, catalog.RISK_MEDIUM):
        return True, None, requirements

    # High-risk work: an approved professional is qualified by trade approval.
    if approved:
        return True, None, requirements
    reason = (
        f"This service is marked {risk} risk. Complete SmartServe verification "
        "(KYC or skill assessment) before accepting it."
    )
    return False, reason, requirements


def skill_verification_requests(connection, status="PENDING"):
    return connection.execute(
        """
        SELECT s.*, p.id AS provider_id, u.name AS provider_name, u.email,
               (SELECT risk_level FROM services WHERE LOWER(name)=LOWER(s.skill_label) LIMIT 1) AS risk_level
        FROM student_skills s
        JOIN providers p ON p.id=s.provider_id
        JOIN users u ON u.id=p.user_id
        WHERE s.status IN ('TRAINING','SUPERVISED')
        ORDER BY s.updated_at DESC LIMIT 100
        """
    ).fetchall()


def verify_skill(connection, skill_id, method="ASSESSMENT", result="VERIFIED", notes=None, admin_user_id=None):
    skill = connection.execute("SELECT * FROM student_skills WHERE id=?", (skill_id,)).fetchone()
    if not skill:
        return None
    new_status = "VERIFIED" if result == "VERIFIED" else "TRAINING"
    new_level = 80 if result == "VERIFIED" else int(skill["level"] or 0)
    if result == "SUPERVISED":
        new_status = "SUPERVISED"
        new_level = max(int(skill["level"] or 0), 55)
    if result == "RESTRICTED":
        new_status = "RESTRICTED"
        new_level = 0
    connection.execute(
        "UPDATE student_skills SET status=?, level=?, verified_jobs=verified_jobs + CASE WHEN ?='VERIFIED' THEN 1 ELSE 0 END, updated_at=? WHERE id=?",
        (new_status, new_level, result, now_str(), skill_id),
    )
    connection.execute(
        """
        INSERT INTO student_skill_verifications(provider_id, student_skill_id, skill_key, method, result, notes, verified_by_user_id)
        VALUES(?,?,?,?,?,?,?)
        """,
        (skill["provider_id"], skill_id, skill["skill_key"], method, new_status, notes, admin_user_id),
    )
    connection.execute(
        "UPDATE providers SET provider_type=COALESCE(provider_type,'professional') WHERE id=?", (skill["provider_id"],)
    )
    return {"status": new_status, "level": new_level}


# ---------------------------------------------------------------------------
# Central hook used by the existing V9.3 request flow
# ---------------------------------------------------------------------------

STAGE_FOR_EVENT = {
    "PROVIDER_DISCOVERY_STARTED": "MATCH",
    "CUSTOMER_SELECTED_PROVIDER": "MATCH",
    "PROVIDER_ACCEPTED": "COORDINATE",
    "PROVIDER_REJECTED": "MATCH",
    "PROVIDER_CANCELLED": "COORDINATE",
    "PRICE_PROPOSED": "COORDINATE",
    "PRICE_ACCEPTED": "SERVICE",
    "PRICE_REJECTED": "COORDINATE",
    "COMPLETION_PROOF_SUBMITTED": "VERIFY",
    "CANCELLED": "COORDINATE",
    "DISPUTE_OPENED": "RECOVER",
    "SOS_TRIGGERED": "SERVICE",
    "SCHEDULED_SERVICE_ACTIVATED": "MATCH",
}


def on_request_event(connection, request_id, event_type, message=None, actor_user_id=None, metadata=None):
    """Mirror an existing V9.3 service event into the V10 black box.

    Called from ``app._log_event`` so every existing workflow automatically
    feeds the Service Black Box without rewriting those routes.
    """
    try:
        if not request_id:
            return
        row = connection.execute(
            "SELECT id, asset_id, mission_id, mission_task_id, lifecycle_stage FROM service_requests WHERE id=?",
            (request_id,),
        ).fetchone()
        if not row:
            return
        stage = STAGE_FOR_EVENT.get(event_type, "EVENT")
        if row["mission_task_id"] and event_type in ("PROVIDER_ACCEPTED", "PROVIDER_REJECTED", "CANCELLED"):
            connection.execute(
                "UPDATE mission_tasks SET status=? WHERE id=? AND status NOT IN ('COMPLETED','CANCELLED')",
                ("ASSIGNED" if event_type == "PROVIDER_ACCEPTED" else "READY", row["mission_task_id"]),
            )
        if row["mission_id"]:
            refresh_mission_state(connection, int(row["mission_id"]))
        if stage not in ("EVENT",):
            connection.execute(
                "UPDATE service_requests SET lifecycle_stage=? WHERE id=? AND lifecycle_stage IS NOT ?",
                (stage, request_id, stage),
            )
        log_evidence(
            connection, request_id=request_id, mission_id=row["mission_id"],
            task_id=row["mission_task_id"], asset_id=row["asset_id"], stage=stage,
            kind="EVENT", note=f"{event_type.replace('_', ' ').title()}: {message or ''}".strip(),
            actor_user_id=actor_user_id, actor_role="system",
        )
    except Exception as exc:  # never break the existing booking flow
        print("[V10 hook] on_request_event warning:", repr(exc))


def on_request_completed(connection, request_id):
    """RECORD → MONITOR. Idempotent: safe to call more than once."""
    try:
        row = connection.execute(
            "SELECT * FROM service_requests WHERE id=?", (request_id,)
        ).fetchone()
        if not row or row["status"] != "COMPLETED":
            return None
        certificate = issue_certificate(connection, request_id)
        asset_id = row["asset_id"]
        if asset_id:
            exists = connection.execute(
                "SELECT id FROM asset_service_history WHERE asset_id=? AND request_id=?",
                (asset_id, request_id),
            ).fetchone()
            service_name = connection.execute(
                "SELECT name FROM services WHERE id=?", (row["service_id"],)
            ).fetchone()
            parts = connection.execute(
                "SELECT * FROM service_parts WHERE request_id=?", (request_id,)
            ).fetchall()
            parts_payload = [
                {"part_name": part["part_name"], "quantity": part["quantity"], "data_source": part["data_source"]}
                for part in parts if part["status"] in ("USED", "PROVIDED", "APPROVED")
            ]
            if not exists:
                connection.execute(
                    """
                    INSERT INTO asset_service_history(
                        asset_id, request_id, mission_id, task_id, provider_id, certificate_id,
                        service_name, problem, diagnosis, work_performed, parts_json, amount,
                        service_date, warranty_until, warranty_days, evidence_verified, outcome_status, data_source
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        asset_id, request_id, row["mission_id"], row["mission_task_id"], row["provider_id"],
                        certificate["id"] if certificate else None,
                        service_name["name"] if service_name else None,
                        row["description"], row["provider_diagnosis"], row["provider_work_performed"],
                        dumps(parts_payload),
                        _to_float(row["final_amount"]) or _to_float(row["agreed_amount"]),
                        row["completed_at"] or now_str(), row["warranty_until"], row["warranty_days"] or 0,
                        1 if row["customer_verification_path"] else 0, "MONITORING",
                        "REAL",
                    ),
                )
                ensure_maintenance_plan(connection, asset_id)
                connection.execute(
                    "UPDATE asset_maintenance_schedule SET status='UPCOMING' WHERE asset_id=? AND status='DUE'",
                    (asset_id,),
                )
            recompute_health(connection, asset_id)
        register_outcome(connection, request_id)
        if row["mission_task_id"]:
            connection.execute(
                "UPDATE mission_tasks SET status='COMPLETED', completed_at=COALESCE(completed_at, ?) WHERE id=?",
                (now_str(), row["mission_task_id"]),
            )
        if row["mission_id"]:
            refresh_mission_state(connection, int(row["mission_id"]))
        connection.execute(
            "UPDATE service_requests SET lifecycle_stage='MONITOR' WHERE id=?", (request_id,)
        )
        log_evidence(
            connection, request_id=request_id, asset_id=asset_id, mission_id=row["mission_id"],
            task_id=row["mission_task_id"], stage="RECORD", kind="CERTIFICATE",
            note=f"Service Certificate {(certificate or {}).get('certificate_code')} issued; Service Passport updated.",
            actor_role="system",
        )
        return certificate
    except Exception as exc:
        print("[V10 hook] on_request_completed warning:", repr(exc))
        return None


def sweep_completed_services(connection, limit=25):
    """Backfill certificates/outcomes for services completed outside the hook."""
    rows = connection.execute(
        """
        SELECT sr.id FROM service_requests sr
        WHERE sr.status='COMPLETED'
          AND NOT EXISTS (SELECT 1 FROM service_certificates c WHERE c.request_id=sr.id)
        ORDER BY sr.id DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    processed = []
    for row in rows:
        if on_request_completed(connection, int(row["id"])):
            processed.append(int(row["id"]))
    if processed:
        connection.commit()
    return processed


# ---------------------------------------------------------------------------
# Search / discovery helpers (Module: search experience)
# ---------------------------------------------------------------------------

def smart_search(connection, query, limit=20):
    """Problem-aware search across services, categories, providers and assets."""
    query = (query or "").strip()
    if not query:
        return {"services": [], "categories": [], "providers": [], "suggestions": [], "query": ""}
    lowered = query.lower()
    tokens = [token for token in lowered.replace(",", " ").split() if len(token) > 2]

    services = connection.execute(
        "SELECT id,name,description,category,category_key,icon,risk_level,estimated_duration,is_popular FROM services WHERE is_active IS NOT 0"
    ).fetchall()

    scored = []
    for service in services:
        name = (service["name"] or "").lower()
        blob = " ".join(filter(None, [
            name, service["description"] or "", service["category"] or "",
            _safe(service, "keywords"), _safe(service, "subcategory"), _safe(service, "asset_types"),
            _safe(service, "possible_parts"),
        ])).lower()
        score = 0
        if lowered in name:
            score += 12
        for token in tokens:
            if token in name:
                score += 6
            if token in blob:
                score += 3
        if score:
            scored.append((score, dict(service)))
    scored.sort(key=lambda item: (-item[0], item[1]["name"]))
    top_services = [item[1] for item in scored[:limit]]

    categories = connection.execute(
        "SELECT * FROM service_categories WHERE is_active=1 ORDER BY sort_order"
    ).fetchall()
    matched_categories = [
        dict(category) for category in categories
        if lowered in (category["name"] or "").lower() or any(
            token in (category["name"] or "").lower() or token in (category["description"] or "").lower()
            for token in tokens
        )
    ][:6]

    providers = connection.execute(
        """
        SELECT p.id AS provider_id, u.name, p.skills, p.rating, p.experience, u.profile_photo_path, p.provider_type
        FROM providers p JOIN users u ON u.id=p.user_id
        WHERE p.approved=1 AND (
            LOWER(u.name) LIKE ? OR LOWER(COALESCE(p.skills,'')) LIKE ? OR LOWER(COALESCE(p.bio,'')) LIKE ?
        ) ORDER BY p.rating DESC LIMIT ?
        """,
        (f"%{lowered}%", f"%{lowered}%", f"%{lowered}%", 6),
    ).fetchall()

    analysis = ai_layer.offline_analysis(None, query) if tokens else {"required_services": []}
    suggestions = [
        {"service": name, "reason": "Matched your description"} for name in (analysis.get("required_services") or [])[:4]
    ]
    for service in top_services[:3]:
        entry = {"service": service["name"], "reason": f"{service['category']} · suggested from your words"}
        if entry not in suggestions:
            suggestions.append(entry)

    return {
        "query": query,
        "services": top_services,
        "categories": matched_categories,
        "providers": [dict(row) for row in providers],
        "suggestions": suggestions[:6],
        "interpretation": {
            "service_hint": top_services[0]["name"] if top_services else None,
            "data_source": analysis.get("data_source", "RULE_BASED"),
        },
    }


def _safe(row, key):
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Bridge: existing V9.3 bookings automatically get V10 mission tracking
# ---------------------------------------------------------------------------

def attach_legacy_request_to_mission(connection, request_id, actor_user_id=None):
    """Wrap a booking created by the legacy V9.3 form in a V10 mission.

    This is how historical/legacy flows take part in the lifecycle without
    changing their user experience: the mission is planned with the deterministic
    planner (no extra AI call), so booking latency is unaffected.
    """
    try:
        row = connection.execute(
            """
            SELECT sr.*, s.name AS service_name FROM service_requests sr
            JOIN services s ON s.id=sr.service_id WHERE sr.id=?
            """,
            (request_id,),
        ).fetchone()
        if not row or row["mission_id"]:
            return None
        analysis = loads(row["ai_structured_json"], {}) or {
            "problem": row["ai_problem"] or row["description"],
            "possible_causes": [c.strip() for c in (row["ai_possible_cause"] or "").split(",") if c.strip()],
            "required_services": [row["service_name"]],
            "possible_parts": [],
            "confidence": None,
            "safety_notes": [row["ai_safety_note"]] if row["ai_safety_note"] else [],
            "recommended_next_step": "On-site inspection confirms the cause.",
            "difficulty": row["ai_difficulty"] or "Medium",
            "estimated_price_range": {"min": None, "max": None, "currency": "INR"},
            "risk_level": "MEDIUM",
            "data_source": "LEGACY",
            "disclaimer": AI_DISCLAIMER,
        }
        plan = ai_layer.offline_mission(row["description"], service_name=row["service_name"], analysis=analysis)
        code = mission_code()
        connection.execute(
            """
            INSERT INTO service_missions(
                mission_code, customer_id, asset_id, title, problem, description, media_json,
                ai_analysis_json, ai_confidence, required_categories, risk_level, safety_notes,
                priority, status, lifecycle_stage, estimated_total, actual_total, currency,
                address_text, pincode, latitude, longitude, scheduled_at, budget_customer, data_source
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                code, row["customer_id"], row["asset_id"], plan["title"][:200], (row["description"] or "")[:2000],
                row["description"], dumps(loads_list(row["image_path"])), dumps(analysis),
                analysis.get("confidence"), dumps(plan.get("required_services") or []),
                plan.get("risk_level", "MEDIUM"), dumps(analysis.get("safety_notes") or []),
                "HIGH" if plan.get("risk_level") in (catalog.RISK_HIGH, catalog.RISK_RESTRICTED) else "NORMAL",
                "PLANNED", "MATCH", analysis.get("estimated_price_range", {}).get("max"), 0, "INR",
                row["address_text"], row["customer_pincode"], row["customer_latitude"],
                row["customer_longitude"], row["scheduled_at"], row["budget_customer"],
                analysis.get("data_source", "RULE_BASED"),
            ),
        )
        mission_id = connection.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        order_to_id = {}
        for task in plan["tasks"]:
            service_row = _service_by_name(connection, task.get("service_name") or row["service_name"])
            depends_order = int(task.get("depends_on_order") or 0)
            connection.execute(
                """
                INSERT INTO mission_tasks(
                    mission_id, task_order, title, description, service_id, service_name,
                    required_skill, risk_level, depends_on_task_id, status, provider_brief, data_source
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    mission_id, task["task_order"], task["title"][:160], task.get("description"),
                    service_row["id"] if service_row else row["service_id"],
                    task.get("service_name") or row["service_name"],
                    task.get("service_name") or row["service_name"], plan.get("risk_level", "MEDIUM"),
                    order_to_id.get(depends_order), "PENDING",
                    dumps({"expected": task.get("description")}), "RULE_BASED",
                ),
            )
            order_to_id[task["task_order"]] = connection.execute(
                "SELECT last_insert_rowid() AS id"
            ).fetchone()["id"]
        primary = order_to_id.get(1)
        connection.execute(
            "UPDATE service_requests SET mission_id=?, mission_task_id=?, lifecycle_stage='MATCH' WHERE id=? AND mission_id IS NULL",
            (mission_id, primary, request_id),
        )
        if primary:
            connection.execute(
                "UPDATE mission_tasks SET request_id=?, status='ASSIGNED' WHERE id=?", (request_id, primary)
            )
        if row["asset_id"]:
            connection.execute(
                "UPDATE assets SET updated_at=? WHERE id=?", (now_str(), row["asset_id"])
            )
        log_evidence(
            connection, request_id=request_id, mission_id=mission_id, task_id=primary,
            asset_id=row["asset_id"], stage="PLAN", kind="PLAN",
            note=f"Mission {code} linked to an existing SmartServe booking.",
            actor_user_id=actor_user_id, actor_role="system",
        )
        refresh_mission_state(connection, mission_id)
        return {"mission_id": mission_id, "mission_code": code}
    except Exception as exc:
        print("[V10 bridge] attach_legacy_request_to_mission warning:", repr(exc))
        return None
