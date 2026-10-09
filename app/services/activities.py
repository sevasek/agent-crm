from app.database import get_db
from app.services.auth import sanitize_text

VALID_TYPES = {"call", "email", "meeting", "note", "system", "research"}


class ActivityError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _clean_source_url(value):
    if value in (None, ""):
        return None
    text = str(value).strip()
    if len(text) > 2000 or not (text.startswith("http://") or text.startswith("https://")):
        raise ActivityError("invalid_source_url")
    return text


def log_activity(partner_id: int, activity_type: str, body: str = "", deal_id: int = None,
                 source_url: str = None, db=None) -> int:
    if activity_type not in VALID_TYPES:
        activity_type = "note"
    source = _clean_source_url(source_url)
    if not partner_id and not deal_id:
        raise ActivityError("partner_or_deal_required")
    if deal_id and partner_id:
        from app.services.deals import get_deal
        deal = get_deal(deal_id)
        if deal and deal.get("partner_id") and int(deal["partner_id"]) != int(partner_id):
            raise ActivityError("deal_mismatch")
    body_value = sanitize_text(body, max_len=4000, allow_newlines=True) or None

    def _insert(conn):
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(activities)").fetchall()}
        if "source_url" in columns:
            conn.execute(
                """INSERT INTO activities (partner_id, deal_id, type, body, source_url)
                   VALUES (?, ?, ?, ?, ?)""",
                (partner_id, deal_id, activity_type, body_value, source),
            )
        else:
            conn.execute(
                """INSERT INTO activities (partner_id, deal_id, type, body)
                   VALUES (?, ?, ?, ?)""",
                (partner_id, deal_id, activity_type, body_value),
            )
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    if db is not None:
        return _insert(db)
    with get_db() as conn:
        activity_id = _insert(conn)
        conn.commit()
        return activity_id


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
