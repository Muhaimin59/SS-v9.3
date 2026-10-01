"""SmartServe V10 — additive database migrations.

Everything in this module is **additive**:

* ``CREATE TABLE IF NOT EXISTS`` for the V10 module tables.
* ``ADD COLUMN`` only when the column is missing (same helper the V9.3 project
  already uses in ``database.py``).
* Catalogue rows are inserted only when they do not already exist and existing
  rows are only *enriched* (never renamed or deleted).

Downgrading is therefore safe: V9.3 code keeps working against a migrated
database because no existing table/column is dropped or rewritten.
"""

from __future__ import annotations

import os
import secrets
import string
from datetime import datetime

from . import catalog


def _add_column(connection, table, column, definition):
    """Add a column only when it is missing (V9.3 compatible)."""
    cursor = connection.cursor()
    existing = {row["name"] for row in cursor.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        print(f"[V10] Added column: {table}.{column}")


def _table_exists(connection, table):
    row = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return bool(row)


def _random_code(length=7, alphabet=string.ascii_uppercase + string.digits):
    return "".join(secrets.choice(alphabet) for _ in range(length))


# =========================================================================
# 1. SCHEMA
# =========================================================================

def create_v10_tables(connection):
    cursor = connection.cursor()

    # ---------------- MODULE 1: Service Passport -------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_uid TEXT UNIQUE NOT NULL,
            customer_id INTEGER NOT NULL,
            asset_type TEXT NOT NULL,
            category TEXT,
            subcategory TEXT,
            nickname TEXT,
            brand TEXT,
            model TEXT,
            serial_number TEXT,
            purchase_date TEXT,
            installation_date TEXT,
            warranty_start TEXT,
            warranty_end TEXT,
            warranty_provider TEXT,
            vendor_name TEXT,
            health_score INTEGER DEFAULT 80,
            health_note TEXT,
            status TEXT DEFAULT 'ACTIVE',
            location_label TEXT,
            photo_path TEXT,
            notes TEXT,
            data_source TEXT DEFAULT 'USER',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(customer_id) REFERENCES users(id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_assets_customer ON assets(customer_id, created_at DESC)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS asset_qr_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id INTEGER NOT NULL,
            token TEXT UNIQUE NOT NULL,
            status TEXT DEFAULT 'ACTIVE',
            scan_count INTEGER DEFAULT 0,
            last_scanned_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            revoked_at TIMESTAMP,
            FOREIGN KEY(asset_id) REFERENCES assets(id) ON DELETE CASCADE
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_asset_qr_asset ON asset_qr_tokens(asset_id, status)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS asset_service_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id INTEGER NOT NULL,
            request_id INTEGER,
            mission_id INTEGER,
            task_id INTEGER,
            provider_id INTEGER,
            certificate_id INTEGER,
            service_name TEXT,
            problem TEXT,
            diagnosis TEXT,
            work_performed TEXT,
            parts_json TEXT,
            amount REAL,
            service_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            warranty_until TIMESTAMP,
            warranty_days INTEGER DEFAULT 0,
            evidence_verified INTEGER DEFAULT 0,
            outcome_status TEXT DEFAULT 'MONITORING',
            data_source TEXT DEFAULT 'REAL',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(asset_id) REFERENCES assets(id) ON DELETE CASCADE
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_asset_history_asset ON asset_service_history(asset_id, service_date DESC)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS asset_maintenance_schedule (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            due_date TEXT,
            interval_days INTEGER,
            status TEXT DEFAULT 'UPCOMING',
            source TEXT DEFAULT 'SYSTEM',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            FOREIGN KEY(asset_id) REFERENCES assets(id) ON DELETE CASCADE
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_asset_maint_asset ON asset_maintenance_schedule(asset_id, status, due_date)")

    # ---------------- MODULE 2: Mission Engine ---------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS service_missions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mission_code TEXT UNIQUE NOT NULL,
            customer_id INTEGER NOT NULL,
            asset_id INTEGER,
            title TEXT NOT NULL,
            problem TEXT NOT NULL,
            description TEXT,
            media_json TEXT,
            voice_note_path TEXT,
            ai_analysis_json TEXT,
            ai_confidence REAL,
            required_categories TEXT,
            risk_level TEXT DEFAULT 'MEDIUM',
            safety_notes TEXT,
            priority TEXT DEFAULT 'NORMAL',
            status TEXT DEFAULT 'PLANNED',
            lifecycle_stage TEXT DEFAULT 'UNDERSTAND',
            estimated_total REAL,
            actual_total REAL,
            currency TEXT DEFAULT 'INR',
            address_text TEXT,
            pincode TEXT,
            latitude REAL,
            longitude REAL,
            preferred_time TEXT,
            scheduled_at TIMESTAMP,
            budget_customer REAL,
            data_source TEXT DEFAULT 'REAL',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            FOREIGN KEY(customer_id) REFERENCES users(id),
            FOREIGN KEY(asset_id) REFERENCES assets(id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_missions_customer ON service_missions(customer_id, created_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_missions_status ON service_missions(status)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mission_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mission_id INTEGER NOT NULL,
            task_order INTEGER DEFAULT 1,
            title TEXT NOT NULL,
            description TEXT,
            service_id INTEGER,
            service_name TEXT,
            required_skill TEXT,
            risk_level TEXT DEFAULT 'MEDIUM',
            depends_on_task_id INTEGER,
            status TEXT DEFAULT 'PENDING',
            provider_id INTEGER,
            request_id INTEGER,
            ai_confidence REAL,
            estimated_cost REAL,
            actual_cost REAL,
            evidence_count INTEGER DEFAULT 0,
            customer_visible INTEGER DEFAULT 1,
            provider_brief TEXT,
            data_source TEXT DEFAULT 'REAL',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            started_at TIMESTAMP,
            completed_at TIMESTAMP,
            FOREIGN KEY(mission_id) REFERENCES service_missions(id) ON DELETE CASCADE,
            FOREIGN KEY(service_id) REFERENCES services(id),
            FOREIGN KEY(provider_id) REFERENCES providers(id),
            FOREIGN KEY(request_id) REFERENCES service_requests(id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_mission_tasks_mission ON mission_tasks(mission_id, task_order)")

    # ---------------- MODULE 3: Service Black Box ------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS service_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER,
            mission_id INTEGER,
            task_id INTEGER,
            asset_id INTEGER,
            stage TEXT NOT NULL,
            kind TEXT DEFAULT 'NOTE',
            path TEXT,
            note TEXT,
            actor_user_id INTEGER,
            actor_role TEXT,
            amount REAL,
            data_source TEXT DEFAULT 'REAL',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(request_id) REFERENCES service_requests(id),
            FOREIGN KEY(mission_id) REFERENCES service_missions(id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_evidence_request ON service_evidence(request_id, created_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_evidence_mission ON service_evidence(mission_id, created_at)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS service_certificates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            certificate_code TEXT UNIQUE NOT NULL,
            request_id INTEGER,
            mission_id INTEGER,
            task_id INTEGER,
            asset_id INTEGER,
            customer_id INTEGER,
            provider_id INTEGER,
            service_name TEXT,
            problem TEXT,
            ai_assessment TEXT,
            provider_diagnosis TEXT,
            work_performed TEXT,
            parts_json TEXT,
            original_estimate REAL,
            parts_cost REAL DEFAULT 0,
            service_fee REAL DEFAULT 0,
            final_amount REAL,
            customer_budget REAL,
            negotiated_amount REAL,
            before_verified INTEGER DEFAULT 0,
            after_verified INTEGER DEFAULT 0,
            customer_confirmed INTEGER DEFAULT 0,
            evidence_count INTEGER DEFAULT 0,
            warranty_days INTEGER DEFAULT 0,
            warranty_until TIMESTAMP,
            issued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            data_source TEXT DEFAULT 'REAL',
            FOREIGN KEY(request_id) REFERENCES service_requests(id),
            FOREIGN KEY(asset_id) REFERENCES assets(id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_certificates_asset ON service_certificates(asset_id, issued_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_certificates_customer ON service_certificates(customer_id, issued_at DESC)")

    # ---------------- MODULE 4: AI Second Opinion ------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS second_opinion_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER,
            mission_id INTEGER,
            task_id INTEGER,
            asset_id INTEGER,
            customer_id INTEGER NOT NULL,
            proposed_repair TEXT,
            quoted_amount REAL,
            provider_diagnosis TEXT,
            customer_question TEXT,
            status TEXT DEFAULT 'PENDING',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(customer_id) REFERENCES users(id),
            FOREIGN KEY(request_id) REFERENCES service_requests(id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_second_opinion_customer ON second_opinion_requests(customer_id, created_at DESC)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS second_opinion_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            opinion_request_id INTEGER NOT NULL UNIQUE,
            summary TEXT,
            explanation TEXT,
            supporting_evidence TEXT,
            information_required TEXT,
            alternatives TEXT,
            recommended_step TEXT,
            confidence REAL DEFAULT 0,
            safety_notes TEXT,
            evidence_used_json TEXT,
            model_used TEXT,
            data_source TEXT DEFAULT 'AI',
            disclaimer TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(opinion_request_id) REFERENCES second_opinion_requests(id) ON DELETE CASCADE
        )
    """)

    # ---------------- MODULE 5: Parts Intelligence -----------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS service_parts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER,
            mission_id INTEGER,
            task_id INTEGER,
            asset_id INTEGER,
            part_name TEXT NOT NULL,
            part_number TEXT,
            compatibility TEXT,
            quantity INTEGER DEFAULT 1,
            unit_price_min REAL,
            unit_price_max REAL,
            currency TEXT DEFAULT 'INR',
            supply_mode TEXT DEFAULT 'PROVIDER',
            availability TEXT DEFAULT 'ESTIMATED',
            availability_note TEXT,
            supplier_note TEXT,
            approval_status TEXT DEFAULT 'NOT_REQUIRED',
            approval_note TEXT,
            warranty_days INTEGER DEFAULT 0,
            status TEXT DEFAULT 'SUGGESTED',
            customer_visible INTEGER DEFAULT 1,
            data_source TEXT DEFAULT 'ESTIMATE',
            created_by_user_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            approved_at TIMESTAMP,
            FOREIGN KEY(request_id) REFERENCES service_requests(id),
            FOREIGN KEY(asset_id) REFERENCES assets(id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_parts_request ON service_parts(request_id, created_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_parts_mission ON service_parts(mission_id, created_at)")

    # ---------------- MODULE 6: Outcome & Recovery -----------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS service_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER NOT NULL UNIQUE,
            asset_id INTEGER,
            customer_id INTEGER NOT NULL,
            provider_id INTEGER,
            outcome_status TEXT DEFAULT 'MONITORING',
            warranty_status TEXT DEFAULT 'NONE',
            warranty_until TIMESTAMP,
            monitored_until TIMESTAMP,
            recurrence_count INTEGER DEFAULT 0,
            last_checked_at TIMESTAMP,
            customer_satisfaction INTEGER,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(request_id) REFERENCES service_requests(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS service_recovery_cases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_code TEXT UNIQUE NOT NULL,
            customer_id INTEGER NOT NULL,
            original_request_id INTEGER,
            new_request_id INTEGER,
            mission_id INTEGER,
            asset_id INTEGER,
            provider_id INTEGER,
            reported_problem TEXT NOT NULL,
            recurrence_category TEXT,
            severity TEXT DEFAULT 'MEDIUM',
            warranty_status TEXT DEFAULT 'UNKNOWN',
            warranty_until TIMESTAMP,
            match_confidence REAL DEFAULT 0,
            match_reason TEXT,
            status TEXT DEFAULT 'OPEN',
            chosen_action TEXT,
            resolution TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            resolved_at TIMESTAMP,
            FOREIGN KEY(customer_id) REFERENCES users(id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_recovery_customer ON service_recovery_cases(customer_id, created_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_recovery_status ON service_recovery_cases(status)")

    # ---------------- MODULE 7: Student Skill Passport -------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS student_skills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_id INTEGER NOT NULL,
            skill_key TEXT NOT NULL,
            skill_label TEXT NOT NULL,
            category TEXT,
            level INTEGER DEFAULT 0,
            status TEXT DEFAULT 'TRAINING',
            supervised_jobs INTEGER DEFAULT 0,
            verified_jobs INTEGER DEFAULT 0,
            certification_id INTEGER,
            notes TEXT,
            updated_by_user_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(provider_id, skill_key),
            FOREIGN KEY(provider_id) REFERENCES providers(id) ON DELETE CASCADE
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_student_skills_provider ON student_skills(provider_id)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS student_skill_verifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_id INTEGER NOT NULL,
            student_skill_id INTEGER,
            skill_key TEXT NOT NULL,
            method TEXT DEFAULT 'ASSESSMENT',
            result TEXT DEFAULT 'VERIFIED',
            evidence TEXT,
            notes TEXT,
            verified_by_user_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(provider_id) REFERENCES providers(id) ON DELETE CASCADE
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_skill_verif_provider ON student_skill_verifications(provider_id, created_at DESC)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS provider_certifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            issuer TEXT,
            credential_id TEXT,
            issued_on TEXT,
            expires_on TEXT,
            document_path TEXT,
            status TEXT DEFAULT 'PENDING',
            reviewed_by_user_id INTEGER,
            reviewed_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(provider_id) REFERENCES providers(id) ON DELETE CASCADE
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_certifications_provider ON provider_certifications(provider_id, status)")

    # ---------------- Catalogue + auth/verification support -------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS service_categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            icon TEXT,
            description TEXT,
            risk_level TEXT DEFAULT 'MEDIUM',
            allowed_provider_types TEXT DEFAULT 'any',
            sort_order INTEGER DEFAULT 100,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS phone_otp_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            phone TEXT NOT NULL,
            purpose TEXT DEFAULT 'SIGNUP',
            code_hash TEXT NOT NULL,
            attempts INTEGER DEFAULT 0,
            expires_at TIMESTAMP NOT NULL,
            consumed_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_otp_user ON phone_otp_codes(user_id, purpose, created_at DESC)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_addresses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            label TEXT,
            address_text TEXT NOT NULL,
            pincode TEXT,
            latitude REAL,
            longitude REAL,
            is_default INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_addresses_user ON user_addresses(user_id, is_default DESC)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notification_preferences (
            user_id INTEGER PRIMARY KEY,
            email_enabled INTEGER DEFAULT 1,
            sms_enabled INTEGER DEFAULT 1,
            push_enabled INTEGER DEFAULT 1,
            whatsapp_enabled INTEGER DEFAULT 0,
            mission_updates INTEGER DEFAULT 1,
            warranty_alerts INTEGER DEFAULT 1,
            marketing INTEGER DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS service_catalog_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER,
            asset_id INTEGER,
            customer_id INTEGER,
            provider_id INTEGER,
            category TEXT,
            message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)


def add_v10_columns(connection):
    # ---- service_requests: link the booking to the lifecycle ------------
    for column, definition in [
        ("asset_id", "INTEGER"),
        ("mission_id", "INTEGER"),
        ("mission_task_id", "INTEGER"),
        ("lifecycle_stage", "TEXT DEFAULT 'MATCH'"),
        ("ai_structured_json", "TEXT"),
        ("budget_customer", "REAL"),
        ("parts_cost", "REAL DEFAULT 0"),
        ("service_fee", "REAL DEFAULT 0"),
        ("provider_diagnosis", "TEXT"),
        ("provider_work_performed", "TEXT"),
        ("before_evidence_paths", "TEXT"),
        ("recovery_case_id", "INTEGER"),
        ("second_opinion_status", "TEXT DEFAULT 'NONE'"),
        ("catalog_metadata_json", "TEXT"),
        ("safety_flags", "TEXT"),
    ]:
        _add_column(connection, "service_requests", column, definition)

    # ---- users: verification, profile, preferences ----------------------
    for column, definition in [
        ("phone_verified", "INTEGER DEFAULT 0"),
        ("phone_verified_at", "TIMESTAMP"),
        ("email_verification_sent_at", "TIMESTAMP"),
        ("address_line", "TEXT"),
        ("city", "TEXT"),
        ("state", "TEXT"),
        ("notification_prefs_json", "TEXT"),
        ("profile_completed", "INTEGER DEFAULT 0"),
        ("last_login_at", "TIMESTAMP"),
        ("verification_note", "TEXT"),
    ]:
        _add_column(connection, "users", column, definition)

    # ---- providers: skill passport / student status ---------------------
    for column, definition in [
        ("provider_type", "TEXT DEFAULT 'professional'"),
        ("student_status", "TEXT DEFAULT 'NONE'"),
        ("service_radius_km", "REAL DEFAULT 10"),
        ("languages", "TEXT"),
        ("equipment", "TEXT"),
        ("response_minutes", "INTEGER"),
        ("kyc_verified_at", "TIMESTAMP"),
        ("verification_score", "INTEGER DEFAULT 0"),
        ("headline", "TEXT"),
        ("availability_json", "TEXT"),
    ]:
        _add_column(connection, "providers", column, definition)

    # ---- services: V10 catalogue metadata -------------------------------
    for column, definition in [
        ("category_key", "TEXT"),
        ("subcategory", "TEXT"),
        ("required_skills", "TEXT"),
        ("required_certification", "TEXT"),
        ("risk_level", "TEXT DEFAULT 'MEDIUM'"),
        ("allowed_provider_types", "TEXT DEFAULT 'any'"),
        ("required_equipment", "TEXT"),
        ("possible_parts", "TEXT"),
        ("estimated_duration", "TEXT"),
        ("pricing_model", "TEXT DEFAULT 'VISIT_PLUS_WORK'"),
        ("keywords", "TEXT"),
        ("asset_types", "TEXT"),
        ("is_popular", "INTEGER DEFAULT 0"),
        ("is_active", "INTEGER DEFAULT 1"),
        ("min_price_hint", "REAL"),
        ("max_price_hint", "REAL"),
    ]:
        _add_column(connection, "services", column, definition)

    # ---- service_warranties: link to asset/passport ---------------------
    for column, definition in [
        ("asset_id", "INTEGER"),
        ("certificate_id", "INTEGER"),
        ("terms_source", "TEXT DEFAULT 'PROVIDER'"),
    ]:
        _add_column(connection, "service_warranties", column, definition)


# =========================================================================
# 2. SEEDING (idempotent)
# =========================================================================

def seed_categories(connection):
    cursor = connection.cursor()
    for category in catalog.CATEGORIES:
        cursor.execute(
            """
            INSERT INTO service_categories(key,name,icon,description,risk_level,allowed_provider_types,sort_order)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(key) DO UPDATE SET
                name=excluded.name,
                icon=excluded.icon,
                description=excluded.description,
                risk_level=excluded.risk_level,
                allowed_provider_types=excluded.allowed_provider_types,
                sort_order=excluded.sort_order
            """,
            (
                category["key"], category["name"], category["icon"], category["description"],
                category["risk_level"], category["allowed_provider_types"], category["sort_order"],
            ),
        )


def _json_list(values):
    import json

    return json.dumps(list(values or []), ensure_ascii=False)


def seed_services(connection):
    """Insert/refresh the V10 catalogue without touching existing history."""
    cursor = connection.cursor()
    category_keys = {c["name"]: c["key"] for c in catalog.CATEGORIES}
    inserted = 0
    enriched = 0

    for entry in catalog.SERVICES:
        existing = cursor.execute(
            "SELECT id FROM services WHERE LOWER(name)=LOWER(?) LIMIT 1", (entry["name"],)
        ).fetchone()
        payload = (
            entry["description"], entry["category"], entry["icon"], category_keys.get(entry["category"]),
            entry["subcategory"], _json_list(entry["required_skills"]), entry["required_certification"],
            entry["risk_level"], entry["allowed_provider_types"], _json_list(entry["required_equipment"]),
            _json_list(entry["possible_parts"]), entry["estimated_duration"], entry["pricing_model"],
            _json_list(entry["keywords"]), _json_list(entry["asset_types"]), entry["popular"],
            entry["min_price"], entry["max_price"], entry["sort_order"],
        )
        if existing:
            cursor.execute(
                """
                UPDATE services SET
                    description=COALESCE(NULLIF(TRIM(description),''), ?),
                    category=?, icon=?, category_key=?, subcategory=?, required_skills=?,
                    required_certification=?, risk_level=?, allowed_provider_types=?,
                    required_equipment=?, possible_parts=?, estimated_duration=?, pricing_model=?,
                    keywords=?, asset_types=?, is_popular=?, min_price_hint=?, max_price_hint=?,
                    sort_order=COALESCE(NULLIF(sort_order,100),?)
                WHERE id=?
                """,
                (*payload, existing["id"]),
            )
            enriched += 1
        else:
            cursor.execute(
                """
                INSERT INTO services(
                    name, description, category, icon, category_key, subcategory, required_skills,
                    required_certification, risk_level, allowed_provider_types, required_equipment,
                    possible_parts, estimated_duration, pricing_model, keywords, asset_types,
                    is_popular, min_price_hint, max_price_hint, sort_order
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (entry["name"], *payload),
            )
            inserted += 1

        service_row = cursor.execute(
            "SELECT id FROM services WHERE LOWER(name)=LOWER(?) LIMIT 1", (entry["name"],)
        ).fetchone()
        if service_row:
            cursor.execute(
                """
                INSERT OR IGNORE INTO service_pricing
                    (service_id, base_price, min_price, max_price, included_km, per_km, platform_fee)
                VALUES(?,?,?,?,3,12,20)
                """,
                (service_row["id"], entry["base_price"], entry["min_price"], entry["max_price"]),
            )

    # Enrich the legacy rows that ship with V9.3 so nothing is left without
    # V10 metadata (they stay in their original category).
    for name, meta in catalog.LEGACY_SERVICE_METADATA.items():
        cursor.execute(
            """
            UPDATE services SET
                subcategory=COALESCE(NULLIF(TRIM(subcategory),''), ?),
                risk_level=COALESCE(NULLIF(TRIM(risk_level),''), ?),
                estimated_duration=COALESCE(NULLIF(TRIM(estimated_duration),''), ?),
                pricing_model=COALESCE(NULLIF(TRIM(pricing_model),''), ?)
            WHERE LOWER(name)=LOWER(?)
            """,
            (meta["subcategory"], meta["risk_level"], meta["estimated_duration"],
             meta["pricing_model"], name),
        )

    # Any service still missing a risk level gets a safe default.
    cursor.execute("UPDATE services SET risk_level='MEDIUM' WHERE risk_level IS NULL OR TRIM(risk_level)=''")
    cursor.execute("UPDATE services SET is_active=1 WHERE is_active IS NULL")
    return inserted, enriched


def seed_asset_types(connection):
    """Asset *types* live in the catalogue module; nothing to persist yet.

    Kept as an explicit function so future admin-managed asset types have a
    single migration hook.
    """
    return len(catalog.ASSET_TYPES)


def seed_demo_passports(connection):
    """Create demo assets for the seeded demo customer, clearly marked DEMO.

    Demo rows are tagged ``data_source='DEMO'`` and every UI surface labels
    them, so demo content can never be mistaken for a real user's asset.
    """
    demo = connection.execute(
        "SELECT id FROM users WHERE LOWER(email)=LOWER('customer@smartserve.demo')"
    ).fetchone()
    if not demo:
        return 0
    created = 0
    demo_assets = [
        ("Air conditioner", "LG", "PS-Q19YNZE", "Living room AC", 2),
        ("Washing machine", "Samsung", "WA70T4560BW", "Utility area washer", 4),
        ("Water purifier", "Kent", "Grand Plus", "Kitchen purifier", 1),
    ]
    for asset_type, brand, model, nickname, years_old in demo_assets:
        row = connection.execute(
            "SELECT id FROM assets WHERE customer_id=? AND asset_type=? AND brand=? LIMIT 1",
            (demo["id"], asset_type, brand),
        ).fetchone()
        if row:
            continue
        asset_uid = "AST-" + _random_code(8)
        purchase_year = datetime.utcnow().year - years_old
        connection.execute(
            """
            INSERT INTO assets(
                asset_uid, customer_id, asset_type, category, brand, model, nickname,
                purchase_date, warranty_start, warranty_end, health_score, status,
                location_label, notes, data_source
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                asset_uid, demo["id"], asset_type, "Appliance & Electronics", brand, model, nickname,
                f"{purchase_year}-06-15", f"{purchase_year}-06-15", f"{purchase_year + 1}-06-14",
                82, "ACTIVE", "Home", "Demo passport created during V10 seeding.", "DEMO",
            ),
        )
        asset_id = connection.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        connection.execute(
            "INSERT INTO asset_qr_tokens(asset_id, token) VALUES(?,?)", (asset_id, secrets.token_urlsafe(24))
        )
        connection.execute(
            """
            INSERT INTO asset_maintenance_schedule(asset_id, title, description, due_date, interval_days, source)
            VALUES(?,?,?,?,?, 'SYSTEM')
            """,
            (asset_id, f"{asset_type} preventive service",
             "Recommended periodic maintenance from the SmartServe care plan.",
             f"{datetime.utcnow().year}-{((datetime.utcnow().month % 12) + 1):02d}-01", 180),
        )
        created += 1
    return created


def seed_demo_student_skills(connection):
    """Give one demo provider a student skill passport (marked DEMO)."""
    provider = connection.execute(
        """SELECT p.id FROM providers p JOIN users u ON u.id=p.user_id
           WHERE LOWER(u.email)=LOWER('meera.electrical@smartserve.demo') LIMIT 1"""
    ).fetchone()
    if not provider:
        return 0
    pid = int(provider["id"])
    connection.execute(
        "UPDATE providers SET provider_type='student', student_status='TRAINEE', languages='English, Hindi, Kannada' WHERE id=?",
        (pid,),
    )
    skills = [
        ("electrical_basics", "Electrical — basic wiring", "Electrical", 70, "VERIFIED", 12, 6),
        ("switch_replacement", "Switch & socket replacement", "Electrical", 85, "VERIFIED", 24, 9),
        ("fan_installation", "Fan installation", "Electrical", 78, "VERIFIED", 15, 5),
        ("lighting_installation", "Lighting & fixture installation", "Electrical", 64, "SUPERVISED", 6, 1),
        ("high_voltage_work", "High-voltage / 3-phase work", "Electrical", 0, "RESTRICTED", 0, 0),
        ("industrial_wiring", "Industrial wiring", "Electrical", 0, "RESTRICTED", 0, 0),
    ]
    for key, label, category, level, status, verified_jobs, supervised_jobs in skills:
        connection.execute(
            """
            INSERT INTO student_skills(provider_id, skill_key, skill_label, category, level, status,
                verified_jobs, supervised_jobs, notes, updated_by_user_id)
            VALUES(?,?,?,?,?,?,?,?,?,NULL)
            ON CONFLICT(provider_id, skill_key) DO UPDATE SET
                skill_label=excluded.skill_label, level=excluded.level, status=excluded.status
            """,
            (pid, key, label, category, level, status, verified_jobs, supervised_jobs,
             "Demo skill passport row seeded by SmartServe V10."),
        )
    return len(skills)


def seed_demo_provider_catalog_services(connection):
    """Link demo providers to the V10 catalogue so the demo flow works.

    Demo accounts (``*.demo``) are the only accounts touched. Real providers
    are never auto-subscribed to services. Demo certifications are labelled
    ``DEMO SEED`` in the issuer field so nobody mistakes them for real
    credentials.
    """
    mapping = {
        "arjun.plumbing@smartserve.demo": ("home_repair", ["Plumbing", "General handyman", "Drainage"]),
        "ravi.carpentry@smartserve.demo": ("home_repair", ["Carpentry", "Doors and windows", "Furniture assembly"]),
        "meera.electrical@smartserve.demo": ("home_repair", ["Electrical", "General handyman"]),
        "neha.cleaning@smartserve.demo": ("cleaning_pest", ["Home cleaning", "Deep cleaning", "Kitchen cleaning", "Bathroom cleaning", "General pest control"]),
        "vikram.appliance@smartserve.demo": ("appliance_electronics", ["Refrigerator", "Washing machine", "Air conditioner", "Microwave"]),
        "sana.mobile@smartserve.demo": ("appliance_electronics", ["Television", "Networking", "Printer"]),
        "kiran.laptop@smartserve.demo": ("appliance_electronics", ["Laptop", "Desktop computer"]),
        "dev.it@smartserve.demo": ("education_digital", ["Digital help", "Device setup", "Training"]),
    }
    category_names = {category["key"]: category["name"] for category in catalog.CATEGORIES}
    linked = 0
    certified = 0
    for email, (category_key, hints) in mapping.items():
        provider = connection.execute(
            """SELECT p.id FROM providers p JOIN users u ON u.id=p.user_id
               WHERE LOWER(u.email)=LOWER(?) LIMIT 1""",
            (email,),
        ).fetchone()
        if not provider:
            continue
        provider_id = int(provider["id"])
        category_name = category_names.get(category_key)
        rows = connection.execute(
            "SELECT id, name, subcategory, required_certification FROM services WHERE category=? AND is_active IS NOT 0",
            (category_name,),
        ).fetchall()
        chosen = [row for row in rows if any(hint.lower() in (row["subcategory"] or "").lower() for hint in hints)]
        if not chosen:
            chosen = rows[:8]
        for row in chosen[:12]:
            connection.execute(
                "INSERT OR IGNORE INTO provider_services(provider_id, service_id) VALUES(?,?)",
                (provider_id, int(row["id"])),
            )
            linked += 1
            certification = row["required_certification"]
            if certification:
                existing = connection.execute(
                    "SELECT id FROM provider_certifications WHERE provider_id=? AND LOWER(name)=LOWER(?)",
                    (provider_id, certification),
                ).fetchone()
                if not existing:
                    connection.execute(
                        """INSERT INTO provider_certifications(provider_id, name, issuer, credential_id, issued_on, expires_on,
                               status, reviewed_at)
                           VALUES(?,?,?,?,date('now','-1 year'),date('now','+2 years'),'VERIFIED',CURRENT_TIMESTAMP)""",
                        (provider_id, certification, "DEMO SEED — sample credential (not a real licence)",
                         f"DEMO-{provider_id}-{int(row['id'])}"),
                    )
                    certified += 1
    return {"services_linked": linked, "certifications": certified}


# =========================================================================
# 3. ENTRY POINT
# =========================================================================

def run_v10_migrations(connection=None, verbose=True):
    """Run every V10 migration. Safe to call on every application start."""
    from database import get_db_connection

    own_connection = connection is None
    connection = connection or get_db_connection()
    try:
        create_v10_tables(connection)
        add_v10_columns(connection)
        seed_categories(connection)
        inserted, enriched = seed_services(connection)
        seed_asset_types(connection)
        demo_assets = seed_demo_passports(connection)
        demo_skills = seed_demo_student_skills(connection)
        demo_links = seed_demo_provider_catalog_services(connection)
        connection.execute("UPDATE users SET phone_verified=0 WHERE phone_verified IS NULL")
        connection.commit()
        if verbose:
            print(
                f"[V10] migrations complete — services inserted={inserted} enriched={enriched} "
                f"demo_assets={demo_assets} demo_skill_rows={demo_skills} "
                f"demo_provider_services={demo_links['services_linked']} demo_certifications={demo_links['certifications']}"
            )
        return {
            "services_inserted": inserted,
            "services_enriched": enriched,
            "demo_assets": demo_assets,
            "demo_skill_rows": demo_skills,
            "demo_provider_services": demo_links["services_linked"],
            "demo_certifications": demo_links["certifications"],
        }
    finally:
        if own_connection:
            connection.close()


if __name__ == "__main__":  # pragma: no cover - manual migration helper
    run_v10_migrations()
