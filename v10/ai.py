"""SmartServe V10 — structured AI layer.

Reuses the existing SmartServe V9.3 Gemini integration (``ai_service``) instead
of replacing it. Everything here returns **structured dictionaries** and always
carries a ``data_source`` marker:

* ``AI``          — a real Gemini response.
* ``RULE_BASED``  — deterministic keyword/catalogue reasoning used when no
                    Gemini key is configured or the model is unavailable.

The rule-based path is never presented to the user as an AI diagnosis. It is
labelled "SmartServe preliminary assessment (offline rules)" everywhere.
"""

from __future__ import annotations

import json
import re

try:  # Reuse the exact same client/model configuration as V9.3.
    from ai_service import (
        DEFAULT_MODEL,
        FALLBACK_MODEL,
        SECOND_FALLBACK_MODEL,
        _client,
        _extract_json,
    )
except Exception:  # pragma: no cover - defensive import
    DEFAULT_MODEL = FALLBACK_MODEL = SECOND_FALLBACK_MODEL = ""
    _client = None
    _extract_json = None

from . import catalog

AI_DISCLAIMER = (
    "SmartServe AI output is a preliminary assessment, not a guaranteed diagnosis. "
    "A qualified professional must confirm the fault on site before costly repairs."
)

OFFLINE_LABEL = "SmartServe preliminary assessment (offline rules)"

SAFETY_KEYWORDS = {
    "spark": "Electrical sparking reported — switch off the circuit at the MCB and avoid touching wet fittings.",
    "shock": "Electric shock risk reported — do not touch the affected fitting; isolate the circuit immediately.",
    "gas": "Gas smell reported — do not switch lights on/off, ventilate the room and call the emergency helpline.",
    "smoke": "Smoke reported — disconnect power if it is safe to do so and leave the area.",
    "fire": "Fire risk reported — evacuate and contact emergency services (112) before arranging a repair.",
    "collapse": "Structural movement reported — keep clear of the area until a professional inspects it.",
    "short circuit": "Short-circuit risk — isolate the circuit before any work begins.",
    "water on electric": "Water near live electrical fittings — isolate power first, plumbing work second.",
}


# ---------------------------------------------------------------------------
# Low level helpers
# ---------------------------------------------------------------------------

def _models():
    return list(dict.fromkeys(
        [m.strip() for m in [DEFAULT_MODEL, FALLBACK_MODEL, SECOND_FALLBACK_MODEL] if m and m.strip()]
    ))


def gemini_available():
    import os

    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key or key.startswith("YOUR_") or key.startswith("your_"):
        return False
    return _client is not None


def _gemini_json(prompt, image_path=None, temperature=0.2):
    """Ask Gemini for a JSON object, reusing V9.3 model fallback behaviour."""
    if not gemini_available():
        raise RuntimeError("Gemini is not configured")

    from google.genai import types  # type: ignore

    contents = [prompt]
    if image_path:
        try:
            import os

            with open(image_path, "rb") as image_file:
                data = image_file.read()
            ext = os.path.splitext(image_path)[1].lower()
            mime = {
                ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"
            }.get(ext, "image/jpeg")
            contents.append(types.Part.from_bytes(data=data, mime_type=mime))
        except Exception as exc:  # pragma: no cover - image is optional
            print("[V10 AI] image attach failed:", repr(exc))

    client = _client()
    last_error = None
    models = _models()
    for index, model in enumerate(models):
        try:
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(
                    temperature=temperature, response_mime_type="application/json"
                ),
            )
            return _extract_json(response.text), model
        except Exception as exc:  # noqa: BLE001 - mirror V9.3 fallback behaviour
            last_error = exc
            message = str(exc).lower()
            transient = any(token in message for token in [
                "429", "408", "500", "502", "503", "504", "unavailable", "resource exhausted",
                "temporarily", "overloaded", "deadline exceeded", "timeout",
            ])
            model_error = any(token in message for token in ["not found", "404", "unsupported"])
            if index < len(models) - 1 and (transient or model_error):
                print(f"[V10 AI] model {model} failed, trying {models[index + 1]}")
                continue
            break
    raise RuntimeError(f"Gemini request failed: {last_error}")


