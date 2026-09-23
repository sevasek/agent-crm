from app.database import get_db
from app.services.auth import sanitize_text

VALID_TYPES = {"call", "email", "meeting", "note", "system"}


def log_activity(partner_id: int, activity_type: str, body: str = "", deal_id: int = None) -> int:
    if activity_type not in VALID_TYPES:
        activity_type = "note"
    with get_db() as db:
        db.execute("""
            INSERT INTO activities (partner_id, deal_id, type, body)
            VALUES (?, ?, ?, ?)
        """, (
            partner_id,
            deal_id,
            activity_type,
            sanitize_text(body, max_len=4000, allow_newlines=True) or None,
        ))
        db.commit()
        return db.execute("SELECT last_insert_rowid()").fetchone()[0]


def list_activities_for_partner(partner_id: int):
    with get_db() as db:
        rows = db.execute("""
            SELECT * FROM activities WHERE partner_id = ? ORDER BY occurred_at DESC, id DESC
        """, (partner_id,)).fetchall()
        return [dict(r) for r in rows]


def list_activities_for_deal(deal_id: int):
    with get_db() as db:
        rows = db.execute("""
            SELECT * FROM activities WHERE deal_id = ? ORDER BY occurred_at DESC, id DESC
        """, (deal_id,)).fetchall()
        return [dict(r) for r in rows]
