"""Campaign records — the shared pitch for a set of deals.

A deal belongs to a campaign when it carries the tag `campaign:<slug>`.
There is no `campaign_id` on `deals`. The tag is already how the call
queue, ingest, and deal list select a campaign, and one deal can carry
more than one. A second id would drift from those tags. Creating a
campaign with a slug that is already on deals links those deals without
rewriting them. Renaming the slug rewrites the tag.

Lead-specific notes stay on the deal timeline. This table holds only the
parts that are the same for every lead in the campaign.
"""
import re
from datetime import datetime, timezone

from app.database import IntegrityConflict, get_db
from app.services.auth import sanitize_text
from app.services.catalog import get_service
from app.services.deal_tags import CAMPAIGN_PREFIX, TAG_MAX_LEN, is_valid_tag
from app.services.deals import list_deals
from app.services.offers import get_offer
from app.services.partners import PREFERRED_CHANNELS

STATUSES = ("draft", "active", "paused", "completed")
# Counts are integers. Won value is a non-negative amount.
TARGET_FIELDS = (
    ("target_leads", "leads", "count"),
    ("target_contacts", "contacts", "count"),
    ("target_conversations", "conversations", "count"),
    ("target_advanced", "advanced", "count"),
    ("target_won", "won", "count"),
    ("target_won_value", "won_value", "money"),
)
TARGET_LABELS = (
    ("leads", "Leads"),
    ("contacts", "Contacts"),
    ("conversations", "Conversations"),
    ("advanced", "Advanced"),
    ("won", "Won"),
    ("won_value", "Won value"),
)
SLUG_MAX = TAG_MAX_LEN - len(CAMPAIGN_PREFIX)

ERROR_MESSAGES = {
    "name_required": "Name is required.",
    "invalid_slug": "Slug must use lowercase letters, digits, and hyphens, as in spring-2026.",
    "duplicate_slug": "A campaign with that slug already exists.",
    "invalid_status": "Status must be draft, active, paused, or completed.",
    "invalid_channel": "Channel must be phone, email, text, or blank.",
    "invalid_date": "Dates must be YYYY-MM-DD.",
    "deadline_before_start": "Deadline must be on or after the start date.",
    "invalid_target": "Targets must be zero or a positive number.",
    "invalid_service": "That service does not exist.",
    "invalid_offer": "That offer does not exist.",
    "offer_service_mismatch": "That offer belongs to a different service.",
    "not_found": "That campaign does not exist.",
}

_CAMPAIGN_SELECT = """
    SELECT campaigns.*,
           services.name AS service_name,
           services.slug AS service_slug,
           offers.name AS offer_name
    FROM campaigns
    LEFT JOIN services ON services.id = campaigns.service_id
    LEFT JOIN offers ON offers.id = campaigns.offer_id
"""


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def name_from_slug(slug: str) -> str:
    """Readable label for a backfilled slug. The operator can rename it."""
    parts = [part for part in re.split(r"[-:]+", slug or "") if part]
    if not parts:
        return (slug or "Campaign")[:120]
    words = [part[:1].upper() + part[1:] for part in parts]
    return " ".join(words)[:120]