def _as_list(value, limit=8):
    if value is None:
        return []
    if isinstance(value, str):
        parts = [p.strip(" •-–\t") for p in re.split(r"[\n;]+|,(?=\s)", value)]
        return [p for p in parts if p][:limit]
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            text = str(item).strip()
            if text:
                out.append(text)
        return out[:limit]
    return [str(value)]


def _match_catalog_services(text, limit=5):
    """Keyword → catalogue matching used by the offline rules engine."""
    text = (text or "").lower()
    scored = []
    for service in catalog.SERVICES:
        score = 0
        for keyword in service["keywords"]:
            if keyword and keyword in text:
                score += 3 if len(keyword) > 4 else 2
        name = service["name"].lower()
        if name in text:
            score += 6
        for token in name.split():
            if len(token) > 3 and token in text:
                score += 1
        if score:
            scored.append((score, service))
    scored.sort(key=lambda item: (-item[0], item[1]["sort_order"]))
    return [service for _, service in scored[:limit]]


def _price_range(service, difficulty="Medium"):
    if not service:
        return {"min": 300, "max": 1200, "currency": "INR"}
    minimum = float(service.get("min_price") or 199)
    maximum = float(service.get("max_price") or 1499)
    factor = {"Easy": 0.7, "Medium": 1.0, "Hard": 1.6}.get(difficulty, 1.0)
    return {"min": int(minimum * factor), "max": int(maximum * factor), "currency": "INR"}


def _safety_notes(*texts):
    blob = " ".join(str(t or "").lower() for t in texts)
    notes = []
    for keyword, note in SAFETY_KEYWORDS.items():
        if keyword in blob and note not in notes:
            notes.append(note)
    return notes


# ---------------------------------------------------------------------------
# 1. Problem understanding (UNDERSTAND + DIAGNOSE stages)
# ---------------------------------------------------------------------------

def analyze_problem(
    service_name,
    description,
    asset=None,
    history=None,
    image_path=None,
    catalog_hints=None,
):
    """Structured problem understanding used by the mission engine.

    Returns a dict with the V10 contract::

        {problem, possible_causes[], required_services[], possible_parts[],
         confidence, safety_notes[], recommended_next_step, difficulty,
         estimated_price_range{}, risk_level, data_source, model_used}
    """
    asset = asset or {}
    history = history or []
    history_text = "; ".join(
        f"{row.get('service_name') or 'Service'} on {row.get('service_date') or 'a past visit'} "
        f"({row.get('problem') or 'no problem text'})"
        for row in history[:5]
    ) or "No previous services recorded for this asset."

    asset_text = ", ".join(
        f"{key}: {asset.get(key)}" for key in ("asset_type", "brand", "model", "purchase_date", "warranty_end")
        if asset.get(key)
    ) or "No asset linked."

    prompt = f"""
You are SmartServe's asset-aware service diagnosis assistant.
Selected service: {service_name or 'Not specified'}
Customer's problem: {description}
Asset on record: {asset_text}
Previous service history: {history_text}

Return ONLY one valid JSON object with exactly these keys:
problem, possible_causes, required_services, possible_parts, confidence, safety_notes,
recommended_next_step, difficulty, estimated_min, estimated_max, risk_level

Rules:
- problem: one short sentence restating the customer's issue in neutral language.
- possible_causes: array of 2-5 short strings, ordered most likely first.
- required_services: array of SmartServe service names from the catalog that may be needed.
- possible_parts: array of short part names that *may* be involved (may be empty).
- confidence: number between 0 and 1 reflecting how certain you are from the text alone.
- safety_notes: array of short safety warnings (empty when nothing is dangerous).
- recommended_next_step: one short sentence.
- difficulty: exactly Easy, Medium or Hard.
- estimated_min / estimated_max: integers in INR for a typical repair, not a quote.
- risk_level: one of LOW, MEDIUM, HIGH, RESTRICTED.
Never claim certainty. Never guarantee a diagnosis. If information is missing, lower confidence.
"""

    try:
        payload, model = _gemini_json(prompt, image_path=image_path)
        result = {
            "problem": str(payload.get("problem") or description)[:600],
            "possible_causes": _as_list(payload.get("possible_causes")) or _offline_causes(description),
            "required_services": _as_list(payload.get("required_services")) or _offline_services(service_name, description),
            "possible_parts": _as_list(payload.get("possible_parts")) or _offline_parts(service_name, description),
            "confidence": _clamp_float(payload.get("confidence"), 0.0, 1.0, 0.55),
            "safety_notes": _as_list(payload.get("safety_notes")) or _safety_notes(description),
            "recommended_next_step": str(payload.get("recommended_next_step") or "Book an on-site inspection to confirm the cause.")[:400],
            "difficulty": _normalise_difficulty(payload.get("difficulty")),
            "estimated_price_range": {
                "min": _clamp_int(payload.get("estimated_min"), 0, 10_000_000, 300),
                "max": _clamp_int(payload.get("estimated_max"), 0, 10_000_000, 1500),
                "currency": "INR",
            },
            "risk_level": _normalise_risk(payload.get("risk_level"), service_name),
            "data_source": "AI",
            "model_used": model,
            "disclaimer": AI_DISCLAIMER,
        }
        if result["estimated_price_range"]["max"] < result["estimated_price_range"]["min"]:
            result["estimated_price_range"]["max"] = result["estimated_price_range"]["min"] + 500
        return result
    except Exception as exc:
        print("[V10 AI] analyze_problem falling back to rules:", repr(exc))
        return offline_analysis(service_name, description, asset=asset)


