"""Deal tags — labels on a deal, including campaign slugs.

A tag is a lowercase slug of letters, digits, hyphens and colons, for example
`campaign:icp-hc-illawarra-2026-09`, `church`, `illawarra`. Stored in
`deal_tags` (deal_id, tag), not as a column on `deals`.
"""
import re
from datetime import datetime

from app.database import get_db

# One or more alphanumerics, then optional :segment or -segment repeats.
# Rejects leading/trailing separators and empty segments (`campaign:`, `a--b`).
_TAG_RE = re.compile(r"^[a-z0-9]+(?:[:\-][a-z0-9]+)*$")
TAG_MAX_LEN = 80
MAX_TAGS = 50
CAMPAIGN_PREFIX = "campaign:"
TAG_HELP = (
    "Tags are lowercase slugs of letters, digits, hyphens and colons "
    "(for example campaign:icp-hc-illawarra-2026-09, church, illawarra)."
)


def is_valid_tag(tag: str) -> bool:
    if not tag or len(tag) > TAG_MAX_LEN:
        return False
    return bool(_TAG_RE.match(tag))


def normalize_tag(value):
    """Lowercase and validate. Returns the slug, or None if it cannot be a tag."""
    if isinstance(value, bool) or value is None:
        return None
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not is_valid_tag(text):
        return None
    return text


def campaign_tag_from_external_ref(external_ref):
    """A `campaign:` slug stored in external_ref, or None.

    The stored external_ref is not rewritten. Only the lowercased form is
    considered, and only when that form is itself a valid tag.
    """
    if not isinstance(external_ref, str):
        return None
    candidate = external_ref.strip().lower()
    if not candidate.startswith(CAMPAIGN_PREFIX):
        return None
    if not is_valid_tag(candidate):
        return None
    return candidate


def parse_tag_list(value):
    """Coerce a tag list for a write.

    None means the caller omitted tags → (None, None).
    A string is comma-separated (the admin form). An array is used as-is.
    Case is normalized. Returns (tags, error_message).
    """
    if value is None:
        return None, None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return [], None
        value = [part.strip() for part in text.split(",") if part.strip()]
    if not isinstance(value, list):
        return None, f"tags must be an array of strings. {TAG_HELP}"
    if len(value) > MAX_TAGS:
        return None, f"At most {MAX_TAGS} tags."
    out = []
    for item in value:
        if not isinstance(item, str):
            return None, f"Invalid tag {item!r}. {TAG_HELP}"
        tag = normalize_tag(item)
        if not tag:
            shown = item.strip() if isinstance(item, str) else item
            return None, f"Invalid tag {shown!r}. {TAG_HELP}"
        if tag not in out:
            out.append(tag)
    return out, None


def tags_from_lead(raw):
    """Ingest: a list of tags, dropping anything that is not a valid slug.

    None (missing, or not a list) means leave the deal's tags alone.
    An empty list adds nothing. Never raises.
    """
    if not isinstance(raw, list):
        return None
    out = []
    for item in raw:
        if not isinstance(item, str):
            continue
        tag = normalize_tag(item)
        if tag and tag not in out:
            out.append(tag)
        if len(out) >= MAX_TAGS:
            break
    return out


def coerce_tag_filter(tags):
    """Return (wanted_tags, impossible).

    None / empty means no filter. An invalid tag matches nothing, so the
    caller should return an empty deal list rather than ignore the filter.
    """
    if tags is None or tags == "" or tags == []:
        return [], False
    if isinstance(tags, str):
        tags = [tags]
    if not isinstance(tags, (list, tuple)):
        return [], True
    wanted = []
    for raw in tags:
        if not isinstance(raw, str):
            return [], True
        tag = normalize_tag(raw)
        if not tag:
            return [], True
        if tag not in wanted:
            wanted.append(tag)
    return wanted, False


def tags_for_deal(deal_id: int) -> list:
    with get_db() as db:
        rows = db.execute(
            "SELECT tag FROM deal_tags WHERE deal_id = ? ORDER BY tag",
            (deal_id,),
        ).fetchall()
    return [row["tag"] for row in rows]


def tags_for_deals(deal_ids) -> dict:
    ids = [int(i) for i in deal_ids if i]
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    with get_db() as db:
        rows = db.execute(
            f"SELECT deal_id, tag FROM deal_tags WHERE deal_id IN ({placeholders}) ORDER BY tag",
            ids,
        ).fetchall()
    grouped = {deal_id: [] for deal_id in ids}
    for row in rows:
        grouped.setdefault(row["deal_id"], []).append(row["tag"])
    return grouped


def attach_tags(deals) -> list:
    """Set deal['tags'] to a list (empty when the deal has none)."""
    if not deals:
        return deals
    grouped = tags_for_deals([d["id"] for d in deals if d.get("id")])
    for deal in deals:
        deal["tags"] = grouped.get(deal["id"], [])
    return deals


def _touch_deal(db, deal_id: int) -> None:
    db.execute(
        "UPDATE deals SET updated_at = ? WHERE id = ?",
        (datetime.utcnow().isoformat(), deal_id),
    )


def apply_deal_tags(deal_id: int, *, replace=None, add=None, remove=None):
    """Replace and/or merge tags. None means that operation was not requested.

    `replace` runs first, then `add`, then `remove`, so a tag listed in both
    add and remove ends up removed. Returns an error code or None.
    """
    if replace is None and not add and not remove:
        return None
    with get_db() as db:
        exists = db.execute("SELECT 1 FROM deals WHERE id = ?", (deal_id,)).fetchone()
        if not exists:
            return "not_found"
        if replace is not None:
            db.execute("DELETE FROM deal_tags WHERE deal_id = ?", (deal_id,))
            for tag in replace:
                db.execute(
                    "INSERT INTO deal_tags (deal_id, tag) VALUES (?, ?)",
                    (deal_id, tag),
                )
        for tag in add or []:
            db.execute(
                "INSERT OR IGNORE INTO deal_tags (deal_id, tag) VALUES (?, ?)",
                (deal_id, tag),
            )
        if remove:
            placeholders = ",".join("?" * len(remove))
            db.execute(
                f"DELETE FROM deal_tags WHERE deal_id = ? AND tag IN ({placeholders})",
                (deal_id, *remove),
            )
        _touch_deal(db, deal_id)
        db.commit()
    return None


def add_deal_tags(deal_id: int, tags) -> None:
    """Merge tags onto a deal. Used by lead ingest. Ignores an unknown deal."""
    if not tags:
        return
    apply_deal_tags(deal_id, add=list(tags))


def list_tags() -> list:
    """Tags currently in use, with how many deals carry each one."""
    with get_db() as db:
        rows = db.execute(
            """SELECT tag, COUNT(*) AS count
               FROM deal_tags
               GROUP BY tag
               ORDER BY tag"""
        ).fetchall()
    return [{"tag": row["tag"], "count": row["count"]} for row in rows]


def backfill_campaign_tags(db) -> int:
    """Copy `campaign:` external_refs onto tags. Leaves external_ref unchanged.

    Idempotent (INSERT OR IGNORE). Returns how many tag rows were inserted.
    """
    rows = db.execute(
        """SELECT id, external_ref FROM deals
           WHERE external_ref IS NOT NULL AND external_ref != ''"""
    ).fetchall()
    inserted = 0
    for row in rows:
        tag = campaign_tag_from_external_ref(row["external_ref"])
        if not tag:
            continue
        cursor = db.execute(
            "INSERT OR IGNORE INTO deal_tags (deal_id, tag) VALUES (?, ?)",
            (row["id"], tag),
        )
        inserted += cursor.rowcount
    return inserted
