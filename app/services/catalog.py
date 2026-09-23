import re
import time

from app.database import get_db, IntegrityConflict, is_lock_error
from app.services.auth import is_valid_slug, sanitize_text


def _clean_nurture_list_slug(value):
    """List slugs are URL path segments. Empty means no nurture list."""
    value = (value or "").strip().lower()
    if not value:
        return None
    return value if is_valid_slug(value) else None


def slug_from_name(name: str):
    """'AI Concierge' → 'ai-concierge'. None if the result is not a valid slug."""
    text = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return text if is_valid_slug(text) else None


def create_service(name, slug, description="", nurture_list_slug=""):
    name = sanitize_text(name, max_len=200)
    with get_db() as db:
        db.execute("""
            INSERT INTO services (name, slug, description, nurture_list_slug)
            VALUES (?, ?, ?, ?)
        """, (
            name,
            slug.strip().lower(),
            sanitize_text(description, max_len=2000, allow_newlines=True) or None,
            _clean_nurture_list_slug(nurture_list_slug),
        ))
        db.commit()
        return db.execute("SELECT last_insert_rowid()").fetchone()[0]


def get_service(service_id: int):
    with get_db() as db:
        row = db.execute("SELECT * FROM services WHERE id = ?", (service_id,)).fetchone()
        return dict(row) if row else None


def get_service_by_slug(slug: str):
    with get_db() as db:
        row = db.execute("SELECT * FROM services WHERE slug = ?", (slug,)).fetchone()
        return dict(row) if row else None


def get_or_create_service(name, slug, description="", nurture_list_slug=""):
    """Return (service_dict, created). Idempotent on slug UNIQUE — never duplicates."""
    slug = slug.strip().lower()
    for attempt in range(5):
        try:
            existing = get_service_by_slug(slug)
            if existing:
                return existing, False
            try:
                service_id = create_service(
                    name, slug, description=description, nurture_list_slug=nurture_list_slug,
                )
            except IntegrityConflict as exc:
                existing = get_service_by_slug(slug)
                if existing:
                    return existing, False
                raise RuntimeError(
                    f"slug {slug!r} conflicted but could not be re-read"
                ) from exc
            service = get_service(service_id)
            if not service:
                raise RuntimeError(f"created service {service_id} could not be re-read")
            return service, True
        except Exception as exc:
            if is_lock_error(exc) and attempt < 4:
                time.sleep(0.05 * (attempt + 1))
                continue
            raise


def update_service(service_id: int, **fields):
    allowed = {"name", "slug", "description", "nurture_list_slug", "active"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if "name" in updates:
        updates["name"] = sanitize_text(updates["name"], max_len=200)
    if "slug" in updates:
        updates["slug"] = (updates["slug"] or "").strip().lower()
    if "description" in updates:
        updates["description"] = sanitize_text(updates["description"], max_len=2000, allow_newlines=True) or None
    if "nurture_list_slug" in updates:
        updates["nurture_list_slug"] = _clean_nurture_list_slug(updates["nurture_list_slug"])
    if "active" in updates and updates["active"] is not None:
        updates["active"] = int(bool(updates["active"]))
    if not updates:
        return False
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [service_id]
    with get_db() as db:
        cursor = db.execute(f"UPDATE services SET {set_clause} WHERE id = ?", values)
        db.commit()
        return cursor.rowcount > 0


def update_catalog_service(service_id: int, **fields):
    """Validated catalog update for MCP. Returns (service_dict, error_code)."""
    service = get_service(service_id)
    if not service:
        return None, "not_found"
    allowed = {"name", "slug", "description", "nurture_list_slug", "active"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if "name" in updates:
        name = sanitize_text(updates["name"], max_len=200)
        if not name:
            return None, "name_required"
        updates["name"] = name
    if "slug" in updates:
        slug = (updates["slug"] or "").strip().lower()
        if not is_valid_slug(slug):
            return None, "invalid_slug"
        existing = get_service_by_slug(slug)
        if existing and existing["id"] != service_id:
            return None, "slug_conflict"
        updates["slug"] = slug
    if not updates:
        return service, None
    try:
        update_service(service_id, **updates)
    except IntegrityConflict:
        return None, "slug_conflict"
    return get_service(service_id), None


def list_services(active_only: bool = False):
    with get_db() as db:
        if active_only:
            rows = db.execute("SELECT * FROM services WHERE active = 1 ORDER BY name COLLATE NOCASE ASC").fetchall()
        else:
            rows = db.execute("SELECT * FROM services ORDER BY name COLLATE NOCASE ASC").fetchall()
        return [dict(r) for r in rows]