def _offline_causes(description):
    text = (description or "").lower()
    causes = []
    table = [
        ("not cooling", "Refrigerant loss or airflow restriction"),
        ("cooling", "Airflow restriction or low refrigerant charge"),
        ("leak", "Worn seal, joint or pipe damage"),
        ("noise", "Loose mounting, worn bearing or debris"),
        ("drain", "Blocked drain line or failed drain pump"),
        ("spark", "Loose connection or damaged insulation"),
        ("not starting", "Power supply, switch or control-board fault"),
        ("vibration", "Unbalanced load or worn mounts"),
        ("water", "Water ingress or drainage fault"),
        ("heat", "Overheating due to blocked airflow or worn part"),
    ]
    for keyword, cause in table:
        if keyword in text and cause not in causes:
            causes.append(cause)
    if not causes:
        causes = [
            "Component wear consistent with the reported symptom",
            "Electrical or mechanical fault that needs on-site confirmation",
        ]
    return causes


def _offline_services(service_name, description):
    matches = _match_catalog_services(f"{description} {service_name or ''}", limit=4)
    names = [m["name"] for m in matches]
    if service_name and service_name not in names:
        names.insert(0, service_name)
    return names[:5]


def _offline_parts(service_name, description):
    matches = _match_catalog_services(f"{description} {service_name or ''}", limit=2)
    parts = []
    for match in matches:
        for part in match["possible_parts"]:
            if part not in parts:
                parts.append(part)
    return parts[:5]


def _clamp_float(value, low, high, default):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _clamp_int(value, low, high, default):
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _normalise_difficulty(value):
    text = str(value or "").strip().capitalize()
    return text if text in {"Easy", "Medium", "Hard"} else "Medium"


def _normalise_risk(value, service_name=None):
    text = str(value or "").strip().upper()
    if text in catalog.RISK_LEVELS:
        return text
    match = next((s for s in catalog.SERVICES if s["name"] == service_name), None)
    return match["risk_level"] if match else catalog.RISK_MEDIUM


def offline_analysis(service_name, description, asset=None):
    """Deterministic fallback analysis (clearly labelled, never called 'AI')."""
    matches = _match_catalog_services(f"{description} {service_name or ''}", limit=1)
    service = matches[0] if matches else None
    difficulty = "Medium"
    text = (description or "").lower()
    if any(word in text for word in ("spark", "shock", "gas", "smoke", "fire", "collapse")):
        difficulty = "Hard"
    elif any(word in text for word in ("noise", "cleaning", "filter", "service", "tighten")):
        difficulty = "Easy"
    causes = _offline_causes(description)
    return {
        "problem": (description or service_name or "Service request")[:600],
        "possible_causes": causes,
        "required_services": _offline_services(service_name, description),
        "possible_parts": _offline_parts(service_name, description),
        "confidence": 0.35,
        "safety_notes": _safety_notes(description),
        "recommended_next_step": "A professional on-site inspection is required to confirm the cause.",
        "difficulty": difficulty,
        "estimated_price_range": _price_range(service, difficulty),
        "risk_level": (service or {}).get("risk_level", catalog.RISK_MEDIUM),
        "data_source": "RULE_BASED",
        "model_used": None,
        "label": OFFLINE_LABEL,
        "disclaimer": (
            "Gemini is not configured on this deployment, so SmartServe used its offline rules engine. "
            "This is a preliminary classification, not a diagnosis."
        ),
    }


