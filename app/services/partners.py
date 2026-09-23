from datetime import datetime
from urllib.parse import urlparse

from app.database import get_db
from app.services.auth import clean_owner_key, sanitize_text
from app.services.phone import phone_digits

# Not schema-enforced (same convention as activities.VALID_TYPES) — an
# unrecognized value is dropped rather than rejected, since this is a
# convenience filter/display hint, not data integrity.
PREFERRED_CHANNELS = {"email", "phone", "text"}

# Per-platform profile URLs. `social_url` stays as a derived "primary" link
# (first filled platform) so ICP / call-queue / lead ingest keep working.
SOCIAL_PLATFORMS = (
    ("linkedin_url", "LinkedIn", ("linkedin.com", "lnkd.in")),
    ("x_url", "X", ("x.com", "twitter.com")),
    ("instagram_url", "Instagram", ("instagram.com", "instagr.am")),
    ("facebook_url", "Facebook", ("facebook.com", "fb.com", "fb.me")),
    ("youtube_url", "YouTube", ("youtube.com", "youtu.be")),
)
SOCIAL_FIELDS = tuple(field for field, _label, _hosts in SOCIAL_PLATFORMS)


def _clean_team_size(value):
    if value in (None, ""):
        return None
    try:
        size = int(value)
        return size if size >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _clean_preferred_channel(value):
    value = (value or "").strip().lower()
    return value if value in PREFERRED_CHANNELS else None


