"""SmartServe V10 — routes (pages + JSON APIs).

Registered on the existing Flask app as a blueprint so that:

* every V9.3 route keeps working exactly as before,
* the V10 layer can be mounted without touching the old route table,
* existing services (matching, chat, tracking, payments, SOS, warranties,
  disputes, reviews, recurring bookings) are reused rather than duplicated.
"""

from __future__ import annotations

import io
import os
import secrets
import uuid
from datetime import datetime, timedelta
from functools import wraps

from flask import (
    Blueprint, abort, current_app, flash, jsonify, redirect, render_template,
    request, send_file, session, url_for,
)

from database import get_db_connection

from . import ai as ai_layer
from . import catalog
from . import engine

v10 = Blueprint("v10", __name__)

DEV_OTP_ENABLED = os.getenv("SMARTSERVE_DEV_OTP", "0") == "1"
OTP_TTL_MINUTES = int(os.getenv("SMARTSERVE_OTP_TTL_MINUTES", "10") or 10)
EMAIL_TOKEN_MINUTES = int(os.getenv("SMARTSERVE_EMAIL_TOKEN_MINUTES", "30") or 30)
OTP_MAX_ATTEMPTS = 5
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
ALLOWED_IMAGE_EXT = {"jpg", "jpeg", "png", "webp"}


# =========================================================================
# Auth / helpers
# =========================================================================

def current_user_id():
    return session.get("user_id")


def current_role():
    return (session.get("role") or "").lower()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user_id():
            if request.path.startswith("/api/"):
                return jsonify({"success": False, "message": "Login required."}), 401
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def role_required(*roles):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not current_user_id():
                if request.path.startswith("/api/"):
                    return jsonify({"success": False, "message": "Login required."}), 401
                return redirect(url_for("login", next=request.path))
            if current_role() not in roles:
                if request.path.startswith("/api/"):
                    return jsonify({"success": False, "message": "This area is for a different account type."}), 403
                flash("This area is for a different account type.")
                return redirect(url_for("home"))
            return view(*args, **kwargs)
        return wrapped
    return decorator


def wants_json():
    return request.path.startswith("/api/") or "application/json" in (request.accept_mimetypes.best_match(["application/json", "text/html"]) or "")


def _ok(payload=None, **extra):
    data = {"success": True}
    if payload:
        data.update(payload)
    data.update(extra)
    return jsonify(data)


def _fail(message, status=400, **extra):
    data = {"success": False, "message": message}
    data.update(extra)
    return jsonify(data), status


def provider_for_user(connection, user_id):
    return connection.execute("SELECT * FROM providers WHERE user_id=?", (user_id,)).fetchone()


def _provider_owns_request(connection, provider_id, request_id):
    row = connection.execute(
        "SELECT id FROM service_requests WHERE id=? AND provider_id=?", (request_id, provider_id)
    ).fetchone()
    return bool(row)


def _provider_can_view_request(connection, provider_id, request_row):
    """Assigned providers, plus eligible providers considering an open request."""
    if request_row["provider_id"] and int(request_row["provider_id"]) == int(provider_id):
        return True, "assigned"
    status = (request_row["status"] or "").upper()
    if status in ("PENDING", "SEARCHING", "OFFERED", "SCHEDULED", "ASSIGNED"):
        offered = connection.execute(
            "SELECT 1 FROM provider_services WHERE provider_id=? AND service_id=?",
            (provider_id, request_row["service_id"]),
        ).fetchone()
        if offered:
            service_row = connection.execute(
                "SELECT * FROM services WHERE id=?", (request_row["service_id"],)
            ).fetchone()
            allowed, _reason, _req = engine.provider_eligibility(connection, provider_id, service_row)
            if allowed:
                return True, "candidate"
    return False, None