# ---------------------------------------------------------------------------
# 2. Mission decomposition (PLAN stage)
# ---------------------------------------------------------------------------

def decompose_mission(problem, service_name=None, asset=None, analysis=None):
    """Split a customer problem into ordered mission tasks."""
    analysis = analysis or {}
    prompt = f"""
You are SmartServe's service mission planner.
Customer's problem: {problem}
Selected service (if any): {service_name or 'not chosen'}
Asset: {(asset or {}).get('asset_type') or 'not linked'}
AI causes already identified: {', '.join(analysis.get('possible_causes') or []) or 'none'}

Return ONLY one valid JSON object with keys: title, required_services, risk_level, tasks
where tasks is an array of objects with keys: title, description, service_name, depends_on_order, estimated_cost

Rules:
- 2 to 5 tasks, in the order a professional would actually perform them.
- depends_on_order is the 1-based order index of the task that must finish first, or 0 for none.
- service_name must be a SmartServe catalog service name where possible.
- Keep titles under 60 characters and descriptions under 200 characters.
- Include a final verification task when multiple trades are involved.
"""
    try:
        payload, model = _gemini_json(prompt, temperature=0.3)
        tasks = payload.get("tasks") or []
        parsed = []
        for index, task in enumerate(tasks[:6], start=1):
            if not isinstance(task, dict):
                continue
            parsed.append({
                "task_order": index,
                "title": str(task.get("title") or f"Step {index}")[:120],
                "description": str(task.get("description") or "")[:400],
                "service_name": str(task.get("service_name") or service_name or "")[:120],
                "depends_on_order": _clamp_int(task.get("depends_on_order"), 0, 6, max(0, index - 1)),
                "estimated_cost": _clamp_int(task.get("estimated_cost"), 0, 1_000_000, 0) or None,
            })
        if len(parsed) >= 2:
            return {
                "title": str(payload.get("title") or problem[:80])[:160],
                "required_services": _as_list(payload.get("required_services")) or _offline_services(service_name, problem),
                "risk_level": _normalise_risk(payload.get("risk_level"), service_name),
                "tasks": parsed,
                "data_source": "AI",
                "model_used": model,
            }
    except Exception as exc:
        print("[V10 AI] decompose_mission falling back to templates:", repr(exc))
    return offline_mission(problem, service_name, analysis=analysis)


def offline_mission(problem, service_name=None, analysis=None):
    """Template-driven mission decomposition (used when AI is unavailable)."""
    text = f"{problem or ''} {service_name or ''}".lower()
    for template in catalog.MISSION_TEMPLATES:
        if any(keyword in text for keyword in template["match"]):
            tasks = []
            for index, (title, svc, description, depends) in enumerate(template["tasks"], start=1):
                tasks.append({
                    "task_order": index,
                    "title": title,
                    "description": description,
                    "service_name": svc,
                    "depends_on_order": depends,
                    "estimated_cost": None,
                })
            return {
                "title": template["title"],
                "required_services": [task["service_name"] for task in tasks],
                "risk_level": _normalise_risk(None, service_name),
                "tasks": tasks,
                "data_source": "RULE_BASED",
                "label": OFFLINE_LABEL,
                "model_used": None,
            }

    causes = (analysis or {}).get("possible_causes") or _offline_causes(problem)
    title, description = catalog.DEFAULT_MISSION_TEMPLATE
    tasks = [
        {
            "task_order": 1,
            "title": "On-site inspection & diagnosis",
            "description": f"Confirm the reported problem. Likely causes: {', '.join(causes[:2])}.",
            "service_name": service_name or "",
            "depends_on_order": 0,
            "estimated_cost": None,
        },
        {
            "task_order": 2,
            "title": "Repair / service execution",
            "description": "Carry out the agreed repair once the cause is confirmed and the price is approved.",
            "service_name": service_name or "",
            "depends_on_order": 1,
            "estimated_cost": None,
        },
        {
            "task_order": 3,
            "title": "Post-service verification",
            "description": "Test the equipment, upload after-service evidence and confirm the fix with the customer.",
            "service_name": service_name or "",
            "depends_on_order": 2,
            "estimated_cost": None,
        },
    ]
    return {
        "title": title if not service_name else f"{service_name} — {title}",
        "required_services": [service_name] if service_name else [],
        "risk_level": _normalise_risk(None, service_name),
        "tasks": tasks,
        "data_source": "RULE_BASED",
        "label": OFFLINE_LABEL,
        "model_used": None,
    }


