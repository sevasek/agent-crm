from app.database import get_db
from app.services.auth import sanitize_text

VALID_TYPES = {"call", "email", "meeting", "note", "system", "research"}
SOURCE_URL_MAX = 2000


def clean_source_url(value):
    """http/https only, max 2,000 characters. Invalid input becomes None."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > SOURCE_URL_MAX:
        return None
    lower = text.lower()
    if not (lower.startswith("http://") or lower.startswith("https://")):
        return None
    return text


def log_activity(partner_id: int, activity_type: str, body: str = "", deal_id: int = None,
                 source_url: str = None) -> int:
    """Write an activity. partner_id may be null when deal_id is set (D5)."""
    if activity_type not in VALID_TYPES:
        activity_type = "note"
    if not partner_id and not deal_id:
        raise ValueError("activity needs a partner_id or a deal_id")
    if deal_id and partner_id:
        from app.services.deals import get_deal
        deal = get_deal(deal_id)
        if deal and deal.get("partner_id") and int(deal["partner_id"]) != int(partner_id):
            raise ValueError("deal_mismatch")
    url = clean_source_url(source_url)
    with get_db() as db:
        db.execute("""
            INSERT INTO activities (partner_id, deal_id, type, body, source_url)
            VALUES (?, ?, ?, ?, ?)
        """, (
            partner_id or None,
            deal_id,
            activity_type,
            sanitize_text(body, max_len=4000, allow_newlines=True) or None,
            url,
        ))
        db.commit()
        return db.execute("SELECT last_insert_rowid()").fetchone()[0]


def list_activities_for_partner(partner_id: int):
    with get_db() as db:
        rows = db.execute("""
            SELECT * FROM activities WHERE partner_id = ? ORDER BY occurred_at DESC, id DESC
        """, (partner_id,)).fetchall()
        return [dict(r) for r in rows]


def list_activities_for_deal(deal_id: int, *, research_first: bool = False):
    order = (
        "ORDER BY CASE WHEN type = 'research' THEN 0 ELSE 1 END, occurred_at DESC, id DESC"
        if research_first
        else "ORDER BY occurred_at DESC, id DESC"
    )
    with get_db() as db:
        rows = db.execute(
            f"SELECT * FROM activities WHERE deal_id = ? {order}",
            (deal_id,),
        ).fetchall()
        return [dict(r) for r in rows]