def _save_upload(file_storage, prefix):
    """Validate and store an image upload. Returns 'uploads/<name>'."""
    if file_storage is None or not file_storage.filename:
        return None
    filename = file_storage.filename
    if "." not in filename:
        raise ValueError("Only JPG, PNG and WEBP images are allowed.")
    ext = filename.rsplit(".", 1)[1].lower()
    if ext not in ALLOWED_IMAGE_EXT:
        raise ValueError("Only JPG, JPEG, PNG and WEBP images are allowed.")

    stream = file_storage.stream
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(0)
    if size > MAX_UPLOAD_BYTES:
        raise ValueError("Images must be smaller than 5 MB.")

    try:
        from PIL import Image as PILImage

        probe = PILImage.open(stream)
        probe.verify()
        detected = (probe.format or "").lower()
        if detected not in ("jpeg", "png", "webp"):
            raise ValueError("Unsupported image format.")
        ext = {"jpeg": "jpg", "png": "png", "webp": "webp"}[detected]
    except ValueError:
        raise
    except ImportError:
        head = stream.read(16)
        if not (head.startswith(b"\xff\xd8") or head.startswith(b"\x89PNG") or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")):
            raise ValueError("The file is not a valid image.")
    except Exception:
        raise ValueError("The uploaded file could not be read as an image.")
    finally:
        try:
            stream.seek(0)
        except Exception:
            pass

    folder = current_app.config.get("UPLOAD_FOLDER") or os.path.join(current_app.root_path, "static", "uploads")
    os.makedirs(folder, exist_ok=True)
    safe_name = f"v10_{prefix}_{uuid.uuid4().hex}.{ext}"
    stream.seek(0)
    file_storage.save(os.path.join(folder, safe_name))
    return "uploads/" + safe_name


def _upload_many(prefix, limit=4):
    saved = []
    files = request.files.getlist("media") or request.files.getlist("images")
    if request.files.get("image") and not files:
        files = [request.files.get("image")]
    for storage in files[:limit]:
        if not storage or not storage.filename:
            continue
        saved.append(_save_upload(storage, prefix))
    return saved


def _media_public_url(path):
    if not path:
        return None
    name = os.path.basename(str(path))
    folder = current_app.config.get("UPLOAD_FOLDER")
    if folder and os.path.isfile(os.path.join(folder, name)):
        return url_for("media_file", filename=name)
    return None


def _request_media_items(row):
    """Normalise the customer's uploaded problem media for a service_requests row."""
    try:
        keys = row.keys()
    except Exception:
        return []
    if "image_path" not in keys:
        return []
    items = []
    for path in engine.loads_list(row["image_path"]):
        if not path:
            continue
        url = _media_public_url(path)
        if url:
            items.append({"path": path, "url": url})
    return items


def _pincode_location(pincode):
    try:
        from app import _lookup_pincode_location
    except Exception:
        return {"latitude": None, "longitude": None, "label": f"PIN {pincode} service area", "pincode": pincode}
    try:
        return _lookup_pincode_location(pincode)
    except Exception:
        return {"latitude": None, "longitude": None, "label": f"PIN {pincode} service area", "pincode": pincode}


def _notify_socket(request_id, event="request_update"):
    try:
        from app import _emit_request_update
        _emit_request_update(request_id, event)
    except Exception:
        pass


def _log_request_event(connection, request_id, actor_user_id, event_type, message, metadata=None):
    try:
        connection.execute(
            "INSERT INTO service_events(request_id,actor_user_id,event_type,message,metadata_json) VALUES(?,?,?,?,?)",
            (request_id, actor_user_id, event_type, message, engine.dumps(metadata or {})),
        )
    except Exception as exc:
        print("[V10] event log warning:", repr(exc))


# =========================================================================
# PAGE — CUSTOMER HOME (mobile app shell)
# =========================================================================

@v10.route("/app")
@role_required("customer")
def customer_home():
    connection = get_db_connection()
    try:
        engine.sweep_completed_services(connection)
        user_id = current_user_id()
        user = connection.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        services = connection.execute(
            """
            SELECT * FROM services WHERE is_active IS NOT 0
            ORDER BY is_popular DESC, COALESCE(sort_order,100), name LIMIT 12
            """
        ).fetchall()
        categories = connection.execute(
            "SELECT * FROM service_categories WHERE is_active=1 ORDER BY sort_order LIMIT 10"
        ).fetchall()
        active_requests = connection.execute(
            """
            SELECT sr.*, s.name AS service_name, s.icon AS service_icon, u.name AS provider_name
            FROM service_requests sr
            JOIN services s ON s.id=sr.service_id
            LEFT JOIN providers p ON p.id=sr.provider_id
            LEFT JOIN users u ON u.id=p.user_id
            WHERE sr.customer_id=? AND sr.status NOT IN ('COMPLETED','CANCELLED','REJECTED','EXPIRED')
            ORDER BY sr.id DESC LIMIT 5
            """,
            (user_id,),
        ).fetchall()
        missions = engine.missions_for_customer(connection, user_id, status="ACTIVE", limit=4)
        assets = engine.list_assets(connection, user_id)[:6]
        warranties = connection.execute(
            """
            SELECT sr.id, sr.warranty_until, s.name AS service_name, sr.asset_id,
                   CAST(julianday(sr.warranty_until)-julianday('now') AS INTEGER) AS days_left
            FROM service_requests sr JOIN services s ON s.id=sr.service_id
            WHERE sr.customer_id=? AND sr.warranty_until IS NOT NULL
              AND sr.status='COMPLETED' AND datetime(sr.warranty_until) >= datetime('now')
            ORDER BY sr.warranty_until ASC LIMIT 5
            """,
            (user_id,),
        ).fetchall()
        recovery_open = connection.execute(
            "SELECT COUNT(*) c FROM service_recovery_cases WHERE customer_id=? AND status='OPEN'", (user_id,)
        ).fetchone()["c"]
        recommended = connection.execute(
            """
            SELECT p.id AS provider_id, u.name, u.profile_photo_path, p.rating, p.skills, p.experience,
                   p.provider_type, p.ekyc_status,
                   (SELECT COUNT(*) FROM reviews r WHERE r.provider_id=p.id) AS review_count
            FROM providers p JOIN users u ON u.id=p.user_id
            WHERE p.approved=1 ORDER BY p.rating DESC, p.experience DESC LIMIT 4
            """
        ).fetchall()
        recent = connection.execute(
            """
            SELECT sr.id, sr.status, sr.created_at, s.name AS service_name, s.icon AS service_icon,
                   sr.asset_id, sr.payment_status
            FROM service_requests sr JOIN services s ON s.id=sr.service_id
            WHERE sr.customer_id=? ORDER BY sr.id DESC LIMIT 5
            """,
            (user_id,),
        ).fetchall()
    finally:
        connection.close()

    return render_template(
        "v10/home.html",
        user=user,
        services=services,
        categories=categories,
        active_requests=active_requests,
        missions=missions,
        assets=assets,
        warranties=warranties,
        recovery_open=recovery_open,
        recommended=recommended,
        recent=recent,
        lifecycle=engine.LIFECYCLE_STAGES,
        lifecycle_labels=engine.LIFECYCLE_LABELS,
        media_url=_media_public_url,
    )


# =========================================================================
# PAGE — CATEGORIES / SEARCH
# =========================================================================

@v10.route("/categories")
def categories_page():
    connection = get_db_connection()
    try:
        categories = connection.execute(
            "SELECT * FROM service_categories WHERE is_active=1 ORDER BY sort_order"
        ).fetchall()
        grouped = {}
        for category in categories:
            services = connection.execute(
                """
                SELECT * FROM services WHERE is_active IS NOT 0 AND category=?
                ORDER BY is_popular DESC, COALESCE(sort_order,100), name
                """,
                (category["name"],),
            ).fetchall()
            grouped[category["key"]] = services
    finally:
        connection.close()
    return render_template("v10/categories.html", categories=categories, grouped=grouped)


@v10.route("/category/<key>")
def category_page(key):
    connection = get_db_connection()
    try:
        category = connection.execute("SELECT * FROM service_categories WHERE key=?", (key,)).fetchone()
        if not category:
            connection.close()
            abort(404)
        services = connection.execute(
            """
            SELECT * FROM services WHERE is_active IS NOT 0 AND category=?
            ORDER BY is_popular DESC, COALESCE(sort_order,100), name
            """,
            (category["name"],),
        ).fetchall()
        subcategories = {}
        for service in services:
            subcategories.setdefault(service["subcategory"] or "General", []).append(service)
    finally:
        connection.close()
    return render_template("v10/category.html", category=category, services=services, subcategories=subcategories)


@v10.route("/search")
def search_page():
    query = request.args.get("q", "").strip()
    results = None
    connection = get_db_connection()
    try:
        if query:
            results = engine.smart_search(connection, query)
        categories = connection.execute(
            "SELECT * FROM service_categories WHERE is_active=1 ORDER BY sort_order"
        ).fetchall()
    finally:
        connection.close()
    return render_template("v10/search.html", query=query, results=results, categories=categories)


@v10.route("/api/search")
def api_search():
    query = request.args.get("q", "").strip()
    if not query:
        return _ok({"results": {"services": [], "providers": [], "categories": [], "suggestions": []}})
    connection = get_db_connection()
    try:
        results = engine.smart_search(connection, query)
    finally:
        connection.close()
    return _ok({"results": results})


@v10.route("/api/service-categories")
def api_service_categories():
    connection = get_db_connection()
    try:
        categories = connection.execute(
            "SELECT * FROM service_categories WHERE is_active=1 ORDER BY sort_order"
        ).fetchall()
        payload = []
        for category in categories:
            services = connection.execute(
                """
                SELECT id,name,description,icon,subcategory,risk_level,estimated_duration,pricing_model,
                       min_price_hint,max_price_hint,required_equipment,possible_parts,is_popular
                FROM services WHERE is_active IS NOT 0 AND category=?
                ORDER BY is_popular DESC, COALESCE(sort_order,100), name
                """,
                (category["name"],),
            ).fetchall()
            entry = dict(category)
            entry["services"] = [
                {
                    **dict(service),
                    "required_equipment": engine.loads_list(service["required_equipment"]),
                    "possible_parts": engine.loads_list(service["possible_parts"]),
                }
                for service in services
            ]
            payload.append(entry)
    finally:
        connection.close()
    return _ok({"categories": payload, "count": len(payload)})


# =========================================================================
# MODULE 2 — BOOKING (dedicated page + stepper)
# =========================================================================

def _resolve_service(service_ref):
    connection = get_db_connection()
    try:
        if str(service_ref).isdigit():
            return connection.execute("SELECT * FROM services WHERE id=?", (int(service_ref),)).fetchone()
        row = connection.execute(
            "SELECT * FROM services WHERE LOWER(name)=LOWER(?) LIMIT 1", (str(service_ref).strip(),)
        ).fetchone()
        if row:
            return row
        slug = str(service_ref).replace("-", " ").strip()
        return connection.execute(
            "SELECT * FROM services WHERE LOWER(name)=LOWER(?) LIMIT 1", (slug,)
        ).fetchone()
    finally:
        connection.close()


@v10.route("/book/<service_ref>")
@role_required("customer")
def booking_page(service_ref):
    service = _resolve_service(service_ref)
    if not service:
        flash("That service is not available.")
        return redirect(url_for("v10.categories_page"))
    connection = get_db_connection()
    try:
        assets = engine.list_assets(connection, current_user_id())
        related = connection.execute(
            """
            SELECT id,name,icon,min_price_hint,max_price_hint,estimated_duration,is_popular
            FROM services WHERE category=? AND id<>? ORDER BY is_popular DESC, name LIMIT 6
            """,
            (service["category"], service["id"]),
        ).fetchall()
        asset_types = catalog.ASSET_TYPES
        address = connection.execute(
            "SELECT * FROM user_addresses WHERE user_id=? ORDER BY is_default DESC, id DESC LIMIT 4",
            (current_user_id(),),
        ).fetchall()
    finally:
        connection.close()
    service_payload = dict(service)
    service_payload["required_equipment"] = engine.loads_list(service["required_equipment"])
    service_payload["possible_parts"] = engine.loads_list(service["possible_parts"])
    service_payload["keywords"] = engine.loads_list(service["keywords"])
    service_payload["asset_types"] = engine.loads_list(service["asset_types"])
    return render_template(
        "v10/book.html",
        service=service_payload,
        related=related,
        assets=assets,
        asset_types=asset_types,
        addresses=address,
        steps=["Problem", "Asset", "Details", "Provider", "Time", "Price", "Confirm"],
    )


@v10.route("/book/<service_ref>/problem")
@role_required("customer")
def booking_problem_step(service_ref):
    """Compatibility route: the stepper lives on the booking page, anchored."""
    return redirect(url_for("v10.booking_page", service_ref=service_ref, step="problem"))


@v10.route("/api/ai/analyze-v10", methods=["POST"])
@role_required("customer")
def api_analyze_v10():
    """Preview AI problem understanding before a booking exists."""
    payload = request.get_json(silent=True) or request.form
    description = (payload.get("description") or "").strip()
    service_name = (payload.get("service_name") or "").strip() or None
    asset_id = payload.get("asset_id")
    if not description:
        return _fail("Describe the problem in your own words first.")
    connection = get_db_connection()
    try:
        asset = engine.get_asset(connection, int(asset_id)) if asset_id else None
        history = [dict(row) for row in engine.asset_history(connection, int(asset_id), limit=5)] if asset_id else []
        analysis = ai_layer.analyze_problem(
            service_name, description, asset=dict(asset) if asset else {}, history=history
        )
        plan = ai_layer.decompose_mission(description, service_name=service_name,
                                          asset=dict(asset) if asset else {}, analysis=analysis)
        parts = ai_layer.suggest_parts(service_name, description, analysis, dict(asset) if asset else {})
    finally:
        connection.close()
    return _ok({"analysis": analysis, "plan": plan, "parts": parts})


@v10.route("/api/v10/bookings", methods=["POST"])
@role_required("customer")
def api_create_booking():
    """Create a mission + its first service request in one step."""
    payload = request.get_json(silent=True) or {}
    form = request.form
    get = lambda key, default=None: (payload.get(key) if payload else None) or form.get(key) or default

    service_id = get("service_id")
    problem = (get("problem") or get("description") or "").strip()
    pincode = "".join(ch for ch in str(get("pincode") or "") if ch.isdigit())
    if not problem:
        return _fail("Tell SmartServe what the problem is.")
    if len(pincode) != 6:
        return _fail("A valid 6-digit PIN code is required to place the request.")

    service = _resolve_service(service_id) if service_id else None
    if not service:
        return _fail("Choose the service you need.")

    try:
        media = _upload_many("request", limit=4)
    except ValueError as exc:
        return _fail(str(exc))

    scheduled_at = (get("scheduled_at") or "").strip() or None
    budget = engine._to_float(get("budget"))
    address_text = (get("address_text") or "").strip() or None
    asset_id = get("asset_id")
    created_asset = None
    connection = get_db_connection()
    try:
        user_id = current_user_id()
        if not asset_id and get("new_asset_type"):
            created_asset = engine.create_asset(
                connection, user_id,
                {
                    "asset_type": get("new_asset_type"),
                    "brand": get("new_asset_brand"),
                    "model": get("new_asset_model"),
                    "serial_number": get("new_asset_serial"),
                    "purchase_date": get("new_asset_purchase_date"),
                    "warranty_end": get("new_asset_warranty_end"),
                    "nickname": get("new_asset_nickname"),
                    "location_label": get("address_label"),
                },
                actor_user_id=user_id,
            )
            asset_id = created_asset
        asset_id = int(asset_id) if asset_id else None
        if asset_id:
            owned = engine.get_asset(connection, asset_id)
            if not owned or int(owned["customer_id"]) != int(user_id):
                return _fail("That Service Passport does not belong to your account.", 403)

        pin = _pincode_location(pincode)
        connection.execute(
            """
            UPDATE users SET latitude=?, longitude=?, location_accuracy=1000, location_updated_at=CURRENT_TIMESTAMP,
                pincode=?, location_source='PINCODE' WHERE id=?
            """,
            (pin.get("latitude"), pin.get("longitude"), pincode, user_id),
        )
        if address_text:
            existing = connection.execute(
                "SELECT id FROM user_addresses WHERE user_id=? AND address_text=? LIMIT 1", (user_id, address_text)
            ).fetchone()
            if not existing:
                has_default = connection.execute(
                    "SELECT COUNT(*) c FROM user_addresses WHERE user_id=?", (user_id,)
                ).fetchone()["c"]
                connection.execute(
                    "INSERT INTO user_addresses(user_id,label,address_text,pincode,latitude,longitude,is_default) VALUES(?,?,?,?,?,?,?)",
                    (user_id, get("address_label") or "Home", address_text, pincode,
                     pin.get("latitude"), pin.get("longitude"), 0 if has_default else 1),
                )

        mission_id, mission_code, analysis, plan = engine.create_mission(
            connection,
            customer_id=user_id,
            problem=problem,
            service_name=service["name"],
            service_id=service["id"],
            asset_id=asset_id,
            description=get("description"),
            media=media,
            address_text=address_text,
            pincode=pincode,
            latitude=pin.get("latitude"),
            longitude=pin.get("longitude"),
            preferred_time=get("preferred_time"),
            scheduled_at=scheduled_at,
            budget=budget,
            created_by_user_id=user_id,
        )

        tasks = connection.execute(
            "SELECT * FROM mission_tasks WHERE mission_id=? ORDER BY task_order", (mission_id,)
        ).fetchall()
        primary = tasks[0] if tasks else None
        initial_status = "SCHEDULED" if scheduled_at else "PENDING"
        connection.execute(
            """
            INSERT INTO service_requests(
                customer_id, service_id, description, image_path, ai_analysis, estimated_price, status,
                ai_problem, ai_possible_cause, ai_difficulty, ai_recommended_service, ai_safety_note,
                scheduled_at, customer_pincode, customer_latitude, customer_longitude,
                customer_location_accuracy, customer_location_source, asset_id, mission_id, mission_task_id,
                lifecycle_stage, ai_structured_json, budget_customer, address_text, booking_mode,
                provider_diagnosis, second_opinion_status
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                user_id, service["id"], problem, media[0] if media else None,
                analysis.get("problem"), _price_text(analysis), initial_status,
                analysis.get("problem"), ", ".join(analysis.get("possible_causes") or [])[:1000],
                analysis.get("difficulty"), ", ".join(analysis.get("required_services") or [])[:500],
                ", ".join(analysis.get("safety_notes") or [])[:500],
                scheduled_at, pincode, pin.get("latitude"), pin.get("longitude"),
                1000, "PINCODE", asset_id, mission_id, primary["id"] if primary else None,
                "MATCH", engine.dumps(analysis), budget, address_text,
                "SCHEDULED" if scheduled_at else "ON_DEMAND", None, "NONE",
            ),
        )
        request_id = connection.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        if primary:
            engine.attach_request_to_task(connection, int(primary["id"]), request_id)
        engine.suggest_parts_for_request(connection, request_id)
        engine.refresh_mission_state(connection, mission_id)
        _log_request_event(connection, request_id, user_id, "MISSION_CREATED",
                           f"Mission {mission_code} created with {len(tasks)} task(s).",
                           {"mission_id": mission_id, "mission_code": mission_code})
        connection.commit()
    except Exception as exc:
        connection.rollback()
        print("[V10 booking] error:", repr(exc))
        return _fail("The booking could not be created. Please try again.")
    finally:
        connection.close()

    _notify_socket(request_id, "mission_created")
    return _ok({
        "request_id": request_id,
        "mission_id": mission_id,
        "mission_code": mission_code,
        "redirect": url_for("v10.booking_detail", request_id=request_id),
        "analysis": analysis,
        "plan": plan,
        "created_asset_id": created_asset,
    })


def _price_text(analysis):
    price = (analysis or {}).get("estimated_price_range") or {}
    if price.get("min") and price.get("max"):
        return f"₹{int(price['min']):,}–₹{int(price['max']):,}"
    return None


# =========================================================================
# PAGE — BOOKINGS
# =========================================================================

def _bookings_query(tab):
    base = """
        SELECT sr.*, s.name AS service_name, s.icon AS service_icon,
               cu.name AS provider_name, cu.profile_photo_path AS provider_photo,
               cu.phone AS provider_phone, p.rating AS provider_rating,
               a.asset_type, a.brand, a.model, a.asset_uid,
               m.mission_code, m.title AS mission_title, m.id AS mission_id,
               rc.id AS recovery_case_id, rc.case_code
        FROM service_requests sr
        JOIN services s ON s.id=sr.service_id
        LEFT JOIN providers p ON p.id=sr.provider_id
        LEFT JOIN users cu ON cu.id=p.user_id
        LEFT JOIN assets a ON a.id=sr.asset_id
        LEFT JOIN service_missions m ON m.id=sr.mission_id
        LEFT JOIN service_recovery_cases rc ON rc.original_request_id=sr.id AND rc.status='OPEN'
        WHERE sr.customer_id=?
    """
    filters = {
        "upcoming": "AND sr.status IN ('SCHEDULED','PENDING','SEARCHING','OFFERED','ASSIGNED')",
        "active": "AND sr.status IN ('ACCEPTED','ARRIVED','IN_PROGRESS','AWAITING_VERIFICATION','AWAITING_PAYMENT')",
        "completed": "AND sr.status='COMPLETED'",
        "cancelled": "AND sr.status IN ('CANCELLED','REJECTED','EXPIRED')",
        "recovery": "AND rc.id IS NOT NULL",
    }
    clause = filters.get(tab, filters["upcoming"])
    return base + clause + " ORDER BY sr.id DESC LIMIT 60"


def _booking_counts(connection, customer_id):
    row = connection.execute(
        """
        SELECT
          SUM(CASE WHEN sr.status IN ('SCHEDULED','PENDING','SEARCHING','OFFERED','ASSIGNED') THEN 1 ELSE 0 END) upcoming,
          SUM(CASE WHEN sr.status IN ('ACCEPTED','ARRIVED','IN_PROGRESS','AWAITING_VERIFICATION','AWAITING_PAYMENT') THEN 1 ELSE 0 END) active,
          SUM(CASE WHEN sr.status='COMPLETED' THEN 1 ELSE 0 END) completed,
          SUM(CASE WHEN sr.status IN ('CANCELLED','REJECTED','EXPIRED') THEN 1 ELSE 0 END) cancelled,
          SUM(CASE WHEN EXISTS (SELECT 1 FROM service_recovery_cases rc
                     WHERE rc.original_request_id=sr.id AND rc.status='OPEN') THEN 1 ELSE 0 END) recovery
        FROM service_requests sr WHERE sr.customer_id=?
        """,
        (customer_id,),
    ).fetchone()
    return {key: int(row[key] or 0) for key in ("upcoming", "active", "completed", "cancelled", "recovery")}


@v10.route("/bookings")
@role_required("customer")
def bookings_page():
    tab = (request.args.get("tab") or "upcoming").lower()
    if tab not in {"upcoming", "active", "completed", "cancelled", "recovery"}:
        tab = "upcoming"
    connection = get_db_connection()
    try:
        engine.sweep_completed_services(connection)
        rows = connection.execute(_bookings_query(tab), (current_user_id(),)).fetchall()
        counts = _booking_counts(connection, current_user_id())
    finally:
        connection.close()
    return render_template("v10/bookings.html", tab=tab, bookings=rows, counts=counts,
                           media_url=_media_public_url)


@v10.route("/booking/<int:request_id>")
@login_required
def booking_detail(request_id):
    """Confirmation + live lifecycle view for one booking."""
    connection = get_db_connection()
    try:
        engine.sweep_completed_services(connection)
        row = connection.execute(
            """
            SELECT sr.*, s.name AS service_name, s.icon AS service_icon, s.estimated_duration,
                   cu.name AS customer_name, cu.profile_photo_path AS customer_photo, cu.phone AS customer_phone,
                   pu.name AS provider_name, pu.profile_photo_path AS provider_photo, pu.phone AS provider_phone,
                   p.rating AS provider_rating, p.experience AS provider_experience, p.ekyc_status,
                   a.asset_uid, a.asset_type, a.brand, a.model, a.id AS asset_row_id,
                   m.mission_code, m.id AS mission_row_id, m.title AS mission_title, m.lifecycle_stage AS mission_stage
            FROM service_requests sr
            JOIN services s ON s.id=sr.service_id
            JOIN users cu ON cu.id=sr.customer_id
            LEFT JOIN providers p ON p.id=sr.provider_id
            LEFT JOIN users pu ON pu.id=p.user_id
            LEFT JOIN assets a ON a.id=sr.asset_id
            LEFT JOIN service_missions m ON m.id=sr.mission_id
            WHERE sr.id=?
            """,
            (request_id,),
        ).fetchone()
        if not row:
            connection.close()
            abort(404)
        user_id = int(current_user_id())
        is_customer = int(row["customer_id"]) == user_id
        provider = provider_for_user(connection, user_id)
        is_provider = bool(provider and row["provider_id"] and int(row["provider_id"]) == int(provider["id"]))
        if not (is_customer or is_provider or current_role() == "admin"):
            connection.close()
            abort(403)
        timeline = engine.black_box_timeline(connection, request_id)
        parts = engine.parts_for_request(connection, request_id)
        opinion = engine.second_opinion_for_request(connection, request_id)
        warranty = connection.execute(
            "SELECT * FROM service_warranties WHERE request_id=?", (request_id,)
        ).fetchone()
        certificate = connection.execute(
            "SELECT * FROM service_certificates WHERE request_id=? ORDER BY id DESC LIMIT 1", (request_id,)
        ).fetchone()
        mission_tasks = engine.mission_tasks(connection, int(row["mission_row_id"])) if row["mission_row_id"] else []
        mission_progress = engine.mission_progress(connection, int(row["mission_row_id"])) if row["mission_row_id"] else None
        outcome = connection.execute(
            "SELECT * FROM service_outcomes WHERE request_id=?", (request_id,)
        ).fetchone()
        messages = connection.execute(
            """
            SELECT m.*, u.name AS sender_name FROM messages m JOIN users u ON u.id=m.sender_id
            WHERE m.request_id=? ORDER BY datetime(m.created_at) LIMIT 50
            """,
            (request_id,),
        ).fetchall()
    finally:
        connection.close()

    return render_template(
        "v10/booking.html",
        r=row,
        timeline=timeline,
        parts=parts,
        parts_summary=engine.parts_summary(parts),
        opinion=opinion,
        warranty=warranty,
        certificate=certificate,
        mission_tasks=mission_tasks,
        mission_progress=mission_progress,
        outcome=outcome,
        messages=messages,
        media_items=_request_media_items(row),
        is_customer=is_customer,
        is_provider=is_provider,
        media_url=_media_public_url,
        lifecycle=engine.LIFECYCLE_STAGES,
        lifecycle_labels=engine.LIFECYCLE_LABELS,
    )


# =========================================================================
# MODULE 2 — MISSIONS
# =========================================================================

@v10.route("/missions")
@role_required("customer")
def missions_page():
    tab = (request.args.get("tab") or "active").lower()
    connection = get_db_connection()
    try:
        engine.sweep_completed_services(connection)
        missions = engine.missions_for_customer(
            connection, current_user_id(), status="COMPLETED" if tab == "completed" else "ACTIVE"
        )
        payload = []
        for mission in missions:
            item = dict(mission)
            item["progress"] = engine.mission_progress(connection, int(mission["id"]))
            payload.append(item)
    finally:
        connection.close()
    return render_template("v10/missions.html", tab=tab, missions=payload)


@v10.route("/missions/<int:mission_id>")
@role_required("customer")
def mission_detail(mission_id):
    connection = get_db_connection()
    try:
        mission = engine.get_mission(connection, mission_id)
        if not mission or int(mission["customer_id"]) != int(current_user_id()):
            connection.close()
            abort(404)
        tasks = engine.mission_tasks(connection, mission_id)
        progress = engine.mission_progress(connection, mission_id)
        asset = engine.get_asset(connection, mission["asset_id"]) if mission["asset_id"] else None
        analysis = engine.loads(mission["ai_analysis_json"], {})
        required = engine.loads_list(mission["required_categories"])
        safety = engine.loads_list(mission["safety_notes"])
        media_items = [{"path": path, "url": _media_public_url(path)} for path in engine.loads_list(mission["media_json"])]
        evidence = connection.execute(
            "SELECT * FROM service_evidence WHERE mission_id=? ORDER BY datetime(created_at) DESC LIMIT 60",
            (mission_id,),
        ).fetchall()
        recovery_cases = connection.execute(
            """
            SELECT rc.*, s.name AS service_name FROM service_recovery_cases rc
            LEFT JOIN service_requests sr ON sr.id=rc.original_request_id
            LEFT JOIN services s ON s.id=sr.service_id
            WHERE rc.mission_id=? OR rc.asset_id=? ORDER BY rc.id DESC LIMIT 10
            """,
            (mission_id, mission["asset_id"]),
        ).fetchall()
    finally:
        connection.close()
    return render_template(
        "v10/mission.html",
        mission=mission,
        tasks=tasks,
        progress=progress,
        asset=asset,
        analysis=analysis,
        required=required,
        safety=safety,
        media_items=media_items,
        evidence=evidence,
        recovery_cases=recovery_cases,
        media_url=_media_public_url,
        lifecycle=engine.LIFECYCLE_STAGES,
        lifecycle_labels=engine.LIFECYCLE_LABELS,
    )


@v10.route("/api/missions", methods=["POST"])
@role_required("customer")
def api_create_mission():
    payload = request.get_json(silent=True) or {}
    form = request.form
    get = lambda key, default=None: (payload.get(key) if payload else None) or form.get(key) or default
    problem = (get("problem") or "").strip()
    if not problem:
        return _fail("Describe the problem for the mission.")
    service_name = (get("service_name") or "").strip() or None
    asset_id = get("asset_id")
    try:
        media = _upload_many("mission", limit=4)
    except ValueError as exc:
        return _fail(str(exc))
    connection = get_db_connection()
    try:
        if asset_id:
            asset = engine.get_asset(connection, int(asset_id))
            if not asset or int(asset["customer_id"]) != int(current_user_id()):
                return _fail("That asset does not belong to your account.", 403)
        mission_id, code, analysis, plan = engine.create_mission(
            connection,
            customer_id=current_user_id(),
            problem=problem,
            service_name=service_name,
            asset_id=int(asset_id) if asset_id else None,
            description=get("description"),
            media=media,
            pincode=get("pincode"),
            address_text=get("address_text"),
            preferred_time=get("preferred_time"),
            budget=engine._to_float(get("budget")),
            created_by_user_id=current_user_id(),
        )
        connection.commit()
    finally:
        connection.close()
    return _ok({"mission_id": mission_id, "mission_code": code, "analysis": analysis, "plan": plan,
                "redirect": url_for("v10.mission_detail", mission_id=mission_id)})


@v10.route("/api/missions/<int:mission_id>")
@login_required
def api_mission(mission_id):
    connection = get_db_connection()
    try:
        mission = engine.get_mission(connection, mission_id)
        if not mission:
            connection.close()
            return _fail("Mission not found.", 404)
        if int(mission["customer_id"]) != int(current_user_id()) and current_role() != "admin":
            connection.close()
            return _fail("You cannot view this mission.", 403)
        payload = dict(mission)
        payload["tasks"] = [dict(row) for row in engine.mission_tasks(connection, mission_id)]
        payload["progress"] = engine.mission_progress(connection, mission_id)
        payload["analysis"] = engine.loads(mission["ai_analysis_json"], {})
    finally:
        connection.close()
    return _ok({"mission": payload})


@v10.route("/api/missions/<int:mission_id>/tasks")
@login_required
def api_mission_tasks(mission_id):
    connection = get_db_connection()
    try:
        mission = engine.get_mission(connection, mission_id)
        if not mission:
            connection.close()
            return _fail("Mission not found.", 404)
        if int(mission["customer_id"]) != int(current_user_id()) and current_role() != "admin":
            connection.close()
            return _fail("You cannot view these tasks.", 403)
        tasks = [dict(row) for row in engine.mission_tasks(connection, mission_id)]
        progress = engine.mission_progress(connection, mission_id)
    finally:
        connection.close()
    return _ok({"tasks": tasks, "progress": progress})


@v10.route("/api/missions/<int:mission_id>/tasks/<int:task_id>/book", methods=["POST"])
@role_required("customer")
def api_book_mission_task(mission_id, task_id):
    """Start a SmartServe booking for a specific mission task (coordinated trades)."""
    payload = request.get_json(silent=True) or {}
    pincode = "".join(ch for ch in str(payload.get("pincode") or "") if ch.isdigit())
    connection = get_db_connection()
    try:
        mission = engine.get_mission(connection, mission_id)
        if not mission or int(mission["customer_id"]) != int(current_user_id()):
            return _fail("Mission not found.", 404)
        task = connection.execute(
            "SELECT * FROM mission_tasks WHERE id=? AND mission_id=?", (task_id, mission_id)
        ).fetchone()
        if not task:
            return _fail("Mission task not found.", 404)
        if task["request_id"]:
            return _ok({"request_id": task["request_id"], "existing": True,
                        "redirect": url_for("v10.booking_detail", request_id=task["request_id"])})
        if task["status"] == "BLOCKED":
            return _fail("This task is waiting for an earlier task to be completed first.")
        service_row = None
        if task["service_id"]:
            service_row = connection.execute("SELECT * FROM services WHERE id=?", (task["service_id"],)).fetchone()
        if not service_row and task["service_name"]:
            service_row = connection.execute(
                "SELECT * FROM services WHERE LOWER(name)=LOWER(?) LIMIT 1", (task["service_name"],)
            ).fetchone()
        if not service_row:
            service_row = connection.execute("SELECT * FROM services WHERE id=1").fetchone()

        analysis = engine.loads(mission["ai_analysis_json"], {})
        pincode = pincode or (mission["pincode"] or "")
        pin = _pincode_location(pincode) if pincode else {"latitude": None, "longitude": None}
        connection.execute(
            """
            INSERT INTO service_requests(
                customer_id, service_id, description, status, ai_analysis, estimated_price,
                ai_problem, ai_possible_cause, ai_recommended_service, ai_safety_note,
                customer_pincode, customer_latitude, customer_longitude, customer_location_source,
                asset_id, mission_id, mission_task_id, lifecycle_stage, ai_structured_json,
                address_text, booking_mode
            ) VALUES(?,?,?,'PENDING',?,?,?,?,?,?,?,?,?,'PINCODE',?,?,?,'MATCH',?,?,'ON_DEMAND')
            """,
            (
                current_user_id(), service_row["id"], task["description"] or task["title"],
                analysis.get("problem"), _price_text(analysis), analysis.get("problem"),
                ", ".join(analysis.get("possible_causes") or []), task["service_name"],
                ", ".join(analysis.get("safety_notes") or []), pincode,
                pin.get("latitude"), pin.get("longitude"), mission["asset_id"], mission_id, task["id"],
                engine.dumps(analysis), mission["address_text"],
            ),
        )
        request_id = connection.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        engine.attach_request_to_task(connection, task_id, request_id)
        engine.suggest_parts_for_request(connection, request_id)
        engine.refresh_mission_state(connection, mission_id)
        connection.commit()
    except Exception as exc:
        connection.rollback()
        print("[V10 task booking] error:", repr(exc))
        return _fail("The task booking could not be created.")
    finally:
        connection.close()
    return _ok({"request_id": request_id, "redirect": url_for("v10.booking_detail", request_id=request_id)})


# =========================================================================
# MODULE 1 — SERVICE PASSPORT / ASSETS / QR
# =========================================================================

@v10.route("/passports")
@role_required("customer")
def passports_page():
    connection = get_db_connection()
    try:
        assets = engine.list_assets(connection, current_user_id())
        payload = []
        for asset in assets:
            item = dict(asset)
            item["warranty"] = engine.warranty_state(asset)
            item["last_service"] = connection.execute(
                "SELECT service_name, service_date FROM asset_service_history WHERE asset_id=? ORDER BY datetime(service_date) DESC LIMIT 1",
                (asset["id"],),
            ).fetchone()
            item["upcoming"] = connection.execute(
                "SELECT title, due_date FROM asset_maintenance_schedule WHERE asset_id=? AND status='UPCOMING' ORDER BY due_date LIMIT 1",
                (asset["id"],),
            ).fetchone()
            payload.append(item)
    finally:
        connection.close()
    return render_template("v10/assets.html", assets=payload, asset_types=catalog.ASSET_TYPES)


@v10.route("/passport/<int:asset_id>")
@login_required
def passport_page(asset_id):
    connection = get_db_connection()
    try:
        asset = engine.get_asset(connection, asset_id)
        if not asset:
            connection.close()
            abort(404)
        user_id = int(current_user_id())
        provider = provider_for_user(connection, user_id)
        is_owner = int(asset["customer_id"]) == user_id
        allowed_provider = False
        if provider and not is_owner:
            allowed_provider = bool(connection.execute(
                """
                SELECT 1 FROM service_requests
                WHERE asset_id=? AND provider_id=? LIMIT 1
                """,
                (asset_id, provider["id"]),
            ).fetchone())
        if not (is_owner or allowed_provider or current_role() == "admin"):
            connection.close()
            abort(403)
        data = engine.passport(connection, asset)
        qr = engine.active_qr_token(connection, asset_id)
        qr_url = url_for("v10.public_passport", token=qr["token"], _external=True) if qr else None
        connection.commit()
    finally:
        connection.close()
    return render_template(
        "v10/asset.html", asset=asset, passport=data, qr=qr, qr_url=qr_url,
        is_owner=is_owner, media_url=_media_public_url,
    )


@v10.route("/asset/<token>")
def public_passport(token):
    """QR target: token-only public passport (no private customer data)."""
    connection = get_db_connection()
    try:
        asset = engine.resolve_public_token(connection, token)
        if not asset:
            connection.commit()
            return render_template("v10/asset_public.html", asset=None, token=token), 404
        data = engine.passport(connection, asset, include_public_only=True)
        connection.commit()
    finally:
        connection.close()
    is_owner = bool(current_user_id() and int(current_user_id()) == int(asset["customer_id"]))
    return render_template(
        "v10/asset_public.html", asset=asset, passport=data, token=token, is_owner=is_owner,
        media_url=_media_public_url,
    )


@v10.route("/passport/<int:asset_id>/print")
@login_required
def passport_print(asset_id):
    connection = get_db_connection()
    try:
        asset = engine.get_asset(connection, asset_id)
        if not asset or (int(asset["customer_id"]) != int(current_user_id()) and current_role() != "admin"):
            connection.close()
            abort(403)
        data = engine.passport(connection, asset, include_public_only=True)
        qr = engine.active_qr_token(connection, asset_id)
        connection.commit()
    finally:
        connection.close()
    return render_template("v10/asset_print.html", asset=asset, passport=data, qr=qr,
                           qr_url=url_for("v10.public_passport", token=qr["token"], _external=True) if qr else None)


def _qr_png(data_text, box_size=10, border=2):
    import qrcode  # imported lazily so the app still runs without the package

    qr = qrcode.QRCode(version=None, box_size=box_size, border=border,
                       error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(data_text)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


@v10.route("/api/assets", methods=["GET", "POST"])
@login_required
def api_assets():
    connection = get_db_connection()
    try:
        if request.method == "GET":
            if current_role() != "customer":
                return _fail("Only customers have Service Passports.", 403)
            assets = engine.list_assets(connection, current_user_id())
            payload = []
            for asset in assets:
                item = dict(asset)
                item["warranty"] = engine.warranty_state(asset)
                item["service_count"] = connection.execute(
                    "SELECT COUNT(*) c FROM asset_service_history WHERE asset_id=?", (asset["id"],)
                ).fetchone()["c"]
                payload.append(item)
            return _ok({"assets": payload, "count": len(payload)})

        if current_role() != "customer":
            return _fail("Only customers can register assets.", 403)
        payload = request.get_json(silent=True) or request.form
        if not (payload.get("asset_type") or "").strip():
            return _fail("Choose the asset type.")
        photo = None
        try:
            photo = _upload_many("asset", limit=1)
        except ValueError as exc:
            return _fail(str(exc))
        asset_id = engine.create_asset(
            connection, current_user_id(),
            {
                "asset_type": payload.get("asset_type"),
                "category": payload.get("category"),
                "subcategory": payload.get("subcategory"),
                "nickname": payload.get("nickname"),
                "brand": payload.get("brand"),
                "model": payload.get("model"),
                "serial_number": payload.get("serial_number"),
                "purchase_date": payload.get("purchase_date"),
                "installation_date": payload.get("installation_date"),
                "warranty_start": payload.get("warranty_start"),
                "warranty_end": payload.get("warranty_end"),
                "warranty_provider": payload.get("warranty_provider"),
                "vendor_name": payload.get("vendor_name"),
                "location_label": payload.get("location_label"),
                "notes": payload.get("notes"),
                "photo_path": photo[0] if photo else None,
            },
            actor_user_id=current_user_id(),
        )
        connection.commit()
        asset = engine.get_asset(connection, asset_id)
    finally:
        connection.close()
    return _ok({"asset": dict(asset), "asset_id": asset_id,
                "passport_url": url_for("v10.passport_page", asset_id=asset_id)})


@v10.route("/api/assets/<int:asset_id>", methods=["GET", "PUT", "PATCH"])
@login_required
def api_asset_detail(asset_id):
    connection = get_db_connection()
    try:
        asset = engine.get_asset(connection, asset_id)
        if not asset:
            return _fail("Asset not found.", 404)
        if int(asset["customer_id"]) != int(current_user_id()) and current_role() != "admin":
            return _fail("You can only manage your own Service Passports.", 403)
        if request.method == "GET":
            return _ok({"asset": dict(asset), "passport": engine.passport(connection, asset)})

        payload = request.get_json(silent=True) or request.form
        editable = [
            "nickname", "brand", "model", "serial_number", "purchase_date", "installation_date",
            "warranty_start", "warranty_end", "warranty_provider", "vendor_name", "location_label",
            "notes", "asset_type", "category", "subcategory", "status",
        ]
        updates = {key: payload.get(key) for key in editable if key in payload}
        if "health_score" in payload:
            try:
                updates["health_score"] = max(0, min(100, int(payload.get("health_score"))))
            except (TypeError, ValueError):
                pass
        if not updates:
            return _fail("Nothing to update.")
        assignments = ", ".join(f"{key}=?" for key in updates)
        connection.execute(
            f"UPDATE assets SET {assignments}, updated_at=? WHERE id=?",
            (*updates.values(), engine.now_str(), asset_id),
        )
        connection.commit()
        return _ok({"asset": dict(engine.get_asset(connection, asset_id))})
    finally:
        connection.close()


@v10.route("/api/assets/<int:asset_id>/passport")
@login_required
def api_asset_passport(asset_id):
    connection = get_db_connection()
    try:
        asset = engine.get_asset(connection, asset_id)
        if not asset:
            return _fail("Asset not found.", 404)
        user_id = int(current_user_id())
        provider = provider_for_user(connection, user_id)
        authorized = int(asset["customer_id"]) == user_id or current_role() == "admin"
        if not authorized and provider:
            authorized = bool(connection.execute(
                "SELECT 1 FROM service_requests WHERE asset_id=? AND provider_id=? LIMIT 1",
                (asset_id, provider["id"]),
            ).fetchone())
        if not authorized:
            return _fail("You cannot view this Service Passport.", 403)
        public_only = int(asset["customer_id"]) != user_id
        return _ok({"passport": engine.passport(connection, asset, include_public_only=public_only)})
    finally:
        connection.close()


@v10.route("/api/assets/<int:asset_id>/history")
@login_required
def api_asset_history(asset_id):
    connection = get_db_connection()
    try:
        asset = engine.get_asset(connection, asset_id)
        if not asset:
            return _fail("Asset not found.", 404)
        if int(asset["customer_id"]) != int(current_user_id()) and current_role() != "admin":
            provider = provider_for_user(connection, current_user_id())
            assigned = provider and connection.execute(
                "SELECT 1 FROM service_requests WHERE asset_id=? AND provider_id=? LIMIT 1",
                (asset_id, provider["id"]),
            ).fetchone()
            if not assigned:
                return _fail("You cannot view this history.", 403)
        return _ok({
            "history": [dict(row) for row in engine.asset_history(connection, asset_id)],
            "maintenance": [dict(row) for row in engine.maintenance_plan(connection, asset_id)],
            "certificates": [dict(row) for row in engine.asset_certificates(connection, asset_id)],
        })
    finally:
        connection.close()


@v10.route("/api/assets/<int:asset_id>/qr", methods=["GET", "POST"])
@login_required
def api_asset_qr(asset_id):
    connection = get_db_connection()
    try:
        asset = engine.get_asset(connection, asset_id)
        if not asset:
            return _fail("Asset not found.", 404)
        if int(asset["customer_id"]) != int(current_user_id()) and current_role() != "admin":
            return _fail("You can only manage QR codes for your own assets.", 403)
        if request.method == "POST":
            rotate = bool((request.get_json(silent=True) or {}).get("rotate"))
            token_row = engine.issue_qr_token(connection, asset_id, revoke_existing=rotate)
            connection.commit()
            return _ok({"token": token_row["token"], "rotated": rotate,
                        "passport_url": url_for("v10.public_passport", token=token_row["token"], _external=True)})

        token_row = engine.active_qr_token(connection, asset_id)
        connection.commit()
        if not token_row:
            return _fail("No QR token exists for this asset.", 404)
        passport_url = url_for("v10.public_passport", token=token_row["token"], _external=True)
        if request.args.get("format") == "json":
            return _ok({"token": token_row["token"], "passport_url": passport_url,
                        "scan_count": token_row["scan_count"], "png": url_for("v10.api_asset_qr_png", asset_id=asset_id)})
        return _qr_response(passport_url, asset, token_row)
    finally:
        connection.close()


def _qr_response(passport_url, asset, token_row):
    download = request.args.get("download") == "1"
    try:
        buffer = _qr_png(passport_url)
    except Exception as exc:
        print("[V10 QR] generation failed:", repr(exc))
        return _fail("QR generation is unavailable on this deployment (qrcode package missing).", 503)
    response = send_file(buffer, mimetype="image/png", download_name=f"{asset['asset_uid']}-qr.png",
                         as_attachment=download, max_age=0)
    response.headers["Cache-Control"] = "no-store"
    return response


@v10.route("/api/assets/<int:asset_id>/qr.png")
@login_required
def api_asset_qr_png(asset_id):
    connection = get_db_connection()
    try:
        asset = engine.get_asset(connection, asset_id)
        if not asset:
            abort(404)
        if int(asset["customer_id"]) != int(current_user_id()) and current_role() != "admin":
            abort(403)
        token_row = engine.active_qr_token(connection, asset_id)
        connection.commit()
    finally:
        connection.close()
    passport_url = url_for("v10.public_passport", token=token_row["token"], _external=True)
    return _qr_response(passport_url, asset, token_row)


@v10.route("/api/assets/<int:asset_id>/maintenance/<int:plan_id>", methods=["POST"])
@login_required
def api_complete_maintenance(asset_id, plan_id):
    connection = get_db_connection()
    try:
        asset = engine.get_asset(connection, asset_id)
        if not asset or int(asset["customer_id"]) != int(current_user_id()):
            return _fail("Asset not found.", 404)
        payload = request.get_json(silent=True) or {}
        status = (payload.get("status") or "COMPLETED").upper()
        connection.execute(
            "UPDATE asset_maintenance_schedule SET status=?, completed_at=CASE WHEN ?='COMPLETED' THEN ? ELSE completed_at END WHERE id=? AND asset_id=?",
            (status, status, engine.now_str(), plan_id, asset_id),
        )
        if status == "COMPLETED":
            plan = connection.execute(
                "SELECT * FROM asset_maintenance_schedule WHERE id=?", (plan_id,)
            ).fetchone()
            if plan and plan["interval_days"]:
                due = (engine.now() + timedelta(days=int(plan["interval_days"]))).strftime("%Y-%m-%d")
                connection.execute(
                    """
                    INSERT INTO asset_maintenance_schedule(asset_id,title,description,due_date,interval_days,source)
                    VALUES(?,?,?,?,?,'SYSTEM')
                    """,
                    (asset_id, plan["title"], plan["description"], due, plan["interval_days"]),
                )
        connection.commit()
    finally:
        connection.close()
    return _ok()


# =========================================================================
# MODULE 3 — BLACK BOX / EVIDENCE / CERTIFICATES
# =========================================================================

@v10.route("/service/<int:request_id>/blackbox")
@login_required
def blackbox_page(request_id):
    connection = get_db_connection()
    try:
        engine.sweep_completed_services(connection)
        timeline = engine.black_box_timeline(connection, request_id)
        if not timeline:
            connection.close()
            abort(404)
        row = timeline["request"]
        user_id = int(current_user_id())
        provider = provider_for_user(connection, user_id)
        is_customer = int(row["customer_id"]) == user_id
        is_provider = bool(provider and row["provider_id"] and int(row["provider_id"]) == int(provider["id"]))
        if not (is_customer or is_provider or current_role() == "admin"):
            connection.close()
            abort(403)
        parts = engine.parts_for_request(connection, request_id)
        connection.commit()
    finally:
        connection.close()
    return render_template("v10/blackbox.html", timeline=timeline, r=row, parts=parts,
                           is_customer=is_customer, is_provider=is_provider, media_url=_media_public_url)


@v10.route("/api/service/<int:request_id>/blackbox")
@login_required
def api_blackbox(request_id):
    connection = get_db_connection()
    try:
        engine.sweep_completed_services(connection)
        timeline = engine.black_box_timeline(connection, request_id)
        if not timeline:
            return _fail("Service not found.", 404)
        row = timeline["request"]
        user_id = int(current_user_id())
        provider = provider_for_user(connection, user_id)
        allowed = (
            int(row["customer_id"]) == user_id
            or (provider and row["provider_id"] and int(row["provider_id"]) == int(provider["id"]))
            or current_role() == "admin"
        )
        if not allowed:
            return _fail("You cannot view this service record.", 403)
        connection.commit()
    finally:
        connection.close()
    return _ok({"timeline": timeline})


@v10.route("/api/services/<int:request_id>/evidence", methods=["POST", "GET"])
@login_required
def api_service_evidence(request_id):
    connection = get_db_connection()
    try:
        row = connection.execute("SELECT * FROM service_requests WHERE id=?", (request_id,)).fetchone()
        if not row:
            return _fail("Service not found.", 404)
        user_id = int(current_user_id())
        provider = provider_for_user(connection, user_id)
        is_customer = int(row["customer_id"]) == user_id
        is_provider = bool(provider and row["provider_id"] and int(row["provider_id"]) == int(provider["id"]))
        if not (is_customer or is_provider or current_role() == "admin"):
            return _fail("You cannot add evidence to this service.", 403)

        if request.method == "GET":
            rows = engine.evidence_for_request(connection, request_id)
            return _ok({"evidence": [dict(item) for item in rows]})

        payload = request.get_json(silent=True) or request.form
        stage = (payload.get("stage") or ("BEFORE" if is_provider else "CUSTOMER_NOTE")).upper()
        note = (payload.get("note") or "").strip() or None
        try:
            uploads = _upload_many("evidence", limit=4)
        except ValueError as exc:
            return _fail(str(exc))
        if not uploads and not note:
            return _fail("Add a note or a photo.")

        role = "provider" if is_provider else ("customer" if is_customer else "admin")
        created = []
        for path in uploads:
            created.append(engine.log_evidence(
                connection, request_id=request_id, asset_id=row["asset_id"], mission_id=row["mission_id"],
                task_id=row["mission_task_id"], stage=stage, kind="PHOTO", path=path,
                note=note, actor_user_id=user_id, actor_role=role,
            ))
        if note:
            created.append(engine.log_evidence(
                connection, request_id=request_id, asset_id=row["asset_id"], mission_id=row["mission_id"],
                task_id=row["mission_task_id"], stage=stage, kind="NOTE", note=note,
                actor_user_id=user_id, actor_role=role,
            ))
        if is_provider and stage in ("BEFORE", "BEFORE_EVIDENCE"):
            existing = engine.loads_list(row["before_evidence_paths"])
            existing.extend(uploads)
            connection.execute(
                "UPDATE service_requests SET before_evidence_paths=? WHERE id=?",
                (engine.dumps(existing), request_id),
            )
        if is_provider and stage in ("DIAGNOSIS",):
            if payload.get("diagnosis"):
                connection.execute(
                    "UPDATE service_requests SET provider_diagnosis=? WHERE id=?",
                    (str(payload.get("diagnosis"))[:2000], request_id),
                )
        if is_provider and stage in ("WORK", "WORK_PERFORMED") and payload.get("work"):
            connection.execute(
                "UPDATE service_requests SET provider_work_performed=? WHERE id=?",
                (str(payload.get("work"))[:2000], request_id),
            )
        engine.sync_task_from_request(connection, request_id)
        connection.commit()
    finally:
        connection.close()
    _notify_socket(request_id)
    return _ok({"evidence_ids": created, "count": len(created), "stage": stage})


@v10.route("/certificate/<int:request_id>")
@login_required
def certificate_page(request_id):
    connection = get_db_connection()
    try:
        row = connection.execute(
            """
            SELECT sr.*, s.name AS service_name, cu.name AS customer_name, pu.name AS provider_name,
                   a.asset_type, a.brand, a.model, a.asset_uid
            FROM service_requests sr JOIN services s ON s.id=sr.service_id
            JOIN users cu ON cu.id=sr.customer_id
            LEFT JOIN providers p ON p.id=sr.provider_id LEFT JOIN users pu ON pu.id=p.user_id
            LEFT JOIN assets a ON a.id=sr.asset_id WHERE sr.id=?
            """,
            (request_id,),
        ).fetchone()
        if not row:
            connection.close()
            abort(404)
        user_id = int(current_user_id())
        provider = provider_for_user(connection, user_id)
        allowed = (int(row["customer_id"]) == user_id
                   or (provider and row["provider_id"] and int(row["provider_id"]) == int(provider["id"]))
                   or current_role() == "admin")
        if not allowed:
            connection.close()
            abort(403)
        certificate = engine.issue_certificate(connection, request_id)
        if not certificate and row["status"] == "COMPLETED":
            certificate = engine.issue_certificate(connection, request_id, force=True)
        parts = engine.parts_for_request(connection, request_id)
        connection.commit()
    finally:
        connection.close()
    return render_template("v10/certificate.html", r=row, certificate=certificate, parts=parts,
                           media_url=_media_public_url)


@v10.route("/api/certificate/<code>")
def api_certificate(code):
    connection = get_db_connection()
    try:
        certificate = engine.certificate_by_code(connection, code)
        if not certificate:
            return _fail("Certificate not found.", 404)
    finally:
        connection.close()
    return _ok({"certificate": certificate})


# =========================================================================
# MODULE 4 — SECOND OPINION
# =========================================================================

@v10.route("/second-opinion/<int:request_id>")
@role_required("customer")
def second_opinion_page(request_id):
    connection = get_db_connection()
    try:
        row = connection.execute(
            """
            SELECT sr.*, s.name AS service_name, pu.name AS provider_name, a.asset_type, a.brand, a.model, a.asset_uid
            FROM service_requests sr JOIN services s ON s.id=sr.service_id
            LEFT JOIN providers p ON p.id=sr.provider_id LEFT JOIN users pu ON pu.id=p.user_id
            LEFT JOIN assets a ON a.id=sr.asset_id
            WHERE sr.id=? AND sr.customer_id=?
            """,
            (request_id, current_user_id()),
        ).fetchone()
        if not row:
            connection.close()
            abort(404)
        result = connection.execute(
            """
            SELECT r.*, o.proposed_repair, o.quoted_amount, o.provider_diagnosis, o.customer_question,
                   o.created_at AS requested_at, o.id AS opinion_request_id
            FROM second_opinion_requests o
            LEFT JOIN second_opinion_results r ON r.opinion_request_id=o.id
            WHERE o.request_id=? ORDER BY o.id DESC LIMIT 1
            """,
            (request_id,),
        ).fetchone()
        history = [dict(item) for item in engine.asset_history(connection, row["asset_id"], limit=6)] if row["asset_id"] else []
        parts = engine.parts_for_request(connection, request_id)
        connection.commit()
    finally:
        connection.close()
    parsed = None
    if result:
        parsed = dict(result)
        for key in ("supporting_evidence", "information_required", "alternatives", "safety_notes"):
            parsed[key] = engine.loads_list(parsed.get(key))
    return render_template(
        "v10/second_opinion.html", r=row, opinion=parsed, history=history, parts=parts,
        ai_live=ai_layer.gemini_available(),
    )


@v10.route("/api/second-opinion", methods=["POST"])
@role_required("customer")
def api_second_opinion():
    payload = request.get_json(silent=True) or request.form
    request_id = payload.get("request_id")
    if not request_id:
        return _fail("Which service should SmartServe review?")
    connection = get_db_connection()
    try:
        opinion_id, error = engine.create_second_opinion(
            connection,
            customer_id=current_user_id(),
            request_id=int(request_id),
            mission_id=payload.get("mission_id"),
            task_id=payload.get("task_id"),
            proposed_repair=(payload.get("proposed_repair") or "").strip() or None,
            quoted_amount=payload.get("quoted_amount"),
            provider_diagnosis=payload.get("provider_diagnosis"),
            question=(payload.get("question") or payload.get("customer_question") or "").strip() or None,
        )
        if error and not opinion_id:
            connection.rollback()
            return _fail(error, 404)
        result = engine.run_second_opinion(connection, opinion_id)
        connection.commit()
    except Exception as exc:
        connection.rollback()
        print("[V10 second opinion] error:", repr(exc))
        return _fail("The second opinion could not be generated right now.")
    finally:
        connection.close()
    return _ok({"opinion_request_id": opinion_id, "result": result})


@v10.route("/api/second-opinion/<int:request_id>")
@login_required
def api_second_opinion_get(request_id):
    connection = get_db_connection()
    try:
        row = connection.execute(
            "SELECT customer_id FROM service_requests WHERE id=?", (request_id,)
        ).fetchone()
        if row and int(row["customer_id"]) != int(current_user_id()) and current_role() != "admin":
            provider = provider_for_user(connection, current_user_id())
            assigned = provider and connection.execute(
                "SELECT 1 FROM service_requests WHERE id=? AND provider_id=?", (request_id, provider["id"])
            ).fetchone()
            if not assigned:
                return _fail("You cannot view this review.", 403)
        opinion = engine.second_opinion_for_request(connection, request_id)
    finally:
        connection.close()
    if not opinion:
        return _ok({"opinion": None})
    for key in ("supporting_evidence", "information_required", "alternatives", "safety_notes"):
        opinion[key] = engine.loads_list(opinion.get(key))
    return _ok({"opinion": opinion})


# =========================================================================
# MODULE 5 — PARTS
# =========================================================================

@v10.route("/api/service/<int:request_id>/parts", methods=["GET", "POST"])
@login_required
def api_service_parts(request_id):
    connection = get_db_connection()
    try:
        row = connection.execute("SELECT * FROM service_requests WHERE id=?", (request_id,)).fetchone()
        if not row:
            return _fail("Service not found.", 404)
        user_id = int(current_user_id())
        provider = provider_for_user(connection, user_id)
        is_customer = int(row["customer_id"]) == user_id
        is_provider = bool(provider and row["provider_id"] and int(row["provider_id"]) == int(provider["id"]))
        if not (is_customer or is_provider or current_role() == "admin"):
            return _fail("You cannot manage parts for this service.", 403)

        if request.method == "GET":
            parts = engine.parts_for_request(connection, request_id)
            if not parts and is_customer:
                parts = engine.suggest_parts_for_request(connection, request_id)
                connection.commit()
            return _ok({"parts": [dict(part) for part in parts], "summary": engine.parts_summary(parts)})

        payload = request.get_json(silent=True) or request.form
        if not (payload.get("part_name") or "").strip():
            return _fail("Enter the part name.")
        quantity = max(1, min(20, int(engine._to_float(payload.get("quantity")) or 1)))
        data_source = (payload.get("data_source") or ("PROVIDER" if is_provider else "ESTIMATE")).upper()
        connection.execute(
            """
            INSERT INTO service_parts(
                request_id, mission_id, task_id, asset_id, part_name, part_number, compatibility,
                quantity, unit_price_min, unit_price_max, supply_mode, availability, availability_note,
                approval_status, warranty_days, status, data_source, created_by_user_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                request_id, row["mission_id"], row["mission_task_id"], row["asset_id"],
                payload.get("part_name")[:200], payload.get("part_number"), payload.get("compatibility"),
                quantity, engine._to_float(payload.get("unit_price_min")), engine._to_float(payload.get("unit_price_max")),
                (payload.get("supply_mode") or "PROVIDER").upper(),
                (payload.get("availability") or ("AVAILABLE" if is_provider else "ESTIMATED")).upper(),
                payload.get("availability_note"),
                "PENDING" if payload.get("requires_approval") else ("NOT_REQUIRED" if is_provider else "PENDING"),
                int(engine._to_float(payload.get("warranty_days")) or 0),
                (payload.get("status") or "PROPOSED").upper(), data_source, user_id,
            ),
        )
        engine.log_evidence(
            connection, request_id=request_id, asset_id=row["asset_id"], mission_id=row["mission_id"],
            task_id=row["mission_task_id"], stage="PARTS", kind="PART",
            note=f"{'Provider' if is_provider else 'Customer'} added part: {payload.get('part_name')} ×{quantity} "
                 f"({data_source.lower()} data).",
            actor_user_id=user_id, actor_role="provider" if is_provider else "customer",
            data_source=data_source,
        )
        connection.commit()
        parts = engine.parts_for_request(connection, request_id)
    finally:
        connection.close()
    _notify_socket(request_id)
    return _ok({"parts": [dict(part) for part in parts], "summary": engine.parts_summary(parts)})


@v10.route("/api/parts/<int:part_id>/approve", methods=["POST"])
@role_required("customer")
def api_approve_part(part_id):
    payload = request.get_json(silent=True) or {}
    decision = (payload.get("decision") or "APPROVE").upper()
    connection = get_db_connection()
    try:
        part = connection.execute("SELECT * FROM service_parts WHERE id=?", (part_id,)).fetchone()
        if not part:
            return _fail("Part not found.", 404)
        row = connection.execute(
            "SELECT customer_id, mission_id, asset_id, mission_task_id, status FROM service_requests WHERE id=?",
            (part["request_id"],),
        ).fetchone()
        if not row or int(row["customer_id"]) != int(current_user_id()):
            return _fail("You cannot approve this part.", 403)
        if row["status"] in ("AWAITING_PAYMENT", "COMPLETED"):
            return _fail("This service is closed for changes. Contact support if the amount is wrong.")
        approval = "APPROVED" if decision == "APPROVE" else "DECLINED"
        connection.execute(
            "UPDATE service_parts SET approval_status=?, approval_note=?, approved_at=?, status=? WHERE id=?",
            (approval, payload.get("note"), engine.now_str(),
             "APPROVED" if decision == "APPROVE" else "DECLINED", part_id),
        )
        engine.log_evidence(
            connection, request_id=part["request_id"], asset_id=part["asset_id"],
            mission_id=part["mission_id"], task_id=part["mission_task_id"], stage="PARTS", kind="APPROVAL",
            note=f"Customer {approval.lower()} part '{part['part_name']}'"
                 + (f" — {payload.get('note')}" if payload.get("note") else ""),
            actor_user_id=current_user_id(), actor_role="customer",
        )
        connection.commit()
    finally:
        connection.close()
    _notify_socket(part["request_id"])
    return _ok({"approval_status": approval})


# =========================================================================
# MODULE 6 — OUTCOME + RECOVERY
# =========================================================================

@v10.route("/recovery")
@role_required("customer")
def recovery_page():
    connection = get_db_connection()
    try:
        candidates = engine.recovery_candidates(connection, current_user_id())
        cases = connection.execute(
            """
            SELECT c.*, s.name AS service_name, a.asset_type, a.brand, a.model
            FROM service_recovery_cases c
            LEFT JOIN service_requests sr ON sr.id=c.original_request_id
            LEFT JOIN services s ON s.id=sr.service_id
            LEFT JOIN assets a ON a.id=c.asset_id
            WHERE c.customer_id=? ORDER BY c.id DESC LIMIT 30
            """,
            (current_user_id(),),
        ).fetchall()
        outcomes = connection.execute(
            """
            SELECT o.*, s.name AS service_name, a.asset_type
            FROM service_outcomes o
            LEFT JOIN service_requests sr ON sr.id=o.request_id
            LEFT JOIN services s ON s.id=sr.service_id
            LEFT JOIN assets a ON a.id=o.asset_id
            WHERE o.customer_id=? ORDER BY o.id DESC LIMIT 20
            """,
            (current_user_id(),),
        ).fetchall()
    finally:
        connection.close()
    return render_template("v10/recovery.html", candidates=candidates, cases=cases, outcomes=outcomes)


@v10.route("/api/service/<int:request_id>/recovery", methods=["POST", "GET"])
@login_required
def api_service_recovery(request_id):
    connection = get_db_connection()
    try:
        engine.sweep_completed_services(connection)
        row = connection.execute("SELECT * FROM service_requests WHERE id=?", (request_id,)).fetchone()
        if not row:
            return _fail("Service not found.", 404)
        user_id = int(current_user_id())
        is_customer = int(row["customer_id"]) == user_id
        provider = provider_for_user(connection, user_id)
        is_provider = bool(provider and row["provider_id"] and int(row["provider_id"]) == int(provider["id"]))
        if not (is_customer or is_provider or current_role() == "admin"):
            return _fail("You cannot open a recovery case for this service.", 403)

        if request.method == "GET":
            case = connection.execute(
                "SELECT * FROM service_recovery_cases WHERE original_request_id=? ORDER BY id DESC LIMIT 1",
                (request_id,),
            ).fetchone()
            outcome = connection.execute(
                "SELECT * FROM service_outcomes WHERE request_id=?", (request_id,)
            ).fetchone()
            detail = engine.recovery_case_detail(connection, int(case["id"])) if case else None
            connection.commit()
            return _ok({"case": detail, "outcome": dict(outcome) if outcome else None})

        if not is_customer:
            return _fail("Only the customer can open a recovery case.", 403)
        payload = request.get_json(silent=True) or request.form
        problem = (payload.get("problem") or "").strip()
        if not problem:
            return _fail("Describe what has happened.")
        try:
            uploads = _upload_many("recovery", limit=4)
        except ValueError as exc:
            return _fail(str(exc))
        case_id, error = engine.create_recovery_case(
            connection, user_id, request_id, problem, evidence_paths=uploads,
            severity=(payload.get("severity") or "MEDIUM").upper(), actor_user_id=user_id,
        )
        if error:
            connection.rollback()
            return _fail(error, 404)
        case = connection.execute("SELECT * FROM service_recovery_cases WHERE id=?", (case_id,)).fetchone()
        connection.commit()
    except Exception as exc:
        connection.rollback()
        print("[V10 recovery] error:", repr(exc))
        return _fail("The recovery case could not be created.")
    finally:
        connection.close()
    _notify_socket(request_id)
    return _ok({"case_id": case_id, "case_code": case["case_code"], "warranty_status": case["warranty_status"],
                "match_confidence": case["match_confidence"], "match_reason": case["match_reason"],
                "redirect": url_for("v10.recovery_case_page", case_id=case_id)})


@v10.route("/recovery/<int:case_id>")
@role_required("customer")
def recovery_case_page(case_id):
    connection = get_db_connection()
    try:
        detail = engine.recovery_case_detail(connection, case_id)
        if not detail or int(detail["case"]["customer_id"]) != int(current_user_id()):
            connection.close()
            abort(404)
        connection.commit()
    finally:
        connection.close()
    return render_template("v10/recovery_case.html", detail=detail, media_url=_media_public_url)


@v10.route("/api/recovery/<int:case_id>/action", methods=["POST"])
@role_required("customer")
def api_recovery_action(case_id):
    payload = request.get_json(silent=True) or {}
    action = (payload.get("action") or "").upper()
    connection = get_db_connection()
    try:
        case = connection.execute(
            "SELECT * FROM service_recovery_cases WHERE id=? AND customer_id=?", (case_id, current_user_id())
        ).fetchone()
        if not case:
            return _fail("Recovery case not found.", 404)
        if action == "CONTACT_PROVIDER":
            engine.resolve_recovery_case(
                connection, case_id, action=action,
                resolution="Customer chose to contact the original provider first.",
            )
            engine.log_evidence(
                connection, request_id=case["original_request_id"], asset_id=case["asset_id"],
                stage="RECOVERY", kind="ACTION", note="Customer chose to contact the original provider.",
                actor_user_id=current_user_id(), actor_role="customer",
            )
            connection.commit()
            return _ok({"action": action, "message": "The original provider can now see your recovery report in the chat."})
        if action == "NEW_PROVIDER":
            engine.resolve_recovery_case(
                connection, case_id, action=action,
                resolution="Customer requested a new provider for this fault.",
            )
            connection.commit()
            original = connection.execute(
                "SELECT service_id, asset_id, description FROM service_requests WHERE id=?",
                (case["original_request_id"],),
            ).fetchone()
            return _ok({
                "action": action,
                "redirect": url_for("v10.booking_page", service_ref=original["service_id"])
                if original else url_for("v10.categories_page"),
                "asset_id": original["asset_id"] if original else None,
                "message": "Create a new booking — SmartServe will carry the previous history into the new mission.",
            })
        if action == "SECOND_OPINION":
            return _ok({"action": action, "redirect": url_for("v10.second_opinion_page", request_id=case["original_request_id"])})
        if action == "OPEN_DISPUTE":
            connection.commit()
            return _ok({"action": action, "redirect": url_for("marketplace_features") + "#disputes",
                        "message": "Open a dispute from the Safety & Tools centre so the operations team can review both services."})
        if action == "CLOSE":
            engine.resolve_recovery_case(connection, case_id, action=action,
                                         resolution=payload.get("resolution") or "Closed by customer.")
            connection.commit()
            return _ok({"action": action})
        return _fail("Unknown action.")
    finally:
        connection.close()



# =========================================================================
# PROVIDER DECISIONS — accept / decline from the V10 mission brief
# =========================================================================

@v10.route("/api/service/<int:request_id>/accept", methods=["POST"])
@role_required("provider")
def api_provider_accept(request_id):
    """Accept a job from the V10 mission brief.

    Extends the legacy ``/provider-response/<id>/accept`` flow with a JSON
    response so the V10 workspace can continue without a page round-trip.
    """
    connection = get_db_connection()
    confirmation_code = None
    try:
        provider = provider_for_user(connection, current_user_id())
        if not provider:
            return _fail("Provider profile not found.", 403)
        row = connection.execute("SELECT * FROM service_requests WHERE id=?", (request_id,)).fetchone()
        if not row:
            return _fail("Request not found.", 404)
        allowed, reason = _provider_can_view_request(connection, int(provider["id"]), row)
        if not allowed:
            return _fail(reason or "You are not eligible for this service request.", 403)
        if row["status"] == "ACCEPTED" and row["provider_id"] and int(row["provider_id"]) == int(provider["id"]):
            return _ok({"message": "Job already accepted.", "redirect": url_for("v10.booking_detail", request_id=request_id)})
        if row["status"] not in ("ASSIGNED", "OFFERED", "SEARCHING", "SCHEDULED", "PENDING"):
            return _fail("This request is no longer available to accept.", 409)
        confirmation_code = f"{secrets.randbelow(900000) + 100000:06d}"
        updated = connection.execute(
            """
            UPDATE service_requests
            SET provider_id=?, status='ACCEPTED', accepted_at=CURRENT_TIMESTAMP,
                confirmation_code=?, arrival_status='NOT_STARTED'
            WHERE id=? AND (provider_id IS NULL OR provider_id=?)
            """,
            (int(provider["id"]), confirmation_code, request_id, int(provider["id"])),
        )
        if updated.rowcount != 1:
            return _fail("Another action changed this request. Refresh and try again.", 409)
        _log_request_event(connection, request_id, current_user_id(), "PROVIDER_ACCEPTED",
                           "Provider accepted the job from the V10 mission brief.",
                           {"provider_id": int(provider["id"]), "source": "V10_MISSION_BRIEF"})
        if row["mission_task_id"]:
            connection.execute(
                "UPDATE mission_tasks SET status='ASSIGNED', provider_id=? WHERE id=? AND request_id=?",
                (int(provider["id"]), int(row["mission_task_id"]), request_id),
            )
        connection.commit()
    finally:
        connection.close()
    _notify_socket(request_id, "provider_accepted")
    return _ok({"message": "Job accepted.", "confirmation_code": confirmation_code,
                "redirect": url_for("v10.booking_detail", request_id=request_id)})


@v10.route("/api/service/<int:request_id>/decline", methods=["POST"])
@role_required("provider")
def api_provider_decline(request_id):
    connection = get_db_connection()
    try:
        provider = provider_for_user(connection, current_user_id())
        if not provider:
            return _fail("Provider profile not found.", 403)
        row = connection.execute("SELECT * FROM service_requests WHERE id=?", (request_id,)).fetchone()
        if not row:
            return _fail("Request not found.", 404)
        if row["provider_id"] is None or int(row["provider_id"]) != int(provider["id"]):
            return _fail("This request is not assigned to you.", 409)
        if row["status"] not in ("ASSIGNED", "OFFERED"):
            return _fail("This request is already in progress — contact support instead of declining.", 409)
        connection.execute(
            "UPDATE service_requests SET provider_id=NULL, status='SEARCHING', accepted_at=NULL WHERE id=?",
            (request_id,),
        )
        _log_request_event(connection, request_id, current_user_id(), "PROVIDER_REJECTED",
                           "Provider declined the job from the V10 mission brief.", {"provider_id": int(provider["id"])})
        connection.commit()
    finally:
        connection.close()
    _notify_socket(request_id, "provider_rejected")
    return _ok({"message": "Request declined — it returns to the matching pool.",
                "redirect": url_for("v10.provider_home")})


# =========================================================================
# MODULE 7 — PROVIDER HUB / SKILL PASSPORT
# =========================================================================

@v10.route("/pro")
@role_required("provider")
def provider_home():
    connection = get_db_connection()
    try:
        engine.sweep_completed_services(connection)
        provider = provider_for_user(connection, current_user_id())
        if not provider:
            connection.close()
            flash("Complete your provider profile first.")
            return redirect(url_for("provider_profile_edit"))
        pid = int(provider["id"])
        new_requests = connection.execute(
            """
            SELECT sr.*, s.name AS service_name, s.icon AS service_icon, s.risk_level, s.estimated_duration,
                   cu.name AS customer_name, a.asset_type, a.brand, a.model,
                   m.id AS mission_id, m.mission_code, m.title AS mission_title, m.risk_level AS mission_risk,
                   (SELECT COUNT(*) FROM mission_tasks t WHERE t.mission_id=sr.mission_id) AS mission_task_count
            FROM service_requests sr
            JOIN services s ON s.id=sr.service_id
            JOIN users cu ON cu.id=sr.customer_id
            LEFT JOIN assets a ON a.id=sr.asset_id
            LEFT JOIN service_missions m ON m.id=sr.mission_id
            WHERE sr.status IN ('PENDING','SEARCHING','OFFERED','SCHEDULED')
              AND (sr.provider_id IS NULL OR sr.provider_id=?)
              AND EXISTS (SELECT 1 FROM provider_services ps WHERE ps.provider_id=? AND ps.service_id=sr.service_id)
            ORDER BY sr.id DESC LIMIT 15
            """,
            (pid, pid),
        ).fetchall()
        active_jobs = connection.execute(
            """
            SELECT sr.*, s.name AS service_name, s.icon AS service_icon, cu.name AS customer_name,
                   cu.phone AS customer_phone, a.asset_type, a.brand, a.model, a.asset_uid,
                   m.mission_code, m.id AS mission_id
            FROM service_requests sr
            JOIN services s ON s.id=sr.service_id
            JOIN users cu ON cu.id=sr.customer_id
            LEFT JOIN assets a ON a.id=sr.asset_id
            LEFT JOIN service_missions m ON m.id=sr.mission_id
            WHERE sr.provider_id=? AND sr.status IN ('ACCEPTED','ARRIVED','IN_PROGRESS','AWAITING_VERIFICATION','AWAITING_PAYMENT')
            ORDER BY sr.id DESC LIMIT 10
            """,
            (pid,),
        ).fetchall()
        stats = connection.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM service_requests WHERE provider_id=? AND status='COMPLETED') completed,
              (SELECT COALESCE(SUM(net_amount),0) FROM provider_earnings WHERE provider_id=?) earnings,
              (SELECT COUNT(*) FROM reviews WHERE provider_id=?) reviews,
              (SELECT COALESCE(AVG(rating),0) FROM reviews WHERE provider_id=?) rating
            """,
            (pid, pid, pid, pid),
        ).fetchone()
        skill_summary = engine.student_skill_passport(connection, pid)
        completed_jobs = connection.execute(
            """
            SELECT sr.id, sr.completed_at, s.name AS service_name, m.mission_code, a.asset_type
            FROM service_requests sr JOIN services s ON s.id=sr.service_id
            LEFT JOIN service_missions m ON m.id=sr.mission_id
            LEFT JOIN assets a ON a.id=sr.asset_id
            WHERE sr.provider_id=? ORDER BY sr.id DESC LIMIT 5
            """,
            (pid,),
        ).fetchall()
        connection.commit()
    finally:
        connection.close()
    return render_template(
        "v10/pro.html", provider=provider, new_requests=new_requests, active_jobs=active_jobs,
        stats=stats, skills=skill_summary, completed_jobs=completed_jobs, media_url=_media_public_url,
    )


@v10.route("/provider/mission-brief/<int:request_id>")
@role_required("provider")
def mission_brief(request_id):
    connection = get_db_connection()
    try:
        provider = provider_for_user(connection, current_user_id())
        if not provider:
            connection.close()
            abort(403)
        row = connection.execute(
            """
            SELECT sr.*, s.name AS service_name, s.icon AS service_icon, s.risk_level, s.estimated_duration,
                   s.required_equipment, s.required_certification, s.pricing_model,
                   cu.name AS customer_name, cu.phone AS customer_phone, cu.profile_photo_path AS customer_photo,
                   a.asset_uid, a.asset_type, a.brand, a.model, a.purchase_date, a.warranty_end, a.health_score,
                   m.id AS mission_id, m.mission_code, m.title AS mission_title, m.problem AS mission_problem,
                   m.risk_level AS mission_risk, m.safety_notes, m.required_categories, m.budget_customer,
                   m.estimated_total
            FROM service_requests sr
            JOIN services s ON s.id=sr.service_id
            JOIN users cu ON cu.id=sr.customer_id
            LEFT JOIN assets a ON a.id=sr.asset_id
            LEFT JOIN service_missions m ON m.id=sr.mission_id
            WHERE sr.id=?
            """,
            (request_id,),
        ).fetchone()
        if not row:
            connection.close()
            abort(404)
        allowed, reason = _provider_can_view_request(connection, int(provider["id"]), row)
        if not allowed:
            connection.close()
            flash("You are not eligible for this service request." + (f" {reason}" if reason else ""))
            return redirect(url_for("v10.provider_home"))
        tasks = engine.mission_tasks(connection, int(row["mission_id"])) if row["mission_id"] else []
        my_task = next((task for task in tasks if row["mission_task_id"] and task["id"] == row["mission_task_id"]), None)
        parts = engine.parts_for_request(connection, request_id)
        if not parts:
            parts = engine.suggest_parts_for_request(connection, request_id)
        history = []
        if row["asset_uid"]:
            history = engine.asset_history(connection, connection.execute(
                "SELECT id FROM assets WHERE asset_uid=?", (row["asset_uid"],)
            ).fetchone()["id"], limit=5)
        analysis = engine.loads(row["ai_structured_json"], {})
        if not analysis:
            analysis = {
                "problem": row["ai_problem"],
                "possible_causes": [c.strip() for c in (row["ai_possible_cause"] or "").split(",") if c.strip()],
                "confidence": None,
                "data_source": "LEGACY",
            }
        connection.commit()
    finally:
        connection.close()
    conversation = None
    connection = get_db_connection()
    try:
        conversation = connection.execute(
            "SELECT COUNT(*) c FROM messages WHERE request_id=?", (request_id,)
        ).fetchone()["c"]
    finally:
        connection.close()
    return render_template(
        "v10/mission_brief.html",
        r=row, provider=provider, tasks=tasks, my_task=my_task, parts=parts, parts_summary=engine.parts_summary(parts),
        history=history, analysis=analysis, media_url=_media_public_url, message_count=conversation,
        eligibility_ok=allowed, media_items=_request_media_items(row),
    )


@v10.route("/api/service/<int:request_id>/mission-brief")
@role_required("provider")
def api_mission_brief(request_id):
    connection = get_db_connection()
    try:
        provider = provider_for_user(connection, current_user_id())
        if not provider:
            return _fail("Provider profile required.", 403)
        row = connection.execute("SELECT * FROM service_requests WHERE id=?", (request_id,)).fetchone()
        if not row:
            return _fail("Request not found.", 404)
        allowed, reason = _provider_can_view_request(connection, int(provider["id"]), row)
        if not allowed:
            return _fail("Not eligible: " + (reason or "this request is not open to you."), 403)
        tasks = engine.mission_tasks(connection, int(row["mission_id"])) if row["mission_id"] else []
        visible = {
            "request_id": row["id"],
            "service": connection.execute("SELECT name FROM services WHERE id=?", (row["service_id"],)).fetchone()["name"],
            "problem": row["description"],
            "ai_analysis": engine.loads(row["ai_structured_json"], {}),
            "asset": {
                "asset_uid": row["asset_id"],
            },
            "mission": engine.get_mission(connection, int(row["mission_id"]))["mission_code"] if row["mission_id"] else None,
            "my_task": next((dict(t) for t in tasks if t["id"] == row["mission_task_id"]), None),
            "customer_area": (row["customer_pincode"] or "")[:3] + "***" if row["customer_pincode"] else None,
            "budget": row["budget_customer"],
            "preferred_time": row["preferred_time"],
            "safety_notes": engine.loads_list(
                (engine.get_mission(connection, int(row["mission_id"]))["safety_notes"] if row["mission_id"] else "[]")
            ),
            "privacy_note": "Customer contact details are shared only after you accept the job.",
        }
    finally:
        connection.close()
    return _ok({"brief": visible})


@v10.route("/api/service/<int:request_id>/clarify", methods=["POST"])
@role_required("provider")
def api_request_clarification(request_id):
    payload = request.get_json(silent=True) or request.form
    question = (payload.get("question") or "").strip()
    if not question:
        return _fail("Write the question you want to ask.")
    connection = get_db_connection()
    try:
        provider = provider_for_user(connection, current_user_id())
        row = connection.execute("SELECT * FROM service_requests WHERE id=?", (request_id,)).fetchone()
        if not row or not provider:
            return _fail("Request not found.", 404)
        allowed, reason = _provider_can_view_request(connection, int(provider["id"]), row)
        if not allowed:
            return _fail("Not eligible: " + (reason or "this request is not open to you."), 403)
        connection.execute(
            "INSERT INTO messages(request_id, sender_id, message) VALUES(?,?,?)",
            (request_id, current_user_id(), f"[Clarification request] {question}"[:1000]),
        )
        _log_request_event(connection, request_id, current_user_id(), "PROVIDER_CLARIFICATION",
                           question[:400], {"provider_id": int(provider["id"])})
        engine.log_evidence(
            connection, request_id=request_id, asset_id=row["asset_id"], mission_id=row["mission_id"],
            task_id=row["mission_task_id"], stage="COORDINATE", kind="QUESTION",
            note=f"Provider asked for clarification: {question[:400]}",
            actor_user_id=current_user_id(), actor_role="provider",
        )
        engine.on_request_event(connection, request_id, "PROVIDER_CLARIFICATION", question[:200], current_user_id())
        connection.commit()
    finally:
        connection.close()
    _notify_socket(request_id, "new_message")
    return _ok({"message": "Your question was sent to the customer."})


@v10.route("/api/service/<int:request_id>/propose-diagnosis", methods=["POST"])
@role_required("provider")
def api_propose_diagnosis(request_id):
    payload = request.get_json(silent=True) or request.form
    diagnosis = (payload.get("diagnosis") or "").strip()
    if not diagnosis:
        return _fail("Describe what you found on site.")
    connection = get_db_connection()
    try:
        provider = provider_for_user(connection, current_user_id())
        row = connection.execute("SELECT * FROM service_requests WHERE id=?", (request_id,)).fetchone()
        if not row or not provider:
            return _fail("Request not found.", 404)
        is_assigned = row["provider_id"] and int(row["provider_id"]) == int(provider["id"])
        if not is_assigned and (row["status"] or "").upper() not in ("PENDING", "SEARCHING", "OFFERED", "ASSIGNED", "SCHEDULED"):
            return _fail("You can only propose a diagnosis on an open or assigned request.", 403)
        connection.execute(
            "UPDATE service_requests SET provider_diagnosis=? WHERE id=?", (diagnosis[:2000], request_id)
        )
        engine.log_evidence(
            connection, request_id=request_id, asset_id=row["asset_id"], mission_id=row["mission_id"],
            task_id=row["mission_task_id"], stage="DIAGNOSIS", kind="DIAGNOSIS", note=diagnosis[:2000],
            actor_user_id=current_user_id(), actor_role="provider",
        )
        engine.sync_task_from_request(connection, request_id)
        connection.commit()
    finally:
        connection.close()
    _notify_socket(request_id)
    return _ok({"message": "Diagnosis recorded in the Service Black Box."})


@v10.route("/api/service/<int:request_id>/propose-package", methods=["POST"])
@role_required("provider")
def api_propose_package(request_id):
    """Provider proposes diagnosis + price + parts together in one submission."""
    payload = request.get_json(silent=True) or request.form
    connection = get_db_connection()
    try:
        provider = provider_for_user(connection, current_user_id())
        row = connection.execute("SELECT * FROM service_requests WHERE id=?", (request_id,)).fetchone()
        if not row or not provider:
            return _fail("Request not found.", 404)
        allowed, reason = _provider_can_view_request(connection, int(provider["id"]), row)
        if not allowed:
            return _fail("Not eligible: " + (reason or "this request is not open to you."), 403)
        diagnosis = (payload.get("diagnosis") or "").strip()
        amount = engine._to_float(payload.get("amount"))
        if diagnosis:
            connection.execute("UPDATE service_requests SET provider_diagnosis=? WHERE id=?", (diagnosis[:2000], request_id))
            engine.log_evidence(
                connection, request_id=request_id, asset_id=row["asset_id"], mission_id=row["mission_id"],
                task_id=row["mission_task_id"], stage="DIAGNOSIS", kind="DIAGNOSIS", note=diagnosis[:2000],
                actor_user_id=current_user_id(), actor_role="provider",
            )
        if amount and amount > 0:
            connection.execute(
                """
                INSERT INTO price_negotiations(request_id, sender_user_id, proposed_amount, message, status)
                VALUES(?,?,?,?, 'PENDING')
                """,
                (request_id, current_user_id(), amount, (payload.get("note") or "Provider package proposal")[:500]),
            )
            connection.execute("UPDATE service_requests SET negotiation_status='OPEN' WHERE id=?", (request_id,))
            engine.log_evidence(
                connection, request_id=request_id, asset_id=row["asset_id"], mission_id=row["mission_id"],
                task_id=row["mission_task_id"], stage="PRICE", kind="PRICE", amount=amount,
                note=f"Provider proposed ₹{amount:,.0f} — customer approval required.",
                actor_user_id=current_user_id(), actor_role="provider",
            )
        parts = payload.get("parts") or []
        if isinstance(parts, str):
            parts = [p.strip() for p in parts.split(",") if p.strip()]
        for part_name in parts[:10]:
            connection.execute(
                """
                INSERT INTO service_parts(
                    request_id, mission_id, task_id, asset_id, part_name, quantity, supply_mode,
                    availability, approval_status, status, data_source, created_by_user_id
                ) VALUES(?,?,?,?,?,1,'PROVIDER','AVAILABLE','PENDING','PROPOSED','PROVIDER',?)
                """,
                (request_id, row["mission_id"], row["mission_task_id"], row["asset_id"], str(part_name)[:200], current_user_id()),
            )
        engine.sync_task_from_request(connection, request_id)
        connection.commit()
    finally:
        connection.close()
    _notify_socket(request_id)
    return _ok({"message": "Proposal sent to the customer for approval."})


@v10.route("/provider/skills")
@role_required("provider")
def skill_passport_page():
    connection = get_db_connection()
    try:
        provider = provider_for_user(connection, current_user_id())
        if not provider:
            connection.close()
            return redirect(url_for("provider_profile_edit"))
        passport = engine.student_skill_passport(connection, int(provider["id"]))
        services = connection.execute(
            """
            SELECT s.* FROM provider_services ps JOIN services s ON s.id=ps.service_id
            WHERE ps.provider_id=? ORDER BY s.name
            """,
            (int(provider["id"]),),
        ).fetchall()
        connection.commit()
    finally:
        connection.close()
    return render_template("v10/skills.html", passport=passport, provider=provider, services=services,
                           media_url=_media_public_url)


@v10.route("/api/provider/skills/<int:skill_id>/request-verification", methods=["POST"])
@role_required("provider")
def api_request_skill_verification(skill_id):
    payload = request.get_json(silent=True) or {}
    connection = get_db_connection()
    try:
        provider = provider_for_user(connection, current_user_id())
        skill = connection.execute(
            "SELECT * FROM student_skills WHERE id=? AND provider_id=?", (skill_id, int(provider["id"]) if provider else -1)
        ).fetchone()
        if not skill:
            return _fail("Skill not found.", 404)
        connection.execute(
            """
            INSERT INTO student_skill_verifications(
                provider_id, student_skill_id, skill_key, method, result, evidence, notes
            ) VALUES(?,?,?,?, 'PENDING_REVIEW', ?, ?)
            """,
            (skill["provider_id"], skill_id, skill["skill_key"], "SELF_REQUEST",
             (payload.get("evidence") or "")[:1000] or None, (payload.get("notes") or "")[:1000] or None),
        )
        connection.execute(
            "UPDATE student_skills SET updated_at=? WHERE id=?", (engine.now_str(), skill_id)
        )
        connection.commit()
    finally:
        connection.close()
    return _ok({"message": "Verification requested. The SmartServe operations team will review it."})


@v10.route("/api/provider/certifications", methods=["GET", "POST"])
@role_required("provider")
def api_provider_certifications():
    connection = get_db_connection()
    try:
        provider = provider_for_user(connection, current_user_id())
        if not provider:
            return _fail("Provider profile required.", 403)
        if request.method == "GET":
            rows = connection.execute(
                "SELECT * FROM provider_certifications WHERE provider_id=? ORDER BY id DESC", (int(provider["id"]),)
            ).fetchall()
            return _ok({"certifications": [dict(row) for row in rows]})
        payload = request.get_json(silent=True) or request.form
        if not (payload.get("name") or "").strip():
            return _fail("Enter the certification name.")
        try:
            documents = _upload_many("certificate", limit=1)
        except ValueError as exc:
            return _fail(str(exc))
        connection.execute(
            """
            INSERT INTO provider_certifications(
                provider_id, name, issuer, credential_id, issued_on, expires_on, document_path, status
            ) VALUES(?,?,?,?,?,?,?, 'PENDING')
            """,
            (int(provider["id"]), payload.get("name")[:200], payload.get("issuer"),
             payload.get("credential_id"), payload.get("issued_on"), payload.get("expires_on"),
             documents[0] if documents else None),
        )
        connection.commit()
    finally:
        connection.close()
    return _ok({"message": "Certification submitted for verification."})


@v10.route("/messages")
@login_required
def messages_page():
    connection = get_db_connection()
    try:
        user_id = current_user_id()
        if current_role() == "provider":
            provider = provider_for_user(connection, user_id)
            conversations = connection.execute(
                """
                SELECT sr.id AS request_id, s.name AS service_name, cu.name AS peer_name,
                       cu.profile_photo_path AS peer_photo, sr.status,
                       (SELECT message FROM messages m WHERE m.request_id=sr.id ORDER BY m.id DESC LIMIT 1) AS last_message,
                       (SELECT created_at FROM messages m WHERE m.request_id=sr.id ORDER BY m.id DESC LIMIT 1) AS last_at,
                       m.mission_code
                FROM service_requests sr
                JOIN services s ON s.id=sr.service_id
                JOIN users cu ON cu.id=sr.customer_id
                LEFT JOIN service_missions m ON m.id=sr.mission_id
                WHERE sr.provider_id=?
                ORDER BY COALESCE(last_at, sr.created_at) DESC LIMIT 30
                """,
                (int(provider["id"]) if provider else -1,),
            ).fetchall()
        else:
            conversations = connection.execute(
                """
                SELECT sr.id AS request_id, s.name AS service_name, pu.name AS peer_name,
                       pu.profile_photo_path AS peer_photo, sr.status,
                       (SELECT message FROM messages m WHERE m.request_id=sr.id ORDER BY m.id DESC LIMIT 1) AS last_message,
                       (SELECT created_at FROM messages m WHERE m.request_id=sr.id ORDER BY m.id DESC LIMIT 1) AS last_at,
                       m.mission_code
                FROM service_requests sr
                JOIN services s ON s.id=sr.service_id
                LEFT JOIN providers p ON p.id=sr.provider_id
                LEFT JOIN users pu ON pu.id=p.user_id
                LEFT JOIN service_missions m ON m.id=sr.mission_id
                WHERE sr.customer_id=?
                ORDER BY COALESCE(last_at, sr.created_at) DESC LIMIT 30
                """,
                (user_id,),
            ).fetchall()
    finally:
        connection.close()
    return render_template("v10/messages.html", conversations=conversations, media_url=_media_public_url)


# =========================================================================
# CUSTOMER PROFILE + VERIFICATION
# =========================================================================

@v10.route("/profile", methods=["GET", "POST"])
@role_required("customer")
def profile_page():
    connection = get_db_connection()
    try:
        user_id = current_user_id()
        profile = connection.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        addresses = connection.execute(
            "SELECT * FROM user_addresses WHERE user_id=? ORDER BY is_default DESC, id DESC", (user_id,)
        ).fetchall()
        favorites = connection.execute(
            """
            SELECT p.id AS provider_id, u.name, p.rating, p.skills, u.profile_photo_path
            FROM favorite_providers f JOIN providers p ON p.id=f.provider_id JOIN users u ON u.id=p.user_id
            WHERE f.customer_id=? ORDER BY f.id DESC LIMIT 10
            """,
            (user_id,),
        ).fetchall()
        assets = engine.list_assets(connection, user_id)
        warranties = connection.execute(
            """
            SELECT sr.id, sr.warranty_until, s.name AS service_name, sr.asset_id
            FROM service_requests sr JOIN services s ON s.id=sr.service_id
            WHERE sr.customer_id=? AND sr.warranty_until IS NOT NULL AND datetime(sr.warranty_until)>=datetime('now')
            ORDER BY sr.warranty_until LIMIT 6
            """,
            (user_id,),
        ).fetchall()
        recovery_cases = connection.execute(
            "SELECT * FROM service_recovery_cases WHERE customer_id=? ORDER BY id DESC LIMIT 6", (user_id,)
        ).fetchall()
        bookings = connection.execute(
            """
            SELECT sr.id, sr.status, sr.created_at, s.name AS service_name
            FROM service_requests sr JOIN services s ON s.id=sr.service_id
            WHERE sr.customer_id=? ORDER BY sr.id DESC LIMIT 6
            """,
            (user_id,),
        ).fetchall()
        reviews = connection.execute(
            """
            SELECT r.rating, r.review, r.created_at, s.name AS service_name
            FROM reviews r JOIN service_requests sr ON sr.id=r.request_id
            JOIN services s ON s.id=sr.service_id
            WHERE r.customer_id=? ORDER BY r.id DESC LIMIT 6
            """,
            (user_id,),
        ).fetchall()
        trusted = connection.execute(
            "SELECT * FROM trusted_contacts WHERE user_id=? ORDER BY id DESC", (user_id,)
        ).fetchall()
        prefs = connection.execute(
            "SELECT * FROM notification_preferences WHERE user_id=?", (user_id,)
        ).fetchone()
    finally:
        connection.close()
    return render_template(
        "v10/profile.html", profile=profile, addresses=addresses, favorites=favorites, assets=assets,
        warranties=warranties, recovery_cases=recovery_cases, bookings=bookings, reviews=reviews,
        trusted=trusted, prefs=prefs, media_url=_media_public_url,
    )


@v10.route("/api/profile", methods=["PUT", "POST"])
@role_required("customer", "provider", "admin")
def api_update_profile():
    payload = request.get_json(silent=True) or request.form
    fields = {}
    if "name" in payload and str(payload.get("name")).strip():
        fields["name"] = str(payload["name"]).strip()[:120]
    for key in ("preferred_language", "address_line", "city", "state", "pincode"):
        if key in payload:
            fields[key] = (str(payload.get(key) or "").strip() or None)
    if payload.get("phone") and str(payload["phone"]).strip() != "":
        new_phone = "".join(ch for ch in str(payload["phone"]) if ch.isdigit() or ch == "+")
        connection = get_db_connection()
        try:
            existing = connection.execute(
                "SELECT id, phone FROM users WHERE id=?", (current_user_id(),)
            ).fetchone()
        finally:
            connection.close()
        if str(existing["phone"] or "") != new_phone:
            connection = get_db_connection()
            try:
                connection.execute(
                    "UPDATE users SET phone=?, phone_verified=0, phone_verified_at=NULL WHERE id=?",
                    (new_phone, current_user_id()),
                )
                connection.commit()
            finally:
                connection.close()
            return _ok({"message": "Phone number updated. Please verify the new number.", "phone_changed": True,
                        "verify_url": url_for("v10.verify_center")})
    if not fields and "email" not in payload:
        return _fail("Nothing to update.")
    connection = get_db_connection()
    try:
        if "email" in payload and str(payload.get("email")).strip():
            new_email = str(payload["email"]).strip().lower()
            current = connection.execute("SELECT email FROM users WHERE id=?", (current_user_id(),)).fetchone()
            if "@" not in new_email or "." not in new_email.split("@")[-1]:
                return _fail("Enter a valid email address.")
            if new_email != (current["email"] or "").lower():
                clash = connection.execute(
                    "SELECT id FROM users WHERE LOWER(email)=? AND id<>?", (new_email, current_user_id())
                ).fetchone()
                if clash:
                    return _fail("That email is already registered.")
                connection.execute(
                    "UPDATE users SET email=?, email_verified=0, email_verification_sent_at=NULL WHERE id=?",
                    (new_email, current_user_id()),
                )
                connection.commit()
                try:
                    from app import _send_auth_email
                    user = connection.execute("SELECT * FROM users WHERE id=?", (current_user_id(),)).fetchone()
                    _send_auth_email(user, event="signup", login_method="email")
                except Exception as exc:
                    print("[V10 profile] verification email warning:", repr(exc))
                fields["email_changed"] = True
        if fields:
            updates = {key: value for key, value in fields.items() if key != "email_changed"}
            if updates:
                assignments = ", ".join(f"{key}=?" for key in updates)
                connection.execute(
                    f"UPDATE users SET {assignments} WHERE id=?", (*updates.values(), current_user_id())
                )
        connection.commit()
        fresh = connection.execute("SELECT * FROM users WHERE id=?", (current_user_id(),)).fetchone()
    finally:
        connection.close()
    return _ok({
        "message": "Profile updated." + (" Verify your new email address to restore full trust status." if fields.get("email_changed") else ""),
        "user": {"name": fresh["name"], "email": fresh["email"], "phone": fresh["phone"],
                 "email_verified": bool(fresh["email_verified"]), "phone_verified": bool(fresh["phone_verified"])},
        "email_changed": bool(fields.get("email_changed")),
    })


@v10.route("/api/profile/photo", methods=["POST"])
@role_required("customer", "provider", "admin")
def api_profile_photo():
    if request.form.get("remove") == "1":
        connection = get_db_connection()
        try:
            connection.execute("UPDATE users SET profile_photo_path=NULL WHERE id=?", (current_user_id(),))
            connection.commit()
        finally:
            connection.close()
        return _ok({"message": "Profile photo removed.", "path": None})
    try:
        saved = _upload_many("avatar", limit=1)
    except ValueError as exc:
        return _fail(str(exc))
    if not saved:
        return _fail("Choose a photo to upload.")
    connection = get_db_connection()
    try:
        connection.execute("UPDATE users SET profile_photo_path=? WHERE id=?", (saved[0], current_user_id()))
        connection.commit()
    finally:
        connection.close()
    return _ok({"message": "Profile photo updated.", "path": saved[0], "url": _media_public_url(saved[0])})


@v10.route("/api/addresses", methods=["GET", "POST"])
@role_required("customer")
def api_addresses():
    connection = get_db_connection()
    try:
        if request.method == "GET":
            rows = connection.execute(
                "SELECT * FROM user_addresses WHERE user_id=? ORDER BY is_default DESC, id DESC",
                (current_user_id(),),
            ).fetchall()
            return _ok({"addresses": [dict(row) for row in rows]})
        payload = request.get_json(silent=True) or request.form
        text = (payload.get("address_text") or "").strip()
        if not text:
            return _fail("Enter the address.")
        pincode = "".join(ch for ch in str(payload.get("pincode") or "") if ch.isdigit()) or None
        is_default = 1 if payload.get("is_default") else 0
        if is_default:
            connection.execute("UPDATE user_addresses SET is_default=0 WHERE user_id=?", (current_user_id(),))
        connection.execute(
            """
            INSERT INTO user_addresses(user_id,label,address_text,pincode,latitude,longitude,is_default)
            VALUES(?,?,?,?,?,?,?)
            """,
            (current_user_id(), payload.get("label") or "Home", text[:500], pincode,
             engine._to_float(payload.get("latitude")), engine._to_float(payload.get("longitude")), is_default),
        )
        connection.commit()
        rows = connection.execute(
            "SELECT * FROM user_addresses WHERE user_id=? ORDER BY is_default DESC, id DESC", (current_user_id(),)
        ).fetchall()
    finally:
        connection.close()
    return _ok({"addresses": [dict(row) for row in rows]})


@v10.route("/api/addresses/<int:address_id>", methods=["DELETE"])
@role_required("customer")
def api_delete_address(address_id):
    connection = get_db_connection()
    try:
        connection.execute("DELETE FROM user_addresses WHERE id=? AND user_id=?", (address_id, current_user_id()))
        connection.commit()
    finally:
        connection.close()
    return _ok({"message": "Address removed."})


@v10.route("/api/preferences", methods=["GET", "POST"])
@login_required
def api_preferences():
    connection = get_db_connection()
    try:
        if request.method == "GET":
            row = connection.execute(
                "SELECT * FROM notification_preferences WHERE user_id=?", (current_user_id(),)
            ).fetchone()
            return _ok({"preferences": dict(row) if row else {}})
        payload = request.get_json(silent=True) or request.form
        keys = ["email_enabled", "sms_enabled", "push_enabled", "whatsapp_enabled",
                "mission_updates", "warranty_alerts", "marketing"]
        values = {key: 1 if str(payload.get(key)).lower() in ("1", "true", "on", "yes") else 0 for key in keys}
        connection.execute(
            """
            INSERT INTO notification_preferences(user_id,email_enabled,sms_enabled,push_enabled,whatsapp_enabled,
                mission_updates,warranty_alerts,marketing,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                email_enabled=excluded.email_enabled, sms_enabled=excluded.sms_enabled,
                push_enabled=excluded.push_enabled, whatsapp_enabled=excluded.whatsapp_enabled,
                mission_updates=excluded.mission_updates, warranty_alerts=excluded.warranty_alerts,
                marketing=excluded.marketing, updated_at=excluded.updated_at
            """,
            (current_user_id(), *(values[key] for key in keys), engine.now_str()),
        )
        connection.commit()
    finally:
        connection.close()
    return _ok({"message": "Preferences saved."})


# ---------------- Verification centre (email + phone) --------------------

@v10.route("/verify")
@login_required
def verify_center():
    connection = get_db_connection()
    try:
        user = connection.execute("SELECT * FROM users WHERE id=?", (current_user_id(),)).fetchone()
        pending = connection.execute(
            """
            SELECT * FROM phone_otp_codes WHERE user_id=? AND consumed_at IS NULL
            ORDER BY id DESC LIMIT 1
            """,
            (current_user_id(),),
        ).fetchone()
        email_configured = bool(os.getenv("SMTP_HOST") and os.getenv("SMTP_USERNAME"))
        connection.commit()
    finally:
        connection.close()
    return render_template(
        "v10/verify.html", user=user, pending=pending, email_configured=email_configured,
        dev_otp=DEV_OTP_ENABLED, resend_cooldown=_resend_seconds_left(user),
    )


def _resend_seconds_left(user):
    sent = user["email_verification_sent_at"] if user else None
    if not sent:
        return 0
    try:
        sent_at = datetime.strptime(str(sent)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return 0
    elapsed = (engine.now() - sent_at).total_seconds()
    return max(0, int(120 - elapsed))


@v10.route("/api/verify-email/send", methods=["POST"])
@login_required
def api_send_email_verification():
    connection = get_db_connection()
    try:
        user = connection.execute("SELECT * FROM users WHERE id=?", (current_user_id(),)).fetchone()
        if user["email_verified"]:
            return _ok({"message": "This email is already verified.", "already": True})
        cooldown = _resend_seconds_left(user)
        if cooldown > 0:
            return _fail(f"Please wait {cooldown}s before requesting another email.", 429)
        token = _email_token(user)
        base = os.getenv("APP_BASE_URL", "").rstrip("/")
        link = url_for("v10.verify_email_token", token=token, _external=True)
        sent = False
        try:
            from app import _send_email
            html = f"""
            <p>Hello {user['name']},</p>
            <p>Confirm your SmartServe email address to unlock warranty records, Service Passports and mission alerts.</p>
            <p><a href="{link}">Verify my email</a></p>
            <p>This link expires in 30 minutes and can be used once.</p>
            <p>If you did not create a SmartServe account you can ignore this message.</p>
            """
            sent = _send_email(user["email"], "Verify your SmartServe email", html,
                               f"Verify your SmartServe email: {link}")
        except Exception as exc:
            print("[V10 verify] email send warning:", repr(exc))
        connection.execute(
            "UPDATE users SET email_verification_sent_at=? WHERE id=?", (engine.now_str(), current_user_id())
        )
        connection.commit()
    finally:
        connection.close()
    if sent:
        return _ok({"message": "Verification email sent. Check your inbox."})
    return _ok({
        "message": "Email delivery is not configured on this deployment. Use the verification link below.",
        "fallback_link": link,
        "fallback": True,
    })


def _email_serializer():
    from itsdangerous import URLSafeTimedSerializer

    return URLSafeTimedSerializer(os.getenv("FLASK_SECRET_KEY", "smart-serve-development-key"),
                                  salt="smartserve-email-verify")


def _email_token(user):
    return _email_serializer().dumps({"uid": int(user["id"]), "email": user["email"]})


@v10.route("/verify-email/<token>")
def verify_email_token(token):
    from itsdangerous import BadSignature, SignatureExpired

    try:
        data = _email_serializer().loads(token, max_age=EMAIL_TOKEN_MINUTES * 60)
    except SignatureExpired:
        flash("That verification link has expired. Request a new one.")
        return redirect(url_for("v10.verify_center"))
    except BadSignature:
        flash("That verification link is not valid.")
        return redirect(url_for("v10.verify_center") if current_user_id() else url_for("login"))

    connection = get_db_connection()
    try:
        user = connection.execute("SELECT * FROM users WHERE id=?", (data.get("uid"),)).fetchone()
        if not user or (user["email"] or "").lower() != (data.get("email") or "").lower():
            connection.close()
            flash("This verification link no longer matches your account email.")
            return redirect(url_for("login"))
        connection.execute(
            "UPDATE users SET email_verified=1, email_verification_sent_at=NULL WHERE id=?", (user["id"],)
        )
        connection.commit()
    finally:
        connection.close()
    flash("Your email address is verified. ✓")
    return redirect(url_for("v10.verify_center") if current_user_id() else url_for("login"))


@v10.route("/api/verify-phone/send", methods=["POST"])
@login_required
def api_send_phone_otp():
    connection = get_db_connection()
    try:
        user = connection.execute("SELECT * FROM users WHERE id=?", (current_user_id(),)).fetchone()
        phone = (request.get_json(silent=True) or request.form or {}).get("phone") or user["phone"]
        digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
        if len(digits) < 10:
            return _fail("Add a valid phone number to your profile first.")
        code = f"{secrets.randbelow(900000) + 100000}"
        code_hash = _hash_otp(code)
        connection.execute(
            """
            INSERT INTO phone_otp_codes(user_id, phone, purpose, code_hash, expires_at)
            VALUES(?,?, 'SIGNUP', ?, ?)
            """,
            (user["id"], digits, code_hash, (engine.now() + timedelta(minutes=OTP_TTL_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")),
        )
        connection.commit()
    finally:
        connection.close()

    payload = {"message": "A 6-digit verification code was generated."}
    if DEV_OTP_ENABLED:
        payload["dev_code"] = code
        payload["message"] = (
            "DEVELOPMENT MODE: the code is shown below because SMS delivery is not configured. "
            "Set SMARTSERVE_DEV_OTP=0 in production."
        )
    else:
        sent = _send_sms(digits, code)
        payload["sent"] = sent
        if not sent:
            payload["message"] = (
                "SMS delivery is not configured on this deployment. Set SMS_PROVIDER credentials "
                "or enable SMARTSERVE_DEV_OTP=1 for development testing."
            )
    return _ok(payload)


def _hash_otp(code):
    import hashlib

    return hashlib.sha256((code + "|" + os.getenv("FLASK_SECRET_KEY", "smartserve")).encode()).hexdigest()


def _send_sms(phone, code):
    """SMS gateway hook. Returns False when no provider is configured."""
    provider_url = os.getenv("SMS_PROVIDER_URL", "").strip()
    api_key = os.getenv("SMS_PROVIDER_KEY", "").strip()
    if not provider_url or not api_key:
        return False
    try:
        import json as _json
        import urllib.request

        body = _json.dumps({
            "to": phone,
            "message": f"{code} is your SmartServe verification code. It expires in {OTP_TTL_MINUTES} minutes.",
            "sender": os.getenv("SMS_SENDER_ID", "SMTSRV"),
        }).encode()
        req = urllib.request.Request(provider_url, data=body,
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"})
        with urllib.request.urlopen(req, timeout=10) as response:
            return 200 <= response.status < 300
    except Exception as exc:
        print("[V10 verify] SMS send warning:", repr(exc))
        return False


@v10.route("/api/verify-phone/confirm", methods=["POST"])
@login_required
def api_confirm_phone_otp():
    payload = request.get_json(silent=True) or request.form
    code = "".join(ch for ch in str(payload.get("code") or "") if ch.isdigit())
    if len(code) != 6:
        return _fail("Enter the 6-digit code.")
    connection = get_db_connection()
    try:
        row = connection.execute(
            """
            SELECT * FROM phone_otp_codes WHERE user_id=? AND consumed_at IS NULL
            ORDER BY id DESC LIMIT 1
            """,
            (current_user_id(),),
        ).fetchone()
        if not row:
            return _fail("Request a new verification code.")
        if int(row["attempts"] or 0) >= OTP_MAX_ATTEMPTS:
            return _fail("Too many attempts. Request a new code.")
        if str(row["expires_at"]) < engine.now_str():
            return _fail("That code has expired. Request a new one.")
        connection.execute(
            "UPDATE phone_otp_codes SET attempts=attempts+1 WHERE id=?", (row["id"],)
        )
        if _hash_otp(code) != row["code_hash"]:
            connection.commit()
            return _fail("That code is not correct.")
        connection.execute(
            "UPDATE phone_otp_codes SET consumed_at=? WHERE id=?", (engine.now_str(), row["id"])
        )
        connection.execute(
            "UPDATE users SET phone_verified=1, phone_verified_at=?, phone=? WHERE id=?",
            (engine.now_str(), row["phone"], current_user_id()),
        )
        connection.commit()
    finally:
        connection.close()
    return _ok({"message": "Phone number verified. ✓"})


@v10.route("/api/verification-status")
@login_required
def api_verification_status():
    connection = get_db_connection()
    try:
        user = connection.execute(
            "SELECT email, phone, email_verified, phone_verified FROM users WHERE id=?", (current_user_id(),)
        ).fetchone()
    finally:
        connection.close()
    return _ok({
        "email_verified": bool(user["email_verified"]),
        "phone_verified": bool(user["phone_verified"]),
        "email": user["email"],
        "phone": engine.mask_phone(user["phone"]),
    })


# =========================================================================
# ADMIN — V10 console
# =========================================================================

@v10.route("/admin/v10")
@role_required("admin")
def admin_v10():
    connection = get_db_connection()
    try:
        engine.sweep_completed_services(connection)
        counts = {
            "assets": connection.execute("SELECT COUNT(*) c FROM assets").fetchone()["c"],
            "missions": connection.execute("SELECT COUNT(*) c FROM service_missions").fetchone()["c"],
            "missions_active": connection.execute(
                "SELECT COUNT(*) c FROM service_missions WHERE status NOT IN ('COMPLETED','CANCELLED')"
            ).fetchone()["c"],
            "certificates": connection.execute("SELECT COUNT(*) c FROM service_certificates").fetchone()["c"],
            "recovery_open": connection.execute(
                "SELECT COUNT(*) c FROM service_recovery_cases WHERE status='OPEN'"
            ).fetchone()["c"],
            "second_opinions": connection.execute("SELECT COUNT(*) c FROM second_opinion_requests").fetchone()["c"],
            "skills_pending": connection.execute(
                "SELECT COUNT(*) c FROM student_skills WHERE status IN ('TRAINING','SUPERVISED')"
            ).fetchone()["c"],
            "certifications_pending": connection.execute(
                "SELECT COUNT(*) c FROM provider_certifications WHERE status='PENDING'"
            ).fetchone()["c"],
            "parts": connection.execute("SELECT COUNT(*) c FROM service_parts").fetchone()["c"],
        }
        missions = connection.execute(
            """
            SELECT m.*, u.name AS customer_name, a.asset_type,
                   (SELECT COUNT(*) FROM mission_tasks t WHERE t.mission_id=m.id) AS task_count,
                   (SELECT COUNT(*) FROM mission_tasks t WHERE t.mission_id=m.id AND t.status='COMPLETED') AS tasks_done
            FROM service_missions m JOIN users u ON u.id=m.customer_id
            LEFT JOIN assets a ON a.id=m.asset_id
            ORDER BY m.id DESC LIMIT 20
            """
        ).fetchall()
        cases = connection.execute(
            """
            SELECT c.*, u.name AS customer_name, s.name AS service_name
            FROM service_recovery_cases c JOIN users u ON u.id=c.customer_id
            LEFT JOIN service_requests sr ON sr.id=c.original_request_id
            LEFT JOIN services s ON s.id=sr.service_id
            ORDER BY c.id DESC LIMIT 20
            """
        ).fetchall()
        skills = engine.skill_verification_requests(connection)
        certifications = connection.execute(
            """
            SELECT c.*, u.name AS provider_name FROM provider_certifications c
            JOIN providers p ON p.id=c.provider_id JOIN users u ON u.id=p.user_id
            ORDER BY c.id DESC LIMIT 20
            """
        ).fetchall()
        categories = connection.execute(
            """
            SELECT c.*, (SELECT COUNT(*) FROM services s WHERE s.category=c.name) AS service_count
            FROM service_categories c ORDER BY c.sort_order
            """
        ).fetchall()
        report = {
            "missions_completed": connection.execute(
                "SELECT COUNT(*) c FROM service_missions WHERE status='COMPLETED'"
            ).fetchone()["c"],
            "recovery_resolved": connection.execute(
                "SELECT COUNT(*) c FROM service_recovery_cases WHERE status IN ('RESOLVED','CLOSED')"
            ).fetchone()["c"],
            "recovery_total": connection.execute(
                "SELECT COUNT(*) c FROM service_recovery_cases"
            ).fetchone()["c"],
            "evidence_items": connection.execute(
                "SELECT COUNT(*) c FROM service_evidence"
            ).fetchone()["c"],
            "parts_estimated_value": connection.execute(
                "SELECT COALESCE(SUM(COALESCE(unit_price_max, unit_price_min, 0) * COALESCE(quantity,1)),0) v "
                "FROM service_parts"
            ).fetchone()["v"],
            "parts_approved_value": connection.execute(
                "SELECT COALESCE(SUM(COALESCE(unit_price_max, unit_price_min, 0) * COALESCE(quantity,1)),0) v "
                "FROM service_parts WHERE approval_status IN ('APPROVED','NOT_REQUIRED')"
            ).fetchone()["v"],
            "skills_verified": connection.execute(
                "SELECT COUNT(*) c FROM student_skills WHERE status='VERIFIED'"
            ).fetchone()["c"],
            "skills_total": connection.execute("SELECT COUNT(*) c FROM student_skills").fetchone()["c"],
            "passports": counts["assets"],
            "certificates": counts["certificates"],
            "second_opinions": counts["second_opinions"],
        }
        risk_services = connection.execute(
            """
            SELECT id, name, category, risk_level, required_certification, allowed_provider_types
            FROM services WHERE is_active IS NOT 0
            ORDER BY CASE risk_level WHEN 'RESTRICTED' THEN 0 WHEN 'HIGH' THEN 1 WHEN 'MEDIUM' THEN 2 ELSE 3 END,
                     name LIMIT 25
            """
        ).fetchall()
        connection.commit()
    finally:
        connection.close()
    return render_template(
        "v10/admin.html", counts=counts, missions=missions, cases=cases, skills=skills,
        certifications=certifications, categories=categories, report=report, risk_services=risk_services,
    )


@v10.route("/admin/v10/skill/<int:skill_id>/verify", methods=["POST"])
@role_required("admin")
def admin_verify_skill(skill_id):
    payload = request.get_json(silent=True) or request.form
    result = (payload.get("result") or "VERIFIED").upper()
    connection = get_db_connection()
    try:
        outcome = engine.verify_skill(
            connection, skill_id, method=payload.get("method") or "ASSESSMENT",
            result=result, notes=payload.get("notes"), admin_user_id=current_user_id(),
        )
        connection.commit()
    finally:
        connection.close()
    if not outcome:
        return _fail("Skill not found.", 404)
    return _ok({"skill": outcome})


@v10.route("/admin/v10/certification/<int:certification_id>/<action>", methods=["POST"])
@role_required("admin")
def admin_review_certification(certification_id, action):
    status = {"verify": "VERIFIED", "reject": "REJECTED"}.get(action.lower())
    if not status:
        return _fail("Unknown action.")
    connection = get_db_connection()
    try:
        connection.execute(
            "UPDATE provider_certifications SET status=?, reviewed_by_user_id=?, reviewed_at=? WHERE id=?",
            (status, current_user_id(), engine.now_str(), certification_id),
        )
        connection.commit()
    finally:
        connection.close()
    return _ok({"status": status})


@v10.route("/admin/v10/category", methods=["POST"])
@role_required("admin")
def admin_upsert_category():
    payload = request.get_json(silent=True) or request.form
    key = (payload.get("key") or "").strip().lower().replace(" ", "_")
    name = (payload.get("name") or "").strip()
    if not key or not name:
        return _fail("Category key and name are required.")
    connection = get_db_connection()
    try:
        connection.execute(
            """
            INSERT INTO service_categories(key,name,icon,description,risk_level,allowed_provider_types,sort_order,is_active)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(key) DO UPDATE SET
                name=excluded.name, icon=excluded.icon, description=excluded.description,
                risk_level=excluded.risk_level, allowed_provider_types=excluded.allowed_provider_types,
                sort_order=excluded.sort_order, is_active=excluded.is_active
            """,
            (key, name, payload.get("icon") or "🛠️", payload.get("description"),
             (payload.get("risk_level") or "MEDIUM").upper(),
             payload.get("allowed_provider_types") or "any",
             int(engine._to_float(payload.get("sort_order")) or 100),
             0 if str(payload.get("is_active")).lower() in ("0", "false", "off") else 1),
        )
        connection.commit()
    finally:
        connection.close()
    return _ok({"key": key})


@v10.route("/admin/v10/service", methods=["POST"])
@role_required("admin")
def admin_update_service():
    payload = request.get_json(silent=True) or request.form
    service_id = payload.get("service_id")
    if not service_id:
        return _fail("Service id is required.")
    editable = ["description", "category", "subcategory", "risk_level", "required_certification",
                "allowed_provider_types", "estimated_duration", "pricing_model", "is_active", "is_popular"]
    updates = {}
    for key in editable:
        if key in payload:
            value = payload.get(key)
            if key in ("is_active", "is_popular"):
                value = 0 if str(value).lower() in ("0", "false", "off") else 1
            updates[key] = value
    if not updates:
        return _fail("Nothing to update.")
    connection = get_db_connection()
    try:
        assignments = ", ".join(f"{key}=?" for key in updates)
        connection.execute(f"UPDATE services SET {assignments} WHERE id=?", (*updates.values(), service_id))
        connection.commit()
    finally:
        connection.close()
    return _ok({"service_id": service_id})


# =========================================================================
# PWA + misc
# =========================================================================

@v10.route("/sw.js")
def service_worker():
    response = current_app.send_static_file("sw.js")
    response.headers["Content-Type"] = "application/javascript"
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache"
    return response


@v10.route("/manifest.webmanifest")
def manifest():
    response = current_app.send_static_file("manifest.webmanifest")
    response.headers["Content-Type"] = "application/manifest+json"
    return response


@v10.route("/offline")
def offline_page():
    return render_template("v10/offline.html")


@v10.route("/api/v10/status")
def api_v10_status():
    connection = get_db_connection()
    try:
        tables = [
            "assets", "asset_qr_tokens", "asset_service_history", "service_missions", "mission_tasks",
            "service_evidence", "service_certificates", "second_opinion_requests", "second_opinion_results",
            "service_parts", "service_outcomes", "service_recovery_cases", "student_skills",
            "student_skill_verifications", "provider_certifications", "service_categories",
        ]
        present = {}
        for table in tables:
            present[table] = bool(connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone())
        service_count = connection.execute("SELECT COUNT(*) c FROM services").fetchone()["c"]
        category_count = connection.execute("SELECT COUNT(*) c FROM service_categories").fetchone()["c"]
    finally:
        connection.close()
    return _ok({
        "version": "10.0",
        "tables": present,
        "services": service_count,
        "categories": category_count,
        "ai_live": ai_layer.gemini_available(),
        "dev_otp": DEV_OTP_ENABLED,
        "qr_available": _qrcode_available(),
    })


def _qrcode_available():
    try:
        import qrcode  # noqa: F401
        return True
    except Exception:
        return False