# ---------------------------------------------------------------------------
# 3. Parts intelligence (Module 5)
# ---------------------------------------------------------------------------

def suggest_parts(service_name, problem, analysis=None, asset=None, limit=6):
    """Return candidate parts. ``data_source`` is always ``ESTIMATE`` here.

    SmartServe has no supplier inventory API, so these are catalogue-derived
    estimates and are labelled as such throughout the UI.
    """
    analysis = analysis or {}
    matches = _match_catalog_services(f"{problem or ''} {service_name or ''}", limit=3)
    if not matches and service_name:
        matches = [s for s in catalog.SERVICES if s["name"] == service_name]
    parts = []
    for service in matches:
        for part in service["possible_parts"]:
            if part.lower() in {"", "n/a"}:
                continue
            if any(p["part_name"].lower() == part.lower() for p in parts):
                continue
            parts.append({
                "part_name": part,
                "compatibility": (asset or {}).get("model") or service["name"],
                "part_number": None,
                "unit_price_min": round(float(service["min_price"]) * 0.25),
                "unit_price_max": round(float(service["max_price"]) * 0.35),
                "quantity": 1,
                "availability": "ESTIMATED",
                "availability_note": "Estimated / simulated availability — SmartServe has no live supplier feed.",
                "supplier_note": None,
                "supply_mode": "PROVIDER",
                "approval_status": "PENDING",
                "warranty_days": 30,
                "data_source": "ESTIMATE",
            })
        if len(parts) >= limit:
            break
    for part in (analysis.get("possible_parts") or [])[:2]:
        if not any(p["part_name"].lower() == part.lower() for p in parts):
            parts.append({
                "part_name": part,
                "compatibility": (asset or {}).get("model") or "Confirm on site",
                "part_number": None,
                "unit_price_min": None,
                "unit_price_max": None,
                "quantity": 1,
                "availability": "ESTIMATED",
                "availability_note": "Estimated / simulated availability — confirm with the provider.",
                "supplier_note": None,
                "supply_mode": "PROVIDER",
                "approval_status": "PENDING",
                "warranty_days": 0,
                "data_source": "ESTIMATE",
            })
    return parts[:limit]


# ---------------------------------------------------------------------------
# 4. Second opinion (Module 4)
# ---------------------------------------------------------------------------

SECOND_OPINION_DISCLAIMER = (
    "SmartServe second opinion is neutral decision support based on the information available. "
    "It is not an accusation about any professional and it is not a substitute for an on-site inspection."
)


