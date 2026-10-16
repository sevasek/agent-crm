"""Lead records: create, update, list, duplicates, merge, convert."""
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

from app.database import get_db
from app.services.activities import list_activities_for_deal, log_activity
from app.services.auth import clean_owner_key, sanitize_text
from app.services.catalog import get_service, get_service_by_slug
from app.services.deal_tags import apply_deal_tags, parse_tag_list, tags_for_deal
from app.services.deals import (
    get_deal,
    get_open_deal_for_partner_service,
    mark_lost,
)
from app.services.icp import build_matchable_row, score_fit
from app.services.lost_reasons import get_lost_reason, resolve_lost_reason
from app.services.offers import get_offer
from app.services.partners import (
    create_partner,
    find_all_partner_matches,
    get_company_by_name,
    get_partner,
    update_partner,
    website_key,
)
from app.services.phone import phone_digits
from app.services import pipeline_stages
from app.services.staleness import operator_today, parse_action_date

LEAD_IDENTITY_FIELDS = (
    "email", "phone", "website", "linkedin_url", "company_name", "partner_id",
)
SHARED_HOSTS = frozenset({
    "facebook.com", "instagram.com", "linkedin.com", "linktr.ee",
    "wixsite.com", "squarespace.com", "square.site", "wordpress.com",
    "google.com", "youtu.be", "youtube.com",
})
LEAD_CONTACT_FIELDS = (
    "name", "contact_name", "company_name", "email", "phone", "website",
    "title", "address", "linkedin_url", "x_url", "instagram_url",
    "facebook_url", "youtube_url", "industry", "preferred_channel",
)
LEAD_WRITE_FIELDS = LEAD_CONTACT_FIELDS + (
    "source", "pain_points", "goals", "next_action", "next_action_date",
    "external_ref", "expected_close",
)
LIST_SORTS = ("fit", "created", "updated", "next_action_date", "last_research")


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _is_empty(value):
    return value is None or value == ""


def _clean_int(value, *, minimum=None, maximum=None):
    if value in (None, ""):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if minimum is not None and number < minimum:
        return None
    if maximum is not None and number > maximum:
        return None
    return number


def _lead_names(fields: dict) -> list:
    names = []
    for key in ("company_name", "contact_name", "name"):
        text = (fields.get(key) or "").strip()
        if text and text.lower() not in {n.lower() for n in names}:
            names.append(text)
    return names


def _linkedin_key(url: str) -> str:
    raw = (url or "").strip().lower()
    if not raw:
        return ""
    parts = urlsplit(raw if "://" in raw else "https://" + raw)
    path = parts.path.rstrip("/")
    return urlunsplit(("", parts.netloc, path, "", "")).lstrip("/")


