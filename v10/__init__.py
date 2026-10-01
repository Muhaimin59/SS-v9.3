"""SmartServe V10 — "An Intelligent Service Lifecycle Platform".

The package is mounted on the existing Flask application as a blueprint, so the
V9.3 application keeps running unchanged while V10 adds the connected lifecycle
layer on top of it.

Modules
-------
1. ``engine``  — assets/Service Passport, missions, black box, certificates,
   second opinion, parts, outcome/recovery and skill eligibility.
2. ``schema``  — additive migrations (no table or column is ever dropped).
3. ``catalog`` — the expanded, metadata-rich service catalogue.
4. ``ai``      — structured AI (problem understanding, mission planning, parts,
   second opinion, recovery classification) with an honest offline fallback.
5. ``routes``  — the V10 pages and JSON APIs.
"""

from __future__ import annotations

V10_VERSION = "10.0"
V10_NAME = "SmartServe V10"


def register(app):
    """Register the V10 blueprint on an existing Flask app."""
    from .routes import v10

    if "v10" not in app.blueprints:
        app.register_blueprint(v10)
    return app


def migrate(connection=None):
    """Run the additive V10 migrations."""
    from .schema import run_v10_migrations

    return run_v10_migrations(connection)


__all__ = ["register", "migrate", "V10_VERSION", "V10_NAME"]
