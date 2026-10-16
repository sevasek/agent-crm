"""Operator-configured lost reasons (ADR 0001 §5.2 / §8.1)."""
from datetime import datetime, timezone

from app.database import IntegrityConflict, get_db
from app.services.auth import sanitize_text


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def list_lost_reasons(active_only: bool = False):
    query = "SELECT * FROM lost_reasons"
    if active_only:
        query += " WHERE active = 1"
    query += " ORDER BY name COLLATE NOCASE, id"
    with get_db() as db:
        return [dict(r) for r in db.execute(query).fetchall()]


def get_lost_reason(reason_id: int):
    if not reason_id:
        return None
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM lost_reasons WHERE id = ?", (int(reason_id),)
        ).fetchone()
    return dict(row) if row else None


def get_lost_reason_by_name(name: str):
    text = sanitize_text(name or "", max_len=80)
    if not text:
        return None
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM lost_reasons WHERE name = ?", (text,)
        ).fetchone()
    return dict(row) if row else None


def resolve_lost_reason(lost_reason_id=None, lost_reason=None):
    if lost_reason_id not in (None, "", 0, "0"):
        try:
            return get_lost_reason(int(lost_reason_id))
        except (TypeError, ValueError):
            return None
    if lost_reason:
        return get_lost_reason_by_name(lost_reason)
    return None


def create_lost_reason(name: str, *, triggers_nurture=False, active=True):
    cleaned = sanitize_text(name or "", max_len=80)
    if not cleaned:
        return None, "name_required"
    now = _now()
    try:
        with get_db() as db:
            db.execute(
                """INSERT INTO lost_reasons
                       (name, triggers_nurture, active, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (cleaned, int(bool(triggers_nurture)), int(bool(active)), now, now),
            )
            db.commit()
            reason_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    except IntegrityConflict:
        return None, "duplicate_name"
    return get_lost_reason(reason_id), None


def update_lost_reason(reason_id: int, **fields):
    current = get_lost_reason(reason_id)
    if not current:
        return None, "not_found"
    allowed = {"name", "triggers_nurture", "active"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if "name" in updates:
        name = sanitize_text(updates["name"] or "", max_len=80)
        if not name:
            return None, "name_required"
        updates["name"] = name
    if "triggers_nurture" in updates:
        updates["triggers_nurture"] = int(bool(updates["triggers_nurture"]))
    if "active" in updates:
        updates["active"] = int(bool(updates["active"]))
    if not updates:
        return current, None
    updates["updated_at"] = _now()
    try:
        with get_db() as db:
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            db.execute(
                f"UPDATE lost_reasons SET {set_clause} WHERE id = ?",
                list(updates.values()) + [reason_id],
            )
            db.commit()
    except IntegrityConflict:
        return None, "duplicate_name"
    return get_lost_reason(reason_id), None