def _host_of_website_key(key: str) -> str:
    host = (key or "").split("/", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    return host


def _is_shared_host(key: str) -> bool:
    host = _host_of_website_key(key)
    if host in SHARED_HOSTS:
        return True
    return any(host.endswith("." + blocked) for blocked in SHARED_HOSTS)


def lead_has_identity(fields: dict) -> bool:
    """Invariant I4."""
    if fields.get("partner_id"):
        return True
    return any((fields.get(key) or "").strip() for key in LEAD_IDENTITY_FIELDS if key != "partner_id")


def create_lead(
    *,
    company_name="", contact_name="", name="", email="", phone="", website="",
    title="", address="", linkedin_url="", x_url="", instagram_url="",
    facebook_url="", youtube_url="", industry="", team_size=None,
    preferred_channel="", source="", tags=None, pain_points="", goals="",
    value_estimate=None, probability=None, priority=0, expected_close="",
    next_action="", next_action_date="", owner="", owner_key="",
    external_ref="", service_slug="", partner_id=None, parent_deal_id=None,
):
    """Returns (lead_dict, None) or (None, error_code). Never creates a partner."""
    if parent_deal_id not in (None, "", 0, "0"):
        return None, "invalid"  # I5
    service_id = None
    slug = (service_slug or "").strip().lower()
    if slug:
        service = get_service_by_slug(slug)
        if not service:
            return None, "invalid_service"
        service_id = service["id"]
    resolved_partner = None
    if partner_id not in (None, "", 0, "0"):
        resolved_partner = get_partner(partner_id)
        if not resolved_partner:
            return None, "partner_not_found"
        partner_id = resolved_partner["id"]
    else:
        partner_id = None
    fields = {
        "name": sanitize_text(name, max_len=200) or None,
        "contact_name": sanitize_text(contact_name, max_len=200) or None,
        "company_name": sanitize_text(company_name, max_len=200) or None,
        "email": sanitize_text(email, max_len=254) or None,
        "phone": sanitize_text(phone, max_len=50) or None,
        "website": sanitize_text(website, max_len=254) or None,
        "title": sanitize_text(title, max_len=120) or None,
        "address": sanitize_text(address, max_len=300) or None,
        "linkedin_url": sanitize_text(linkedin_url, max_len=300) or None,
        "x_url": sanitize_text(x_url, max_len=300) or None,
        "instagram_url": sanitize_text(instagram_url, max_len=300) or None,
        "facebook_url": sanitize_text(facebook_url, max_len=300) or None,
        "youtube_url": sanitize_text(youtube_url, max_len=300) or None,
        "industry": sanitize_text(industry, max_len=120) or None,
        "preferred_channel": (preferred_channel or "").strip().lower() or None,
        "source": sanitize_text(source, max_len=120) or None,
        "pain_points": sanitize_text(pain_points, max_len=2000, allow_newlines=True) or None,
        "goals": sanitize_text(goals, max_len=2000, allow_newlines=True) or None,
        "next_action": sanitize_text(next_action, max_len=500) or None,
        "next_action_date": next_action_date or None,
        "external_ref": sanitize_text(external_ref, max_len=200) or None,
        "expected_close": expected_close or None,
        "partner_id": partner_id,
    }
    if fields["preferred_channel"] not in (None, "email", "phone", "text"):
        fields["preferred_channel"] = None
    if not lead_has_identity(fields):
        return None, "invalid"
    owner_value = clean_owner_key(owner_key or owner)
    now = _now()
    with get_db() as db:
        db.execute(
            """INSERT INTO deals (
                   partner_id, service_id, stage, type, name, contact_name, company_name,
                   email, phone, website, title, address, linkedin_url, x_url,
                   instagram_url, facebook_url, youtube_url, industry, team_size,
                   preferred_channel, source, value_estimate, pain_points, goals,
                   next_action, next_action_date, owner_key, external_ref,
                   probability, priority, expected_close, active, created_at, updated_at
               ) VALUES (
                   ?, ?, 'new', 'lead', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                   ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?
               )""",
            (
                partner_id, service_id,
                fields["name"], fields["contact_name"], fields["company_name"],
                fields["email"], fields["phone"], fields["website"], fields["title"],
                fields["address"], fields["linkedin_url"], fields["x_url"],
                fields["instagram_url"], fields["facebook_url"], fields["youtube_url"],
                fields["industry"], _clean_int(team_size, minimum=0),
                fields["preferred_channel"], fields["source"], value_estimate,
                fields["pain_points"], fields["goals"], fields["next_action"],
                fields["next_action_date"], owner_value, fields["external_ref"],
                _clean_int(probability, minimum=0, maximum=100),
                _clean_int(priority, minimum=0, maximum=3) or 0,
                fields["expected_close"], now, now,
            ),
        )
        db.commit()
        lead_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    cleaned_tags, _err = parse_tag_list(tags) if tags else (None, None)
    if cleaned_tags:
        apply_deal_tags(lead_id, replace=cleaned_tags)
    return get_lead(lead_id, include_extras=False), None


def update_lead(lead_id: int, *, fill_empty_only=True, tags=None, add_tags=None,
                remove_tags=None, **fields):
    lead = get_deal(lead_id)
    if not lead:
        return None, "not_found"
    if lead.get("type") != "lead":
        return None, "not_a_lead"
    if not lead.get("active", 1):
        return None, "lead_lost"
    if "parent_deal_id" in fields and fields["parent_deal_id"] not in (None, "", 0, "0"):
        return None, "invalid"
    updates = {}
    if "service_slug" in fields:
        slug = (fields.pop("service_slug") or "").strip().lower()
        if slug:
            service = get_service_by_slug(slug)
            if not service:
                return None, "invalid_service"
            updates["service_id"] = service["id"]
        else:
            updates["service_id"] = None
    if "partner_id" in fields:
        pid = fields.pop("partner_id")
        if pid in (None, "", 0, "0"):
            updates["partner_id"] = None
        else:
            partner = get_partner(pid)
            if not partner:
                return None, "partner_not_found"
            updates["partner_id"] = partner["id"]
    if "owner" in fields and "owner_key" not in fields:
        fields["owner_key"] = fields.pop("owner")
    elif "owner" in fields:
        fields.pop("owner")
    text_limits = {
        "name": 200, "contact_name": 200, "company_name": 200, "email": 254,
        "phone": 50, "website": 254, "title": 120, "address": 300,
        "linkedin_url": 300, "x_url": 300, "instagram_url": 300,
        "facebook_url": 300, "youtube_url": 300, "industry": 120, "source": 120,
        "next_action": 500, "external_ref": 200,
    }
    long_text = {"pain_points": 2000, "goals": 2000}
    for key in LEAD_WRITE_FIELDS:
        if key not in fields:
            continue
        value = fields[key]
        if key in long_text:
            value = sanitize_text(value or "", max_len=long_text[key], allow_newlines=True) or None
        elif key in text_limits:
            value = sanitize_text(value or "", max_len=text_limits[key]) or None
        elif key == "preferred_channel":
            value = (value or "").strip().lower() or None
            if value not in ("email", "phone", "text"):
                value = None
        if fill_empty_only and not _is_empty(lead.get(key)):
            continue
        updates[key] = value
    if "team_size" in fields:
        incoming = _clean_int(fields["team_size"], minimum=0)
        if not fill_empty_only or _is_empty(lead.get("team_size")):
            updates["team_size"] = incoming
    if "value_estimate" in fields:
        if not fill_empty_only or _is_empty(lead.get("value_estimate")):
            updates["value_estimate"] = fields["value_estimate"]
    if "probability" in fields:
        if not fill_empty_only or _is_empty(lead.get("probability")):
            updates["probability"] = _clean_int(fields["probability"], minimum=0, maximum=100)
    if "priority" in fields:
        if not fill_empty_only or _is_empty(lead.get("priority")):
            updates["priority"] = _clean_int(fields["priority"], minimum=0, maximum=3) or 0
    if "owner_key" in fields:
        if not fill_empty_only or _is_empty(lead.get("owner_key")):
            updates["owner_key"] = clean_owner_key(fields["owner_key"])
    if "type" in fields:
        return None, "not_a_lead"
    merged = dict(lead)
    merged.update(updates)
    if not lead_has_identity(merged):
        return None, "invalid"
    # Validate tags before any write: a bad tag list must not leave field
    # updates committed behind an error return.
    replace, add, remove = None, None, None
    if tags is not None:
        replace, err = parse_tag_list(tags)
        if err:
            return None, "invalid"
    if add_tags is not None:
        add, err = parse_tag_list(add_tags)
        if err:
            return None, "invalid"
    if remove_tags is not None:
        remove, err = parse_tag_list(remove_tags)
        if err:
            return None, "invalid"
    if updates:
        updates["updated_at"] = _now()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        with get_db() as db:
            db.execute(
                f"UPDATE deals SET {set_clause} WHERE id = ?",
                list(updates.values()) + [lead_id],
            )
            db.commit()
    if replace is not None or add or remove:
        apply_deal_tags(lead_id, replace=replace, add=add, remove=remove)
    return get_lead(lead_id, include_extras=False), None


def find_duplicates(lead_id=None, **fields):
    """Strong and weak matches against open deal records and partners."""
    if lead_id:
        lead = get_deal(lead_id)
        if not lead:
            return None, "not_found"
        probe = dict(lead)
        probe.update({k: v for k, v in fields.items() if v not in (None, "")})
        skip_deal_id = lead["id"]
    else:
        probe = fields
        skip_deal_id = None
    email = (probe.get("email") or "").strip().lower()
    phone = phone_digits(probe.get("phone"))
    site = website_key(probe.get("website") or "")
    linkedin = _linkedin_key(probe.get("linkedin_url") or "")
    names = {n.lower(): n for n in _lead_names(probe)}
    company = (probe.get("company_name") or "").strip()

    won = pipeline_stages.won_stage_keys()
    with get_db() as db:
        deals = [dict(r) for r in db.execute("SELECT * FROM deals WHERE active = 1").fetchall()]
        partners = [dict(r) for r in db.execute("SELECT * FROM partners").fetchall()]

    hits = {}

    def _hit(kind, row_id, key, strength):
        if skip_deal_id and kind != "partner" and row_id == skip_deal_id:
            return
        slot = hits.setdefault((kind, row_id), {
            "kind": kind, "id": row_id, "matched_on": [], "strength": "weak",
        })
        if key not in slot["matched_on"]:
            slot["matched_on"].append(key)
        if strength == "strong":
            slot["strength"] = "strong"

    for deal in deals:
        if deal.get("stage") in won:
            continue
        kind = "lead" if deal.get("type") == "lead" else "opportunity"
        deal_email = (deal.get("email") or "").strip().lower()
        deal_phone = phone_digits(deal.get("phone"))
        deal_site = website_key(deal.get("website") or "")
        deal_li = _linkedin_key(deal.get("linkedin_url") or "")
        deal_names = {n.lower() for n in _lead_names(deal)}
        deal_company = (deal.get("company_name") or "").strip()
        if email and deal_email and email == deal_email:
            _hit(kind, deal["id"], "email", "strong")
        if phone and deal_phone and phone == deal_phone:
            if names and deal_names & set(names):
                _hit(kind, deal["id"], "phone_and_name", "strong")
            else:
                _hit(kind, deal["id"], "phone", "weak")
        if site and deal_site and site == deal_site:
            if names and deal_names & set(names):
                _hit(kind, deal["id"], "website_and_name", "strong")
            elif not _is_shared_host(site):
                _hit(kind, deal["id"], "website", "weak")
        if linkedin and deal_li and linkedin == deal_li:
            _hit(kind, deal["id"], "linkedin_url", "weak")
        if company and deal_company and company.lower() == deal_company.lower():
            _hit(kind, deal["id"], "company_name", "weak")

    for partner in partners:
        partner_email = (partner.get("email") or "").strip().lower()
        partner_phone = phone_digits(partner.get("phone"))
        partner_site = website_key(partner.get("website") or "")
        partner_li = _linkedin_key(partner.get("linkedin_url") or "")
        partner_name = (partner.get("name") or "").strip()
        if email and partner_email and email == partner_email:
            _hit("partner", partner["id"], "email", "strong")
        if phone and partner_phone and phone == partner_phone:
            if names and partner_name.lower() in names:
                _hit("partner", partner["id"], "phone_and_name", "strong")
            else:
                _hit("partner", partner["id"], "phone", "weak")
        if site and partner_site and site == partner_site:
            if names and partner_name.lower() in names:
                _hit("partner", partner["id"], "website_and_name", "strong")
            elif not _is_shared_host(site):
                _hit("partner", partner["id"], "website", "weak")
        if linkedin and partner_li and linkedin == partner_li:
            _hit("partner", partner["id"], "linkedin_url", "weak")
        if company and partner_name and company.lower() == partner_name.lower():
            _hit("partner", partner["id"], "company_name", "weak")

    results = sorted(hits.values(), key=lambda r: (0 if r["strength"] == "strong" else 1, r["kind"], r["id"]))
    return results, None


def _research_stats(deal_id: int):
    with get_db() as db:
        row = db.execute(
            """SELECT COUNT(*) AS n, MAX(occurred_at) AS last_at
               FROM activities WHERE deal_id = ? AND type = 'research'""",
            (deal_id,),
        ).fetchone()
    return int(row["n"] or 0), row["last_at"]


def _conversion_readiness(lead, duplicates, fit):
    missing = []
    if not lead.get("service_id"):
        missing.append("service")
    if not lead_has_identity(lead):
        missing.append("identity")
    warnings = []
    if any(d["kind"] == "partner" for d in duplicates):
        warnings.append("possible_duplicate_partner")
    if any(d["kind"] == "lead" for d in duplicates):
        warnings.append("possible_duplicate_lead")
    notes, _last = _research_stats(lead["id"])
    if notes == 0:
        warnings.append("no_research_notes")
    if not (lead.get("contact_name") or "").strip():
        warnings.append("no_contact_person")
    if not (lead.get("email") or "").strip() and not (lead.get("phone") or "").strip():
        warnings.append("no_email_or_phone")
    return {
        "ready": not missing,
        "missing": missing,
        "warnings": warnings,
        "fit_score": fit["score"],
        "fit_max": fit["max"],
    }


def _fit_for_lead(lead):
    partner = get_partner(lead.get("partner_id")) if lead.get("partner_id") else {}
    return score_fit(build_matchable_row(partner or {}, lead))


def get_lead(lead_id: int, *, include_extras=True):
    lead = get_deal(lead_id)
    if not lead:
        return None
    if lead.get("type") != "lead":
        return None
    lead["tags"] = tags_for_deal(lead_id)
    if not include_extras:
        return lead
    activities = list_activities_for_deal(lead_id, research_first=True)
    research_count, last_researched = _research_stats(lead_id)
    duplicates, _err = find_duplicates(lead_id=lead_id)
    fit = _fit_for_lead(lead)
    partner = get_partner(lead.get("partner_id")) if lead.get("partner_id") else None
    lead["activities"] = activities
    lead["last_researched_at"] = last_researched
    lead["research_note_count"] = research_count
    lead["possible_duplicates"] = duplicates or []
    lead["conversion_readiness"] = _conversion_readiness(lead, duplicates or [], fit)
    lead["fit"] = {
        "score": fit["score"],
        "max": fit["max"],
        "matched": [c.get("label") or c.get("field") for c in fit["matched"]],
        "unmatched": [c.get("label") or c.get("field") for c in fit["unmatched"]],
    }
    lead["partner"] = partner
    if lead.get("lost_reason_id"):
        lead["lost_reason"] = get_lost_reason(lead["lost_reason_id"])
    return lead


def list_leads(*, query="", owner="", service_slug="", tags=None, source_prefix="",
               min_fit=None, due_only=False, include_lost=False, sort="updated",
               limit=50):
    from app.services.deal_tags import coerce_tag_filter

    wanted, impossible = coerce_tag_filter(tags)
    if impossible:
        return []
    limit = max(1, min(int(limit or 50), 50))
    sort_key = sort if sort in LIST_SORTS else "updated"
    rows = []
    sql = "SELECT * FROM deals WHERE type = 'lead'"
    params = []
    if not include_lost:
        sql += " AND active = 1"
    if owner:
        sql += " AND owner_key = ?"
        params.append(clean_owner_key(owner))
    if service_slug:
        service = get_service_by_slug(service_slug.strip().lower())
        if not service:
            return []
        sql += " AND service_id = ?"
        params.append(service["id"])
    if source_prefix:
        sql += " AND lower(IFNULL(source, '')) LIKE lower(?) || '%'"
        params.append(source_prefix)
    with get_db() as db:
        deals = [dict(r) for r in db.execute(sql, params).fetchall()]
    q = (query or "").strip().lower()
    today = operator_today()
    for deal in deals:
        if wanted:
            deal_tags = set(tags_for_deal(deal["id"]))
            if not set(wanted) <= deal_tags:
                continue
        if q:
            hay = " ".join(
                str(deal.get(k) or "")
                for k in ("name", "company_name", "contact_name", "email", "phone", "website")
            ).lower()
            if q not in hay:
                continue
        if due_only:
            action = parse_action_date(deal.get("next_action_date"))
            if action is None or action > today:
                continue
        deal["tags"] = tags_for_deal(deal["id"])
        fit = _fit_for_lead(deal)
        deal["fit_score"] = fit["score"]
        if min_fit is not None and fit["score"] < int(min_fit):
            continue
        notes, last_at = _research_stats(deal["id"])
        deal["last_researched_at"] = last_at
        deal["research_note_count"] = notes
        deal["conversion_readiness"] = _conversion_readiness(deal, [], fit)
        rows.append(deal)
    if sort_key == "fit":
        rows.sort(key=lambda d: (-(d.get("fit_score") or 0), d["id"]))
    elif sort_key == "created":
        rows.sort(key=lambda d: (d.get("created_at") or "", d["id"]), reverse=True)
    elif sort_key == "next_action_date":
        rows.sort(key=lambda d: (d.get("next_action_date") or "9999", d["id"]))
    elif sort_key == "last_research":
        rows.sort(key=lambda d: (d.get("last_researched_at") or "", d["id"]), reverse=True)
    else:
        rows.sort(key=lambda d: (d.get("updated_at") or "", d["id"]), reverse=True)
    return rows[:limit]


def _copy_lead_to_partner(partner, lead):
    profile = {
        "phone": lead.get("phone"),
        "title": lead.get("title"),
        "website": lead.get("website"),
        "address": lead.get("address"),
        "linkedin_url": lead.get("linkedin_url"),
        "x_url": lead.get("x_url"),
        "instagram_url": lead.get("instagram_url"),
        "facebook_url": lead.get("facebook_url"),
        "youtube_url": lead.get("youtube_url"),
        "preferred_channel": lead.get("preferred_channel"),
        "industry": lead.get("industry"),
        "team_size": lead.get("team_size"),
        "email": lead.get("email"),
    }
    updates = {}
    for field, value in profile.items():
        if _is_empty(value):
            continue
        if _is_empty(partner.get(field)):
            updates[field] = value
    if updates:
        update_partner(partner["id"], **updates)


def _resolve_convert_partner(lead, partner_action, partner_id):
    action = (partner_action or "auto").strip().lower()
    if action not in ("auto", "link", "create"):
        action = "auto"
    if action == "link":
        partner = get_partner(partner_id)
        if not partner:
            return None, False, "partner_not_found", []
        return partner, False, None, []
    name = (lead.get("contact_name") or lead.get("name") or "").strip()
    company_name = (lead.get("company_name") or "").strip()
    if action == "create":
        company_id = None
        company_created = False
        if company_name:
            existing = get_company_by_name(company_name)
            if existing:
                company_id = existing["id"]
            else:
                company_id = create_partner(
                    company_name, is_company=True,
                    website=lead.get("website") or "",
                    industry=lead.get("industry") or "",
                    team_size=lead.get("team_size"),
                )
                company_created = True
        if not name and not company_name:
            name = (lead.get("email") or lead.get("website") or lead.get("phone") or "Lead").strip()
        if name:
            person_id = create_partner(
                name, is_company=False, parent_id=company_id,
                email=lead.get("email") or "",
                phone=lead.get("phone") or "",
                website=lead.get("website") or "",
                title=lead.get("title") or "",
                address=lead.get("address") or "",
                linkedin_url=lead.get("linkedin_url") or "",
                x_url=lead.get("x_url") or "",
                instagram_url=lead.get("instagram_url") or "",
                facebook_url=lead.get("facebook_url") or "",
                youtube_url=lead.get("youtube_url") or "",
                preferred_channel=lead.get("preferred_channel") or "",
                industry=lead.get("industry") or "",
                team_size=lead.get("team_size"),
            )
            return get_partner(person_id), True, None, []
        if company_id:
            return get_partner(company_id), company_created, None, []
        return None, False, "partner_not_found", []
    matches = find_all_partner_matches(
        email=lead.get("email") or "",
        name=name or company_name,
        phone=lead.get("phone") or "",
        website=lead.get("website") or "",
        is_company=bool(company_name and not name),
        parent_id=None,
        company_name=company_name,
    )
    if len(matches) > 1:
        return None, False, "partner_ambiguous", matches
    if len(matches) == 1:
        return matches[0], False, None, []
    return _resolve_convert_partner(lead, "create", None)


def convert_lead(
    lead_id: int, *, partner_action="auto", partner_id=None, service_slug="",
    stage=None, value_estimate=None, expected_close=None, probability=None,
    offer_id=None, next_action=None, next_action_date=None, owner=None,
    owner_key=None, note="",
):
    """Returns (result_dict, None) or (None, error, extra)."""
    lead = get_deal(lead_id)
    if not lead or lead.get("type") != "lead":
        return None, "not_a_lead"
    if not lead.get("active", 1):
        return None, "lead_lost"
    service = None
    slug = (service_slug or "").strip().lower()
    if slug:
        service = get_service_by_slug(slug)
        if not service:
            return None, "missing_service"
    elif lead.get("service_id"):
        service = get_service(lead["service_id"])
    if not service:
        return None, "missing_service"
    partner, partner_created, error, candidates = _resolve_convert_partner(
        lead, partner_action, partner_id,
    )
    if error == "partner_ambiguous":
        return {
            "candidates": [
                {"id": p["id"], "name": p.get("name"), "email": p.get("email")}
                for p in candidates
            ]
        }, "partner_ambiguous"
    if error:
        return None, error
    other = get_open_deal_for_partner_service(partner["id"], service["id"])
    if other and other["id"] != lead_id:
        return {"deal_id": other["id"]}, "duplicate_open_opportunity"
    _copy_lead_to_partner(partner, lead)
    target_stage = stage if stage and pipeline_stages.get_stage(stage) else pipeline_stages.default_stage_key()
    now = _now()
    owner_value = clean_owner_key(owner_key if owner_key not in (None, "") else owner)
    offer_value = offer_id if offer_id and get_offer(offer_id) else lead.get("offer_id")
    with get_db() as db:
        db.execute(
            """UPDATE deals SET
                   partner_id = ?, service_id = ?, type = 'opportunity',
                   date_conversion = ?, stage = ?,
                   value_estimate = COALESCE(?, value_estimate),
                   expected_close = COALESCE(?, expected_close),
                   probability = COALESCE(?, probability),
                   offer_id = COALESCE(?, offer_id),
                   next_action = COALESCE(?, next_action),
                   next_action_date = COALESCE(?, next_action_date),
                   owner_key = COALESCE(?, owner_key),
                   updated_at = ?
               WHERE id = ?""",
            (
                partner["id"], service["id"], now, target_stage,
                value_estimate, expected_close,
                _clean_int(probability, minimum=0, maximum=100) if probability is not None else None,
                offer_value,
                sanitize_text(next_action, max_len=500) if next_action is not None else None,
                next_action_date if next_action_date is not None else None,
                owner_value or None,
                now, lead_id,
            ),
        )
        db.execute(
            "UPDATE activities SET partner_id = ? WHERE deal_id = ? AND partner_id IS NULL",
            (partner["id"], lead_id),
        )
        db.execute(
            "UPDATE delegated_tasks SET partner_id = ? WHERE deal_id = ? AND partner_id IS NULL",
            (partner["id"], lead_id),
        )
        db.commit()
    fit = _fit_for_lead(lead)
    matched = ", ".join(c.get("label") or c.get("field") for c in fit["matched"]) or "none"
    body = (
        f"Converted lead to opportunity ({partner_action or 'auto'}). "
        f"Fit {fit['score']}/{fit['max']} matched: {matched}."
    )
    if note:
        body += f" {sanitize_text(note, max_len=2000, allow_newlines=True)}"
    activity_id = log_activity(partner["id"], "system", body, deal_id=lead_id)
    opportunity = get_deal(lead_id)
    return {
        "opportunity": opportunity,
        "partner": get_partner(partner["id"]),
        "partner_created": partner_created,
        "activity_id": activity_id,
    }, None


def merge_leads(target_id: int, source_ids):
    """Fill-empty + move children; sources become lost with merged_into_id."""
    if not source_ids or len(source_ids) > 10:
        return None, "invalid_merge"
    target = get_deal(target_id)
    if not target:
        return None, "not_found"
    if not target.get("active", 1):
        return None, "invalid_merge"
    sources = []
    seen = set()
    for raw in source_ids:
        try:
            sid = int(raw)
        except (TypeError, ValueError):
            return None, "not_found"
        if sid in seen:
            continue
        seen.add(sid)
        if sid == target_id:
            return None, "invalid_merge"
        row = get_deal(sid)
        if not row:
            return None, "not_found"
        if row.get("type") != "lead" or not row.get("active", 1):
            return None, "invalid_merge"
        if target.get("type") == "lead" or (
            target.get("type") == "opportunity" and row.get("type") == "lead"
        ):
            sources.append(row)
        else:
            return None, "invalid_merge"
    if target.get("type") not in ("lead", "opportunity"):
        return None, "invalid_merge"
    # Resolve the merge lost reason before any writes: if it was renamed or
    # deleted, fail up front rather than after children already moved.
    if not resolve_lost_reason(lost_reason="Duplicate"):
        return None, "invalid_lost_reason"
    sources.sort(key=lambda r: (r.get("created_at") or "", r["id"]))
    fill_keys = list(LEAD_WRITE_FIELDS) + [
        "team_size", "value_estimate", "probability", "priority",
        "service_id", "partner_id", "owner_key",
    ]
    updates = {}
    for source in sources:
        for key in fill_keys:
            if _is_empty(target.get(key)) and not _is_empty(source.get(key)) and key not in updates:
                updates[key] = source[key]
    if target.get("type") == "lead" and updates.get("parent_deal_id"):
        updates.pop("parent_deal_id", None)
    if updates:
        updates["updated_at"] = _now()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        with get_db() as db:
            db.execute(
                f"UPDATE deals SET {set_clause} WHERE id = ?",
                list(updates.values()) + [target_id],
            )
            db.commit()
        target = get_deal(target_id)
    partner_id = target.get("partner_id")
    merged_ids = []
    for source in sources:
        with get_db() as db:
            db.execute(
                "UPDATE activities SET deal_id = ? WHERE deal_id = ?",
                (target_id, source["id"]),
            )
            if partner_id:
                db.execute(
                    """UPDATE activities SET partner_id = ?
                       WHERE deal_id = ? AND partner_id IS NULL""",
                    (partner_id, target_id),
                )
            db.execute(
                "INSERT OR IGNORE INTO deal_tags (deal_id, tag) SELECT ?, tag FROM deal_tags WHERE deal_id = ?",
                (target_id, source["id"]),
            )
            db.execute("DELETE FROM deal_tags WHERE deal_id = ?", (source["id"],))
            db.execute(
                "UPDATE delegated_tasks SET deal_id = ? WHERE deal_id = ?",
                (target_id, source["id"]),
            )
            if partner_id:
                db.execute(
                    """UPDATE delegated_tasks SET partner_id = ?
                       WHERE deal_id = ? AND partner_id IS NULL""",
                    (partner_id, target_id),
                )
            db.commit()
        marked, error = mark_lost(source["id"], lost_reason="Duplicate")
        if error:
            return None, error
        with get_db() as db:
            db.execute(
                "UPDATE deals SET merged_into_id = ? WHERE id = ?",
                (target_id, source["id"]),
            )
            db.commit()
        merged_ids.append(source["id"])
    log_activity(
        target.get("partner_id"),
        "system",
        "Merged leads: " + ", ".join(f"#{i}" for i in merged_ids),
        deal_id=target_id,
    )
    return {"target": get_deal(target_id), "merged_ids": merged_ids}, None