def second_opinion(context):
    """Neutral, structured second opinion.

    ``context`` keys: service_name, problem, proposal, quoted_amount, provider_diagnosis,
    asset, history, previous_parts, warranty_status, evidence, ai_analysis.
    """
    context = context or {}
    proposal = (context.get("proposal") or "").strip() or "Repair proposed by the provider"
    quoted = context.get("quoted_amount")
    quoted_text = f"₹{int(float(quoted)):,}" if quoted not in (None, "", 0) else "not stated"
    history = context.get("history") or []
    history_text = "; ".join(
        f"{row.get('service_name') or 'Service'} ({row.get('service_date') or 'date unknown'}): {row.get('problem') or 'no problem recorded'}"
        for row in history[:5]
    ) or "No earlier service recorded for this asset."
    parts_text = ", ".join(context.get("previous_parts") or []) or "none recorded"
    analysis = context.get("ai_analysis") or {}

    prompt = f"""
You are SmartServe's neutral second-opinion assistant helping a customer decide whether
to approve a proposed repair. You must never accuse or imply dishonesty by any professional.

Service: {context.get('service_name') or 'not specified'}
Customer's original problem: {context.get('problem') or 'not provided'}
Asset: {(context.get('asset') or {}).get('asset_type') or 'not linked'} {(context.get('asset') or {}).get('brand') or ''} {(context.get('asset') or {}).get('model') or ''}
Provider's proposed repair: {proposal}
Provider's diagnosis: {context.get('provider_diagnosis') or 'not provided'}
Quoted amount: {quoted_text}
Previous service history: {history_text}
Previously replaced parts: {parts_text}
Warranty status: {context.get('warranty_status') or 'unknown'}
Evidence available: {context.get('evidence') or 'customer description only'}
Preliminary AI assessment: {json.dumps(analysis)[:1200] if analysis else 'none'}

Return ONLY one valid JSON object with keys:
explanation, supporting_evidence, information_required, alternatives, recommended_step, confidence, safety_notes

Rules:
- Use neutral, respectful, decision-support language at all times.
- explanation: what could reasonably explain the proposed repair (2-4 sentences).
- supporting_evidence: array of items that support the proposal, from the data above.
- information_required: array of specific diagnostic facts still missing before approving.
- alternatives: array of other plausible explanations or cheaper first steps to rule out.
- recommended_step: one concrete next diagnostic step (not a command to refuse the repair).
- confidence: number 0-1. If below 0.5, state in recommended_step that a professional on-site inspection is required.
- safety_notes: array of safety warnings, empty when none.
"""
    try:
        payload, model = _gemini_json(prompt, temperature=0.2)
        confidence = _clamp_float(payload.get("confidence"), 0.0, 1.0, 0.5)
        step = str(payload.get("recommended_step") or "").strip()
        if confidence < 0.5 and "inspection" not in step.lower():
            step = (step + " " if step else "") + "A professional on-site inspection is required before approving this repair."
        return {
            "summary": f"Second opinion for: {proposal}"[:250],
            "explanation": str(payload.get("explanation") or "")[:2000],
            "supporting_evidence": _as_list(payload.get("supporting_evidence")) or [],
            "information_required": _as_list(payload.get("information_required")) or _offline_information_required(context),
            "alternatives": _as_list(payload.get("alternatives")) or [],
            "recommended_step": step or "Ask the provider to document the diagnostic readings before the repair.",
            "confidence": confidence,
            "safety_notes": _as_list(payload.get("safety_notes")) or _safety_notes(context.get("problem")),
            "data_source": "AI",
            "model_used": model,
            "disclaimer": SECOND_OPINION_DISCLAIMER,
        }
    except Exception as exc:
        print("[V10 AI] second_opinion falling back to rules:", repr(exc))
        return offline_second_opinion(context)


def _offline_information_required(context):
    items = []
    if not context.get("provider_diagnosis"):
        items.append("The provider's written diagnosis (what was measured and observed).")
    if not context.get("evidence"):
        items.append("Photos or readings from the provider's on-site diagnosis.")
    if not (context.get("history") or []):
        items.append("Any earlier repair history for this asset.")
    if context.get("quoted_amount") and float(context.get("quoted_amount") or 0) > 5000:
        items.append("An itemised breakdown of the quoted amount (parts, labour, taxes).")
    items.append("Confirmation of the warranty terms that apply to this repair.")
    return items


def offline_second_opinion(context):
    context = context or {}
    analysis = context.get("ai_analysis") or {}
    causes = analysis.get("possible_causes") or _offline_causes(context.get("problem"))
    history = context.get("history") or []
    supporting = []
    if history:
        supporting.append(f"{len(history)} earlier service record(s) exist for this asset.")
        if any((row.get("problem") or "").lower()[:20] in (context.get("problem") or "").lower() for row in history):
            supporting.append("A previous service on this asset had a similar reported problem.")
    if analysis.get("possible_causes"):
        supporting.append("The preliminary assessment listed causes consistent with the proposed repair.")
    if context.get("warranty_status") == "ACTIVE":
        supporting.append("An active warranty exists, so a repeat fault may be covered.")
    return {
        "summary": f"Second opinion for: {(context.get('proposal') or 'proposed repair')[:200]}",
        "explanation": (
            "The proposed repair is one reasonable way to address the reported symptom. "
            f"Possible explanations for the symptom include: {', '.join(causes[:3])}. "
            "Without on-site measurements SmartServe cannot confirm that this is the only cause."
        )[:2000],
        "supporting_evidence": supporting or ["The provider inspected the asset on site."],
        "information_required": _offline_information_required(context),
        "alternatives": [
            "A lower-cost diagnostic step (cleaning, testing pressures or readings) before replacing the part.",
            "Checking whether an active warranty or a recent repair covers the same fault.",
        ],
        "recommended_step": (
            "Ask the provider for the measured readings and an itemised quote before approving the repair. "
            "If they cannot be provided, request an independent on-site inspection."
        ),
        "confidence": 0.3,
        "safety_notes": _safety_notes(context.get("problem")),
        "data_source": "RULE_BASED",
        "label": OFFLINE_LABEL,
        "model_used": None,
        "disclaimer": SECOND_OPINION_DISCLAIMER,
    }