def _url_host(url):
    raw = (url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    try:
        host = (urlparse(raw).hostname or "").lower()
    except ValueError:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


def platform_field_for_url(url):
    """Return the social column a URL belongs to, or None if unclassified."""
    host = _url_host(url)
    if not host:
        return None
    for field, _label, hosts in SOCIAL_PLATFORMS:
        for candidate in hosts:
            if host == candidate or host.endswith("." + candidate):
                return field
    return None


def _clean_social_url(value):
    return sanitize_text(value, max_len=300) or None


def apply_social_fields(existing=None, incoming=None):
    """Merge incoming social columns onto an existing partner row.

    Platform fields in `incoming` win. A leftover `social_url` (lead ingest,
    markdown import, or a pre-platform row) is copied onto the matching
    empty platform column — LinkedIn if the host is unknown — so it stays
    visible on the form. `social_url` is then the first filled platform.
    """
    current = {field: (existing or {}).get(field) for field in SOCIAL_FIELDS}
    incoming = incoming or {}
    for field in SOCIAL_FIELDS:
        if field in incoming:
            current[field] = _clean_social_url(incoming[field])
    if "social_url" in incoming:
        leftover = _clean_social_url(incoming["social_url"])
        if leftover:
            target = platform_field_for_url(leftover)
            if target and not current[target]:
                current[target] = leftover
            elif not any(current[field] for field in SOCIAL_FIELDS):
                current["linkedin_url"] = leftover
    current["social_url"] = next((current[field] for field in SOCIAL_FIELDS if current[field]), None)
    return current


def primary_social_url(partner):
    if not partner:
        return None
    for field in SOCIAL_FIELDS:
        if partner.get(field):
            return partner[field]
    return partner.get("social_url") or None


def listed_social_links(partner):
    """(label, url) pairs for display, skipping blanks."""
    if not partner:
        return []
    links = []
    seen = set()
    for field, label, _hosts in SOCIAL_PLATFORMS:
        url = partner.get(field)
        if url and url not in seen:
            links.append((label, url))
            seen.add(url)
    leftover = partner.get("social_url")
    if leftover and leftover not in seen:
        links.append(("Profile", leftover))
    return links


def _website_key(value: str) -> str:
    text = (value or "").strip().lower().rstrip("/")
    for prefix in ("https://", "http://"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    if text.startswith("www."):
        text = text[4:]
    return text


def create_partner(name, is_company=False, parent_id=None, email="", phone="", website="", title="",
                    address="", social_url="", preferred_channel="", industry="", team_size=None,
                    linkedin_url="", x_url="", instagram_url="", facebook_url="", youtube_url="",
                    owner_key=""):
    name = sanitize_text(name, max_len=200)
    socials = apply_social_fields({}, {
        "linkedin_url": linkedin_url,
        "x_url": x_url,
        "instagram_url": instagram_url,
        "facebook_url": facebook_url,
        "youtube_url": youtube_url,
        "social_url": social_url,
    })
    with get_db() as db:
        db.execute("""
            INSERT INTO partners (
                is_company, parent_id, name, email, phone, website, title,
                address, social_url, preferred_channel, industry, team_size,
                linkedin_url, x_url, instagram_url, facebook_url, youtube_url,
                owner_key
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            1 if is_company else 0,
            parent_id or None,
            name,
            sanitize_text(email, max_len=254) or None,
            sanitize_text(phone, max_len=50) or None,
            sanitize_text(website, max_len=254) or None,
            sanitize_text(title, max_len=120) or None,
            sanitize_text(address, max_len=300) or None,
            socials["social_url"],
            _clean_preferred_channel(preferred_channel),
            sanitize_text(industry, max_len=120) or None,
            _clean_team_size(team_size),
            socials["linkedin_url"],
            socials["x_url"],
            socials["instagram_url"],
            socials["facebook_url"],
            socials["youtube_url"],
            clean_owner_key(owner_key),
        ))
        db.commit()
        return db.execute("SELECT last_insert_rowid()").fetchone()[0]


def get_partner(partner_id: int):
    with get_db() as db:
        row = db.execute("SELECT * FROM partners WHERE id = ?", (partner_id,)).fetchone()
        return dict(row) if row else None


def update_partner(partner_id: int, **fields):
    allowed = {
        "is_company", "parent_id", "name", "email", "phone", "website", "title",
        "address", "social_url", "preferred_channel", "industry", "team_size",
        "owner_key",
        *SOCIAL_FIELDS,
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    text_fields = {
        "name": 200, "email": 254, "phone": 50, "website": 254, "title": 120,
        "address": 300, "social_url": 300, "industry": 120,
        **{field: 300 for field in SOCIAL_FIELDS},
    }
    for k, max_len in text_fields.items():
        if k in updates and updates[k] is not None:
            updates[k] = sanitize_text(updates[k], max_len=max_len) or None
    if "preferred_channel" in updates:
        updates["preferred_channel"] = _clean_preferred_channel(updates["preferred_channel"])
    if "team_size" in updates:
        updates["team_size"] = _clean_team_size(updates["team_size"])
    if "owner_key" in updates:
        updates["owner_key"] = clean_owner_key(updates["owner_key"])
    social_incoming = {k: updates[k] for k in (*SOCIAL_FIELDS, "social_url") if k in updates}
    if social_incoming:
        existing = get_partner(partner_id) or {}
        updates.update(apply_social_fields(existing, social_incoming))
    if not updates:
        return False
    updates["updated_at"] = datetime.utcnow().isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [partner_id]
    with get_db() as db:
        cursor = db.execute(f"UPDATE partners SET {set_clause} WHERE id = ?", values)
        db.commit()
        return cursor.rowcount > 0


def list_partners(search: str = ""):
    with get_db() as db:
        if search:
            like = f"%{search.strip()}%"
            rows = db.execute("""
                SELECT * FROM partners
                WHERE name LIKE ? OR email LIKE ? OR IFNULL(phone, '') LIKE ?
                   OR IFNULL(website, '') LIKE ?
                ORDER BY name COLLATE NOCASE ASC
            """, (like, like, like, like)).fetchall()
        else:
            rows = db.execute("SELECT * FROM partners ORDER BY name COLLATE NOCASE ASC").fetchall()
        return [dict(r) for r in rows]


def get_children(parent_id: int):
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM partners WHERE parent_id = ? ORDER BY name COLLATE NOCASE ASC",
            (parent_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def list_companies():
    """Partners eligible to be a parent — companies only, to keep the hierarchy one level deep."""
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM partners WHERE is_company = 1 ORDER BY name COLLATE NOCASE ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_partner_by_email(email: str):
    """Exact match, for dedup — unlike list_partners' substring search."""
    email = (email or "").strip().lower()
    if not email:
        return None
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM partners WHERE lower(email) = ? LIMIT 1", (email,)
        ).fetchone()
        return dict(row) if row else None


def get_company_by_name(name: str):
    """Exact match on a company partner's name, for the lead-ingestion API's
    find-or-create-company step. Case-insensitive since ETL sources won't
    reliably match casing on repeated runs."""
    name = (name or "").strip()
    if not name:
        return None
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM partners WHERE is_company = 1 AND lower(name) = lower(?) LIMIT 1",
            (name,),
        ).fetchone()
        return dict(row) if row else None


def get_partner_by_phone_and_name(name: str, phone: str, is_company=None):
    """Digit-normalized phone + case-insensitive name. Scraped lists reformat spaces."""
    name = (name or "").strip()
    digits = phone_digits(phone)
    if not name or not digits:
        return None
    with get_db() as db:
        rows = db.execute(
            """SELECT * FROM partners
               WHERE lower(name) = lower(?) AND phone IS NOT NULL AND TRIM(phone) != ''""",
            (name,),
        ).fetchall()
    for row in rows:
        partner = dict(row)
        if phone_digits(partner.get("phone")) != digits:
            continue
        if is_company is None or bool(partner["is_company"]) == bool(is_company):
            return partner
    return None


def get_partner_by_website_and_name(name: str, website: str, is_company=None):
    name = (name or "").strip()
    key = _website_key(website)
    if not name or not key:
        return None
    with get_db() as db:
        rows = db.execute(
            """SELECT * FROM partners
               WHERE lower(name) = lower(?) AND website IS NOT NULL AND TRIM(website) != ''""",
            (name,),
        ).fetchall()
    for row in rows:
        partner = dict(row)
        if _website_key(partner.get("website")) != key:
            continue
        if is_company is None or bool(partner["is_company"]) == bool(is_company):
            return partner
    return None


def get_person_by_name(name: str, parent_id=None, is_company=False):
    """Case-insensitive lookup by name, scoped to a company when parent_id is set.

    Used by lead ingest when the payload has no email, and by the client-markdown
    importer for the same reason. parent_id None matches a partner with no known
    employer. is_company scopes the match to the same company/person distinction
    as the incoming record — a company-flagged lead must not match (or create a
    duplicate of) an existing person row with the same name, and vice versa.
    """
    name = (name or "").strip()
    if not name:
        return None
    is_company_flag = 1 if is_company else 0
    with get_db() as db:
        if parent_id:
            row = db.execute(
                """SELECT * FROM partners
                   WHERE is_company = ? AND lower(name) = lower(?) AND parent_id = ?
                   LIMIT 1""",
                (is_company_flag, name, parent_id),
            ).fetchone()
        else:
            row = db.execute(
                """SELECT * FROM partners
                   WHERE is_company = ? AND lower(name) = lower(?) AND parent_id IS NULL
                   LIMIT 1""",
                (is_company_flag, name),
            ).fetchone()
        return dict(row) if row else None