def normalize_slug(value) -> str | None:
    """A campaign slug, or None.

    An already-valid slug is kept (including a colon, which the tag grammar
    allows). Anything else is turned into a hyphenated slug. A leading
    `campaign:` is stripped so the stored slug is not doubled.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    text = value.strip().lower()
    if text.startswith(CAMPAIGN_PREFIX):
        text = text[len(CAMPAIGN_PREFIX):]
    if _is_slug(text):
        return text
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    text = re.sub(r"-{2,}", "-", text)
    text = text[:SLUG_MAX].strip("-")
    if not _is_slug(text):
        return None
    return text


def _is_slug(text: str) -> bool:
    if not text or len(text) > SLUG_MAX:
        return False
    return is_valid_tag(CAMPAIGN_PREFIX + text)


def campaign_tag(slug: str) -> str | None:
    """`campaign:<slug>` for a stored slug, or None if it cannot be a tag."""
    text = (slug or "").strip().lower()
    if not _is_slug(text):
        return None
    return CAMPAIGN_PREFIX + text


def present_campaign(row):
    if not row:
        return None
    data = dict(row)
    data["tag"] = CAMPAIGN_PREFIX + data["slug"]
    return data


def _optional_id(value):
    if value in (None, "", 0, "0"):
        return None
    if isinstance(value, bool):
        return False
    try:
        number = int(value)
    except (TypeError, ValueError):
        return False
    if number < 1:
        return False
    return number


def _clean_count(value):
    if value is None or value == "":
        return None, None
    if isinstance(value, bool):
        return None, "invalid_target"
    if isinstance(value, int):
        number = value
    elif isinstance(value, float):
        if not value.is_integer():
            return None, "invalid_target"
        number = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None, None
        if not re.fullmatch(r"\d+", text):
            return None, "invalid_target"
        number = int(text)
    else:
        return None, "invalid_target"
    if number < 0:
        return None, "invalid_target"
    return number, None


def _clean_money(value):
    if value is None or value == "":
        return None, None
    if isinstance(value, bool):
        return None, "invalid_target"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None, "invalid_target"
    if number < 0 or number != number or number in (float("inf"), float("-inf")):
        return None, "invalid_target"
    return number, None


def _clean_date(value):
    if value is None or value == "":
        return None, None
    text = str(value).strip()[:10]
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None, "invalid_date"
    return text, None


def _clean_text(value, *, max_len, newlines=False):
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    text = sanitize_text(value, max_len=max_len, allow_newlines=newlines)
    return text or None


def _resolve_links(service_id, offer_id):
    if service_id is False:
        return None, None, "invalid_service"
    if offer_id is False:
        return None, None, "invalid_offer"
    if service_id and not get_service(service_id):
        return None, None, "invalid_service"
    if offer_id:
        offer = get_offer(offer_id)
        if not offer:
            return None, None, "invalid_offer"
        offer_service = offer.get("service_id")
        if offer_service and service_id and int(offer_service) != int(service_id):
            return None, None, "offer_service_mismatch"
        if offer_service and not service_id:
            service_id = int(offer_service)
    return service_id, offer_id, None


def _clean_payload(fields, *, creating, current=None):
    """Return (column dict, error code).

    On update, only keys present in `fields` are applied. None clears an
    optional column. `slug` renames. Status and name cannot be cleared.
    """
    cleaned = {}
    if creating or "name" in fields:
        name = sanitize_text((fields.get("name") or ""), max_len=120)
        if not name:
            return None, "name_required"
        cleaned["name"] = name

    if creating or "slug" in fields:
        raw_slug = fields.get("slug")
        if creating and (raw_slug is None or str(raw_slug).strip() == ""):
            slug = normalize_slug(cleaned.get("name") or "")
        else:
            slug = normalize_slug(raw_slug)
        if not slug:
            return None, "invalid_slug"
        cleaned["slug"] = slug

    if creating or "status" in fields:
        status_raw = fields.get("status")
        status = "" if status_raw is None else str(status_raw).strip().lower()
        if not status:
            status = "draft" if creating else ""
        if status not in STATUSES:
            return None, "invalid_status"
        cleaned["status"] = status

    if creating or "channel" in fields:
        channel_raw = fields.get("channel")
        channel = "" if channel_raw is None else str(channel_raw).strip().lower()
        if not channel:
            cleaned["channel"] = None
        elif channel not in PREFERRED_CHANNELS:
            return None, "invalid_channel"
        else:
            cleaned["channel"] = channel

    for key, newlines in (("audience", True), ("angle", True), ("opener", True)):
        if creating or key in fields:
            cleaned[key] = _clean_text(fields.get(key), max_len=4000, newlines=newlines)

    start_date = current.get("start_date") if current else None
    deadline = current.get("deadline") if current else None
    if creating or "start_date" in fields:
        start_date, error = _clean_date(fields.get("start_date"))
        if error:
            return None, error
        cleaned["start_date"] = start_date
    if creating or "deadline" in fields:
        deadline, error = _clean_date(fields.get("deadline"))
        if error:
            return None, error
        cleaned["deadline"] = deadline
    if start_date and deadline and deadline < start_date:
        return None, "deadline_before_start"

    service_id = current.get("service_id") if current else None
    offer_id = current.get("offer_id") if current else None
    if creating or "service_id" in fields:
        service_id = _optional_id(fields.get("service_id"))
    if creating or "offer_id" in fields:
        offer_id = _optional_id(fields.get("offer_id"))
    if creating or "service_id" in fields or "offer_id" in fields:
        service_id, offer_id, error = _resolve_links(service_id, offer_id)
        if error:
            return None, error
        cleaned["service_id"] = service_id
        cleaned["offer_id"] = offer_id

    for column, _key, kind in TARGET_FIELDS:
        if not creating and column not in fields:
            continue
        cleaner = _clean_count if kind == "count" else _clean_money
        number, error = cleaner(fields.get(column))
        if error:
            return None, error
        cleaned[column] = number

    return cleaned, None


def list_campaigns(status=None):
    query = _CAMPAIGN_SELECT
    params = []
    if status:
        query += " WHERE campaigns.status = ?"
        params.append(status)
    query += """
        ORDER BY
            CASE campaigns.status
                WHEN 'active' THEN 0
                WHEN 'paused' THEN 1
                WHEN 'draft' THEN 2
                ELSE 3
            END,
            CASE WHEN campaigns.deadline IS NULL OR campaigns.deadline = '' THEN 1 ELSE 0 END,
            campaigns.deadline,
            campaigns.name COLLATE NOCASE
    """
    with get_db() as db:
        rows = db.execute(query, params).fetchall()
    return [present_campaign(row) for row in rows]


def get_campaign(campaign_id=None, slug=None):
    with get_db() as db:
        if campaign_id:
            row = db.execute(
                _CAMPAIGN_SELECT + " WHERE campaigns.id = ?",
                (int(campaign_id),),
            ).fetchone()
        elif slug:
            text = normalize_slug(slug)
            if not text:
                return None
            row = db.execute(
                _CAMPAIGN_SELECT + " WHERE campaigns.slug = ?",
                (text,),
            ).fetchone()
        else:
            return None
    return present_campaign(row)


def get_campaign_by_slug(slug: str):
    return get_campaign(slug=slug)


def create_campaign(name, **fields):
    """Return (campaign, None) or (None, error_code)."""
    fields = dict(fields)
    fields["name"] = name
    cleaned, error = _clean_payload(fields, creating=True)
    if error:
        return None, error
    now = _now()
    columns = [
        "name", "slug", "audience", "service_id", "offer_id", "angle", "opener",
        "channel", "status", "start_date", "deadline",
        "target_leads", "target_contacts", "target_conversations",
        "target_advanced", "target_won", "target_won_value",
        "created_at", "updated_at",
    ]
    values = [cleaned.get(column) for column in columns[:-2]] + [now, now]
    placeholders = ", ".join("?" * len(columns))
    try:
        with get_db() as db:
            db.execute(
                f"INSERT INTO campaigns ({', '.join(columns)}) VALUES ({placeholders})",
                values,
            )
            db.commit()
            new_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    except IntegrityConflict:
        return None, "duplicate_slug"
    return get_campaign(new_id), None


def _rewrite_campaign_tag(db, old_slug: str, new_slug: str) -> None:
    old_tag = CAMPAIGN_PREFIX + old_slug
    new_tag = CAMPAIGN_PREFIX + new_slug
    if old_tag == new_tag:
        return
    # Drop the old tag where the deal already has the new one, then rename
    # the rest. The primary key is (deal_id, tag), so the delete has to
    # happen first.
    db.execute(
        """DELETE FROM deal_tags
           WHERE tag = ?
             AND deal_id IN (SELECT deal_id FROM deal_tags WHERE tag = ?)""",
        (old_tag, new_tag),
    )
    db.execute(
        "UPDATE deal_tags SET tag = ? WHERE tag = ?",
        (new_tag, old_tag),
    )


def update_campaign(campaign_id: int, **fields):
    """Return (campaign, None) or (None, error_code).

    Omitted keys are left as they are. None clears an optional field.
    Changing `slug` rewrites `campaign:<old>` tags to `campaign:<new>`.
    """
    current = get_campaign(campaign_id)
    if not current:
        return None, "not_found"
    cleaned, error = _clean_payload(fields, creating=False, current=current)
    if error:
        return None, error
    if not cleaned:
        return current, None
    cleaned["updated_at"] = _now()
    new_slug = cleaned.get("slug", current["slug"])
    try:
        with get_db() as db:
            if new_slug != current["slug"]:
                taken = db.execute(
                    "SELECT id FROM campaigns WHERE slug = ? AND id != ?",
                    (new_slug, current["id"]),
                ).fetchone()
                if taken:
                    return None, "duplicate_slug"
                _rewrite_campaign_tag(db, current["slug"], new_slug)
            assignments = ", ".join(f"{column} = ?" for column in cleaned)
            db.execute(
                f"UPDATE campaigns SET {assignments} WHERE id = ?",
                list(cleaned.values()) + [current["id"]],
            )
            db.commit()
    except IntegrityConflict:
        return None, "duplicate_slug"
    return get_campaign(current["id"]), None


def backfill_campaigns_from_tags(db) -> int:
    """Insert a campaign for each distinct `campaign:` tag. Idempotent.

    Uses the caller's connection and does not commit. Status is `active`
    because the tag is already on deals. The name is a readable form of
    the slug.
    """
    rows = db.execute(
        """SELECT DISTINCT tag FROM deal_tags
           WHERE tag LIKE 'campaign:%'
           ORDER BY tag"""
    ).fetchall()
    now = _now()
    inserted = 0
    for row in rows:
        tag = row["tag"]
        if not isinstance(tag, str) or not tag.startswith(CAMPAIGN_PREFIX):
            continue
        slug = tag[len(CAMPAIGN_PREFIX):]
        if not _is_slug(slug):
            continue
        cursor = db.execute(
            """INSERT OR IGNORE INTO campaigns
               (name, slug, status, created_at, updated_at)
               VALUES (?, ?, 'active', ?, ?)""",
            (name_from_slug(slug), slug, now, now),
        )
        inserted += cursor.rowcount
    return inserted


def campaign_links_for_tags(tags):
    """Campaigns referenced by `campaign:` tags, in tag order.

    A tag with no campaign row is returned with `campaign` None so the
    deal page can still show the tag. Creating the campaign later links
    it, because membership is the tag.
    """
    slugs = []
    for tag in tags or []:
        if not isinstance(tag, str) or not tag.startswith(CAMPAIGN_PREFIX):
            continue
        slug = tag[len(CAMPAIGN_PREFIX):]
        if slug and slug not in slugs:
            slugs.append(slug)
    if not slugs:
        return []
    found = {}
    placeholders = ", ".join("?" * len(slugs))
    with get_db() as db:
        rows = db.execute(
            _CAMPAIGN_SELECT + f" WHERE campaigns.slug IN ({placeholders})",
            slugs,
        ).fetchall()
    for row in rows:
        item = present_campaign(row)
        found[item["slug"]] = item
    links = []
    for slug in slugs:
        links.append({
            "slug": slug,
            "tag": CAMPAIGN_PREFIX + slug,
            "campaign": found.get(slug),
        })
    return links


def _display_name(deal) -> str:
    for key in ("partner_name", "company_name", "contact_name", "name"):
        text = (deal.get(key) or "").strip()
        if text:
            return text
    return f"#{deal['id']}"


def list_campaign_deals(slug: str):
    """Every lead and opportunity carrying this campaign's tag, including lost.

    `outcome` uses the same classifier as the campaign metrics.
    """
    tag = campaign_tag(slug)
    if not tag:
        return []
    deals = list_deals(tags=[tag], type="all", include_lost=True)
    from app.services.campaign_metrics_adapter import annotate_outcomes

    annotate_outcomes(deals)
    for deal in deals:
        deal["display_name"] = _display_name(deal)
    return deals