# ---------------------------------------------------------------------------
# 5. Recovery classification (Module 6)
# ---------------------------------------------------------------------------

RECOVERY_KEYWORDS = {
    "same_problem": ["same problem", "again", "returned", "recurring", "still not", "not fixed", "repeat"],
    "new_problem": ["different", "new problem", "another issue", "unrelated"],
    "damage_during_service": ["damaged", "broke during", "scratch", "while servicing", "dent"],
    "parts_failure": ["part failed", "replaced part", "spare failed", "new part"],
}


def classify_recovery(reported_problem, original_problem="", original_service=""):
    """Classify whether a new complaint matches the previous service.

    Returns ``{category, confidence, reason, data_source}``. Never accuses the
    provider — it only measures textual/asset overlap.
    """
    reported = (reported_problem or "").lower()
    original = f"{original_problem or ''} {original_service or ''}".lower()

    category = "new_problem"
    for key, keywords in RECOVERY_KEYWORDS.items():
        if any(keyword in reported for keyword in keywords):
            category = key
            break

    reported_words = {w for w in re.findall(r"[a-z]{4,}", reported)}
    original_words = {w for w in re.findall(r"[a-z]{4,}", original)}
    overlap = reported_words & original_words
    ignoring = {"service", "problem", "issue", "again", "returned", "still", "fixed", "please", "work"}
    meaningful = {w for w in overlap if w not in ignoring}
    ratio = len(meaningful) / max(1, len(reported_words))
    confidence = round(min(0.95, 0.25 + ratio * 1.6), 2)
    if category == "same_problem" and confidence < 0.6:
        confidence = min(0.9, confidence + 0.2)
    if category == "new_problem":
        confidence = min(confidence, 0.6)

    reason_bits = []
    if category == "same_problem":
        reason_bits.append("The customer's wording suggests the same problem has returned.")
    elif category == "damage_during_service":
        reason_bits.append("The complaint mentions damage, which needs a separate review.")
    elif category == "parts_failure":
        reason_bits.append("The complaint may relate to a part used in the earlier repair.")
    else:
        reason_bits.append("The complaint does not clearly match the earlier service.")
    if meaningful:
        reason_bits.append("Matching terms: " + ", ".join(sorted(meaningful)[:6]) + ".")

    return {
        "category": category,
        "confidence": confidence,
        "reason": " ".join(reason_bits),
        "data_source": "RULE_BASED",
    }


def maintenance_suggestions(asset_type, warranty_end=None):
    """Standard preventive-maintenance plan per asset type (SYSTEM source)."""
    plans = {
        "Air conditioner": ("AC preventive service", 180, "Clean filters and coils, check gas pressure and drainage."),
        "Refrigerator": ("Refrigerator health check", 365, "Inspect door gasket, condenser coils and temperature stability."),
        "Washing machine": ("Washing machine service", 180, "Clean drum/filter, check inlet and drain lines."),
        "Water purifier": ("Filter & membrane service", 180, "Replace sediment/carbon filters and check TDS output."),
        "Geyser": ("Geyser safety check", 365, "Inspect heating element, thermostat and safety valve."),
        "Chimney": ("Chimney deep clean", 180, "Degrease baffle filters and clean the blower assembly."),
        "Car": ("Vehicle periodic service", 180, "Oil, filters, brakes and battery health."),
        "Two-wheeler": ("Two-wheeler service", 120, "Engine oil, chain, brakes and battery health."),
        "Laptop": ("Laptop thermal service", 365, "Clean cooling system and refresh thermal paste."),
        "CCTV system": ("CCTV maintenance", 180, "Clean lenses, verify recording and check power supply."),
    }
    default = ("General preventive check", 365, "Periodic inspection to catch problems early.")
    title, days, description = plans.get(asset_type, default)
    if warranty_end:
        description += f" Current warranty runs until {warranty_end}."
    return {"title": title, "interval_days": days, "description": description}
