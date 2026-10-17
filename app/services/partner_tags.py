"""Partner (contacts) tags — independent of deal_tags, stored in `partner_tags`.

Validation (slug shape, parsing, filter coercion) is shared with deals' tags;
only storage is separate, since a partner and a deal are different entities.
"""
from datetime import datetime, timezone

from app.database import get_db
from app.services.deal_tags import (  # noqa: F401 - re-exported for callers
    MAX_TAGS, TAG_HELP, is_valid_tag, normalize_tag, parse_tag_list, coerce_tag_filter,
)


def tags_for_partner(partner_id: int) -> list:
    with get_db() as db:
        rows = db.execute(
            "SELECT tag FROM partner_tags WHERE partner_id = ? ORDER BY tag",
            (partner_id,),
        ).fetchall()
    return [row["tag"] for row in rows]


def tags_for_partners(partner_ids) -> dict:
    ids = [int(i) for i in partner_ids if i]
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    with get_db() as db:
        rows = db.execute(
            f"SELECT partner_id, tag FROM partner_tags WHERE partner_id IN ({placeholders}) ORDER BY tag",
            ids,
        ).fetchall()
    grouped = {partner_id: [] for partner_id in ids}
    for row in rows:
        grouped.setdefault(row["partner_id"], []).append(row["tag"])
    return grouped


def attach_tags(partners) -> list:
    """Set partner['tags'] to a list (empty when the partner has none)."""
    if not partners:
        return partners
    grouped = tags_for_partners([p["id"] for p in partners if p.get("id")])
    for partner in partners:
        partner["tags"] = grouped.get(partner["id"], [])
    return partners


def _touch_partner(db, partner_id: int) -> None:
    db.execute(
        "UPDATE partners SET updated_at = ? WHERE id = ?",
        (datetime.now(timezone.utc).replace(tzinfo=None).isoformat(), partner_id),
    )


def apply_partner_tags(partner_id: int, *, replace=None, add=None, remove=None):
    """Replace and/or merge tags. None means that operation was not requested.

    `replace` runs first, then `add`, then `remove`. Returns an error code or None.
    """
    if replace is None and not add and not remove:
        return None
    with get_db() as db:
        exists = db.execute("SELECT 1 FROM partners WHERE id = ?", (partner_id,)).fetchone()
        if not exists:
            return "not_found"
        if replace is not None:
            db.execute("DELETE FROM partner_tags WHERE partner_id = ?", (partner_id,))
            for tag in replace:
                db.execute(
                    "INSERT INTO partner_tags (partner_id, tag) VALUES (?, ?)",
                    (partner_id, tag),
                )
        for tag in add or []:
            db.execute(
                "INSERT OR IGNORE INTO partner_tags (partner_id, tag) VALUES (?, ?)",
                (partner_id, tag),
            )
        if remove:
            placeholders = ",".join("?" * len(remove))
            db.execute(
                f"DELETE FROM partner_tags WHERE partner_id = ? AND tag IN ({placeholders})",
                (partner_id, *remove),
            )
        _touch_partner(db, partner_id)
        db.commit()
    return None
