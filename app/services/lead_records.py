"""Leads and opportunities on the deals table.

A lead is an unqualified deal record. Conversion flips type on the same
row and resolves a partner. Lost is active = 0 plus a lost reason.
"""
from datetime import datetime

from app.database import IntegrityConflict, get_db
from app.services.activities import log_activity
from app.services.auth import clean_owner_key, sanitize_text
from app.services.catalog import get_service, get_service_by_slug
from app.services.deal_tags import apply_deal_tags, parse_tag_list, tags_for_deal
from app.services.deals import get_deal, get_open_deal_for_partner_service
from app.services.icp import build_matchable_row, score_fit
from app.services.offers import get_offer
from app.services.partners import (
    _clean_preferred_channel,
    _clean_team_size,
    _website_key,
    create_partner,
    get_company_by_name,
    get_partner,
    partner_match_candidates,
    update_partner,
)
from app.services.phone import phone_digits
from app.services import pipeline_stages

_LEAD_CONTACT = (
    "name", "contact_name", "company_name", "email", "phone", "website",
    "title", "address", "linkedin_url", "x_url", "instagram_url",
    "facebook_url", "youtube_url", "industry", "team_size", "preferred_channel",
)
_LEAD_EXTRAS = (
    "source", "pain_points", "goals", "value_estimate", "probability",
    "priority", "expected_close", "next_action", "next_action_date",
    "external_ref", "owner_key", "service_id", "partner_id", "offer_id",
)
_TEXT_LIMITS = {
    "name": 200, "contact_name": 200, "company_name": 200, "email": 254,
    "phone": 50, "website": 300, "title": 120, "address": 300,
    "linkedin_url": 300, "x_url": 300, "instagram_url": 300,
    "facebook_url": 300, "youtube_url": 300, "industry": 120,
    "source": 120, "next_action": 500, "external_ref": 200, "expected_close": 40,
    "lost_note": 2000,
}
_SHARED_HOSTS = {
    "facebook.com", "instagram.com", "linkedin.com", "linktr.ee",
    "wixsite.com", "squarespace.com", "square.site", "wordpress.com",
    "google.com", "youtu.be", "youtube.com",
}
_STRONG = ("email", "phone_and_name", "website_and_name")
_MERGE_FIELDS = _LEAD_CONTACT + (
    "source", "pain_points", "goals", "value_estimate", "next_action",
    "next_action_date", "external_ref", "partner_id", "service_id",
)


def _now():
    return datetime.utcnow().isoformat()


def _empty(value):
    return value is None or value == ""


def _text(value, limit):
    if _empty(value):
        return None
    return sanitize_text(value, max_len=limit) or None


def _fail(error, **extra):
    payload = {"ok": False, "error": error}
    payload.update(extra)
    return payload


def _partner_count():
    with get_db() as db:
        return db.execute("SELECT COUNT(*) AS n FROM partners").fetchone()["n"]


def list_lost_reasons(*, active_only=False):
    sql = "SELECT * FROM lost_reasons"
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY id"
    with get_db() as db:
        return [dict(row) for row in db.execute(sql).fetchall()]


def get_lost_reason(reason_id):
    if not reason_id:
        return None
    with get_db() as db:
        row = db.execute("SELECT * FROM lost_reasons WHERE id = ?", (reason_id,)).fetchone()
        return dict(row) if row else None


def get_lost_reason_by_name(name):
    name = (name or "").strip()
    if not name:
        return None
    with get_db() as db:
        row = db.execute("SELECT * FROM lost_reasons WHERE name = ?", (name,)).fetchone()
        return dict(row) if row else None


def create_lost_reason(name, *, triggers_nurture=False):
    name = (name or "").strip()
    if not name:
        return _fail("invalid")
    if get_lost_reason_by_name(name):
        return _fail("duplicate_name")
    try:
        with get_db() as db:
            db.execute(
                "INSERT INTO lost_reasons (name, triggers_nurture) VALUES (?, ?)",
                (name, 1 if triggers_nurture else 0),
            )
            db.commit()
            reason_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    except IntegrityConflict:
        return _fail("duplicate_name")
    return {"ok": True, "lost_reason": get_lost_reason(reason_id)}


def update_lost_reason(reason_id, *, name=None, triggers_nurture=None, active=None):
    reason = get_lost_reason(reason_id)
    if not reason:
        return _fail("not_found")
    updates = {}
    if name is not None:
        cleaned = name.strip()
        if not cleaned:
            return _fail("invalid")
        other = get_lost_reason_by_name(cleaned)
        if other and other["id"] != reason["id"]:
            return _fail("duplicate_name")
        updates["name"] = cleaned
    if triggers_nurture is not None:
        updates["triggers_nurture"] = 1 if triggers_nurture else 0
    if active is not None:
        updates["active"] = 1 if active else 0
    if not updates:
        return {"ok": True, "lost_reason": reason}
    set_clause = ", ".join(f"{key} = ?" for key in updates)
    try:
        with get_db() as db:
            db.execute(
                f"UPDATE lost_reasons SET {set_clause}, updated_at = ? WHERE id = ?",
                (*updates.values(), _now(), reason_id),
            )
            db.commit()
    except IntegrityConflict:
        return _fail("duplicate_name")
    return {"ok": True, "lost_reason": get_lost_reason(reason_id)}


def _names(fields):
    found = set()
    for key in ("company_name", "contact_name", "name"):
        value = (fields.get(key) or "").strip().lower()
        if value:
            found.add(value)
    return found


def _linkedin_key(value):
    text = (value or "").strip().lower()
    if not text:
        return ""
    return text.split("?", 1)[0].rstrip("/")


def _shared_host(website_key):
    host = (website_key or "").split("/", 1)[0]
    return host in _SHARED_HOSTS


def _match_fields(row, kind):
    if kind != "partner":
        return row
    company = bool(row.get("is_company"))
    return {
        "email": row.get("email"),
        "phone": row.get("phone"),
        "website": row.get("website"),
        "linkedin_url": row.get("linkedin_url"),
        "company_name": row.get("name") if company else "",
        "contact_name": "" if company else row.get("name"),
        "name": row.get("name"),
    }


def _matched_keys(probe, candidate):
    matched = []
    probe_email = (probe.get("email") or "").strip().lower()
    cand_email = (candidate.get("email") or "").strip().lower()
    if probe_email and probe_email == cand_email:
        matched.append("email")
    probe_phone = phone_digits(probe.get("phone") or "")
    cand_phone = phone_digits(candidate.get("phone") or "")
    names_overlap = bool(_names(probe) & _names(candidate))
    if probe_phone and probe_phone == cand_phone:
        if names_overlap:
            matched.append("phone_and_name")
        else:
            matched.append("phone")
    probe_web = _website_key(probe.get("website") or "")
    cand_web = _website_key(candidate.get("website") or "")
    if probe_web and probe_web == cand_web:
        if names_overlap:
            matched.append("website_and_name")
        elif not _shared_host(probe_web):
            matched.append("website")
    probe_li = _linkedin_key(probe.get("linkedin_url"))
    cand_li = _linkedin_key(candidate.get("linkedin_url"))
    if probe_li and probe_li == cand_li:
        matched.append("linkedin_url")
    probe_company = (probe.get("company_name") or "").strip().lower()
    cand_company = (candidate.get("company_name") or "").strip().lower()
    if probe_company and probe_company == cand_company:
        matched.append("company_name")
    if not matched:
        return None
    strength = "strong" if any(key in _STRONG for key in matched) else "weak"
    return {"matched_on": matched, "strength": strength}


def find_duplicates(*, lead_id=None, exclude_id=None, **fields):
    """Open deal records and partners that share a strong or weak key."""
    probe = dict(fields)
    if lead_id:
        lead = get_deal(lead_id)
        if not lead:
            return _fail("not_found")
        probe = lead
        exclude_id = lead["id"]
    hits = []
    with get_db() as db:
        deals = [dict(row) for row in db.execute("SELECT * FROM deals WHERE active = 1").fetchall()]
        partners = [dict(row) for row in db.execute("SELECT * FROM partners").fetchall()]
    for deal in deals:
        if exclude_id and deal["id"] == exclude_id:
            continue
        candidate = deal
        if deal.get("type") == "opportunity" and deal.get("partner_id"):
            partner = get_partner(deal["partner_id"]) or {}
            candidate = dict(deal)
            if _empty(candidate.get("email")):
                candidate["email"] = partner.get("email")
            if _empty(candidate.get("phone")):
                candidate["phone"] = partner.get("phone")
            if _empty(candidate.get("website")):
                candidate["website"] = partner.get("website")
            if _empty(candidate.get("company_name")) and partner.get("is_company"):
                candidate["company_name"] = partner.get("name")
            if _empty(candidate.get("contact_name")) and not partner.get("is_company"):
                candidate["contact_name"] = partner.get("name")
                candidate["name"] = partner.get("name")
        found = _matched_keys(probe, candidate)
        if not found:
            continue
        hits.append({
            "kind": "lead" if deal.get("type") == "lead" else "opportunity",
            "id": deal["id"],
            **found,
        })
    for partner in partners:
        found = _matched_keys(probe, _match_fields(partner, "partner"))
        if not found:
            continue
        hits.append({"kind": "partner", "id": partner["id"], **found})
    hits.sort(key=lambda hit: (0 if hit["strength"] == "strong" else 1, hit["kind"], hit["id"]))
    if lead_id and not get_deal(lead_id):
        return _fail("not_found")
    return {"ok": True, "matches": hits}


def _has_identity(fields):
    return any(not _empty(fields.get(key)) for key in (
        "email", "phone", "website", "linkedin_url", "company_name", "partner_id",
    ))


def _fit_for(deal):
    """Existing fit function. Lead-only inputs land in P5."""
    partner = get_partner(deal.get("partner_id")) if deal.get("partner_id") else {}
    fit = score_fit(build_matchable_row(partner or {}, deal))
    return {
        "score": fit["score"],
        "max": fit["max"],
        "matched": [{"id": row["id"], "label": row.get("label"), "field": row["field"], "weight": row["weight"]} for row in fit["matched"]],
        "unmatched": [{"id": row["id"], "label": row.get("label"), "field": row["field"], "weight": row["weight"]} for row in fit["unmatched"]],
    }


def _research_stats(deal_id):
    with get_db() as db:
        row = db.execute(
            """SELECT COUNT(*) AS n, MAX(occurred_at) AS last_at
               FROM activities WHERE deal_id = ? AND type = 'research'""",
            (deal_id,),
        ).fetchone()
    return int(row["n"] or 0), row["last_at"]


def conversion_readiness(deal, duplicates=None):
    if duplicates is None:
        found = find_duplicates(exclude_id=deal["id"], **deal)
        duplicates = found.get("matches") or []
    missing = []
    if not deal.get("service_id"):
        missing.append("service")
    if not _has_identity(deal):
        missing.append("identity")
    else:
        ambiguous = partner_match_candidates(
            email=deal.get("email"),
            name=deal.get("contact_name") or deal.get("company_name") or deal.get("name") or "",
            phone=deal.get("phone"),
            website=deal.get("website"),
            is_company=bool(deal.get("company_name")) and not deal.get("contact_name"),
            parent_id=None,
        )
        if len(ambiguous) > 1 and not deal.get("partner_id"):
            missing.append("identity")
    note_count, _last = _research_stats(deal["id"]) if deal.get("id") else (0, None)
    warnings = []
    if any(hit["kind"] == "partner" for hit in duplicates):
        warnings.append("possible_duplicate_partner")
    if any(hit["kind"] == "lead" for hit in duplicates):
        warnings.append("possible_duplicate_lead")
    if note_count == 0:
        warnings.append("no_research_notes")
    if _empty(deal.get("contact_name")):
        warnings.append("no_contact_person")
    if _empty(deal.get("email")) and _empty(deal.get("phone")):
        warnings.append("no_email_or_phone")
    fit = _fit_for(deal) if deal.get("id") else {"score": 0, "max": 0}
    return {
        "ready": not missing,
        "missing": missing,
        "warnings": warnings,
        "fit_score": fit["score"],
        "fit_max": fit["max"],
    }


def _clean_lead_fields(raw, *, partial=False):
    """Return (fields, error). partial=True keeps omitted keys out."""
    fields = {}
    for key in _LEAD_CONTACT + _LEAD_EXTRAS + ("tags", "add_tags", "remove_tags"):
        if partial and key not in raw:
            continue
        if key not in raw and not partial:
            continue
        value = raw.get(key)
        if key in _TEXT_LIMITS:
            if key in ("pain_points", "goals"):
                fields[key] = sanitize_text(value, max_len=2000, allow_newlines=True) or None
            else:
                fields[key] = _text(value, _TEXT_LIMITS[key])
        elif key == "team_size":
            fields[key] = _clean_team_size(value)
        elif key == "preferred_channel":
            fields[key] = _clean_preferred_channel(value)
        elif key == "probability":
            if _empty(value):
                fields[key] = None
            else:
                try:
                    number = int(value)
                except (TypeError, ValueError):
                    return None, "invalid"
                if number < 0 or number > 100:
                    return None, "invalid"
                fields[key] = number
        elif key == "priority":
            if _empty(value):
                fields[key] = 0
            else:
                try:
                    number = int(value)
                except (TypeError, ValueError):
                    return None, "invalid"
                if number < 0 or number > 3:
                    return None, "invalid"
                fields[key] = number
        elif key == "value_estimate":
            if _empty(value):
                fields[key] = None
            else:
                try:
                    fields[key] = float(value)
                except (TypeError, ValueError):
                    return None, "invalid"
        elif key in ("partner_id", "service_id", "offer_id"):
            if _empty(value):
                fields[key] = None
            else:
                try:
                    fields[key] = int(value)
                except (TypeError, ValueError):
                    return None, "invalid"
        elif key in ("tags", "add_tags", "remove_tags"):
            fields[key] = value
        else:
            fields[key] = value
    return fields, None


def _apply_tags(deal_id, fields):
    if "tags" in fields and fields["tags"] is not None:
        cleaned, error = parse_tag_list(fields["tags"])
        if error:
            return error
        apply_deal_tags(deal_id, replace=cleaned or [])
    if fields.get("add_tags"):
        cleaned, error = parse_tag_list(fields["add_tags"])
        if error:
            return error
        apply_deal_tags(deal_id, add=cleaned or [])
    if fields.get("remove_tags"):
        cleaned, error = parse_tag_list(fields["remove_tags"])
        if error:
            return error
        apply_deal_tags(deal_id, remove=cleaned or [])
    return None


def create_lead(*, log_create=True, **raw):
    before_partners = _partner_count()
    fields, error = _clean_lead_fields(raw, partial=False)
    if error:
        return _fail(error)
    if raw.get("service_slug"):
        service = get_service_by_slug(str(raw["service_slug"]).strip())
        if not service:
            return _fail("invalid_service")
        fields["service_id"] = service["id"]
    if fields.get("partner_id") and not get_partner(fields["partner_id"]):
        return _fail("partner_not_found")
    if fields.get("offer_id") and not get_offer(fields["offer_id"]):
        fields["offer_id"] = None
    if not _has_identity(fields):
        return _fail("invalid")
    stage = pipeline_stages.default_stage_key() or "new"
    columns = [
        "type", "stage", "active", "priority",
        "name", "contact_name", "company_name", "email", "phone", "website",
        "title", "address", "linkedin_url", "x_url", "instagram_url",
        "facebook_url", "youtube_url", "industry", "team_size", "preferred_channel",
        "source", "pain_points", "goals", "value_estimate", "probability",
        "expected_close", "next_action", "next_action_date", "external_ref",
        "owner_key", "service_id", "partner_id", "offer_id",
    ]
    values = {
        "type": "lead",
        "stage": stage,
        "active": 1,
        "priority": fields.get("priority") if fields.get("priority") is not None else 0,
    }
    for key in columns:
        if key in fields and key not in values:
            values[key] = fields.get(key)
    values["owner_key"] = clean_owner_key(raw.get("owner") or raw.get("owner_key") or "")
    col_sql = ", ".join(values)
    placeholders = ", ".join("?" * len(values))
    with get_db() as db:
        db.execute(
            f"INSERT INTO deals ({col_sql}) VALUES ({placeholders})",
            list(values.values()),
        )
        db.commit()
        deal_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    tag_error = _apply_tags(deal_id, fields)
    if tag_error:
        return _fail("invalid")
    if log_create:
        log_activity(None, "system", "Lead created", deal_id=deal_id)
    if _partner_count() != before_partners:
        return _fail("invalid")
    lead = get_deal(deal_id)
    dupes = find_duplicates(exclude_id=deal_id, **lead)["matches"]
    return {"ok": True, "lead": lead, "possible_duplicates": dupes}


def _require_open_lead(lead_id):
    deal = get_deal(lead_id)
    if not deal:
        return None, "not_found"
    if deal.get("type") != "lead":
        return None, "not_a_lead"
    if deal.get("active") == 0:
        return None, "lead_lost"
    return deal, None


def update_lead(lead_id, *, fill_empty_only=True, **raw):
    deal, error = _require_open_lead(lead_id)
    if error:
        return _fail(error)
    fields, error = _clean_lead_fields(raw, partial=True)
    if error:
        return _fail(error)
    if raw.get("service_slug"):
        service = get_service_by_slug(str(raw["service_slug"]).strip())
        if not service:
            return _fail("invalid_service")
        fields["service_id"] = service["id"]
    if "partner_id" in fields and fields["partner_id"] and not get_partner(fields["partner_id"]):
        return _fail("partner_not_found")
    if "owner" in raw or "owner_key" in raw:
        fields["owner_key"] = clean_owner_key(raw.get("owner_key") or raw.get("owner") or "")
    updates = {}
    for key, value in fields.items():
        if key in ("tags", "add_tags", "remove_tags"):
            continue
        if fill_empty_only and not _empty(deal.get(key)):
            continue
        updates[key] = value
    if updates:
        updates["updated_at"] = _now()
        set_clause = ", ".join(f"{key} = ?" for key in updates)
        with get_db() as db:
            db.execute(
                f"UPDATE deals SET {set_clause} WHERE id = ?",
                (*updates.values(), lead_id),
            )
            db.commit()
    tag_error = _apply_tags(lead_id, fields)
    if tag_error:
        return _fail("invalid")
    return {"ok": True, "lead": get_deal(lead_id)}


def _activities_for_lead(deal_id, limit=20):
    with get_db() as db:
        rows = db.execute(
            """SELECT * FROM activities WHERE deal_id = ?
               ORDER BY CASE WHEN type = 'research' THEN 0 ELSE 1 END,
                        occurred_at DESC, id DESC
               LIMIT ?""",
            (deal_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def get_lead(lead_id):
    deal = get_deal(lead_id)
    if not deal:
        return _fail("not_found")
    if deal.get("type") != "lead":
        return _fail("not_a_lead")
    note_count, last_at = _research_stats(lead_id)
    duplicates = find_duplicates(exclude_id=lead_id, **deal)["matches"]
    return {
        "ok": True,
        "lead": deal,
        "tags": tags_for_deal(lead_id),
        "activities": _activities_for_lead(lead_id),
        "last_researched_at": last_at,
        "research_note_count": note_count,
        "fit": _fit_for(deal),
        "possible_duplicates": duplicates,
        "conversion_readiness": conversion_readiness(deal, duplicates),
        "partner": get_partner(deal.get("partner_id")) if deal.get("partner_id") else None,
    }


def list_leads(*, query=None, owner=None, service_slug=None, tags=None, source_prefix=None,
               min_fit=None, due_only=False, include_lost=False, sort="updated", limit=50):
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 50))
    rows = []
    from app.services.deals import list_deals
    deals = list_deals(
        type="lead",
        include_lost=bool(include_lost),
        owner_key=owner,
        service_slug=service_slug,
        tags=tags,
    )
    needle = (query or "").strip().lower()
    prefix = (source_prefix or "").strip().lower()
    today = datetime.utcnow().date().isoformat()
    for deal in deals:
        if needle:
            blob = " ".join(str(deal.get(key) or "") for key in (
                "name", "company_name", "contact_name", "email", "phone", "website",
            )).lower()
            if needle not in blob:
                continue
        if prefix and not str(deal.get("source") or "").lower().startswith(prefix):
            continue
        if due_only:
            action = (deal.get("next_action_date") or "")[:10]
            if not action or action > today:
                continue
        note_count, last_at = _research_stats(deal["id"])
        fit = _fit_for(deal)
        if min_fit is not None and fit["score"] < int(min_fit):
            continue
        deal["fit_score"] = fit["score"]
        deal["last_researched_at"] = last_at
        deal["research_note_count"] = note_count
        ready = conversion_readiness(deal)
        deal["conversion_readiness"] = {
            "ready": ready["ready"],
            "missing": ready["missing"],
        }
        rows.append(deal)
    def _sort_key(row):
        if sort == "fit":
            return (-row["fit_score"], row["id"])
        if sort == "created":
            return (row.get("created_at") or "", row["id"])
        if sort == "next_action_date":
            return (row.get("next_action_date") or "9999", row["id"])
        if sort == "last_research":
            return (row.get("last_researched_at") or "", row["id"])
        return (row.get("updated_at") or "", row["id"])
    reverse = sort in ("fit", "created", "updated", "last_research")
    if sort == "next_action_date":
        reverse = False
    rows.sort(key=_sort_key, reverse=reverse)
    return {"ok": True, "leads": rows[:limit]}


def _resolve_reason(lost_reason_id=None, lost_reason=None):
    if lost_reason_id:
        reason = get_lost_reason(int(lost_reason_id))
    else:
        reason = get_lost_reason_by_name(lost_reason or "")
    if not reason or not reason.get("active"):
        return None
    return reason


def mark_lost(deal_id, *, lost_reason_id=None, lost_reason=None, note=None):
    deal = get_deal(deal_id)
    if not deal:
        return _fail("not_found")
    if deal.get("active") == 0:
        return _fail("already_lost")
    reason = _resolve_reason(lost_reason_id, lost_reason)
    if not reason:
        return _fail("invalid_lost_reason")
    now = _now()
    lost_note = sanitize_text(note, max_len=2000, allow_newlines=True) or None
    with get_db() as db:
        db.execute(
            """UPDATE deals
               SET active = 0, lost_reason_id = ?, lost_note = ?, closed_at = ?, updated_at = ?
               WHERE id = ?""",
            (reason["id"], lost_note, now, now, deal_id),
        )
        db.commit()
    log_activity(deal.get("partner_id"), "system", f"Marked lost: {reason['name']}", deal_id=deal_id)
    record = get_deal(deal_id)
    record["lost_reason"] = reason["name"]
    nurture = _nurture_after_loss(record, reason) if reason.get("triggers_nurture") else None
    return {"ok": True, "record": record, "nurture": nurture}


def _nurture_after_loss(deal, reason):
    from app.services.nurture import FAILED, enroll_partner_in_nurture

    service = get_service(deal.get("service_id")) if deal.get("service_id") else None
    if not service or not (service.get("nurture_list_slug") or "").strip():
        log_activity(
            deal.get("partner_id"), "system",
            "Nurture hand-off failed: no nurture list slug on the service",
            deal_id=deal["id"],
        )
        return {"status": FAILED, "message": "no nurture list slug"}
    partner = get_partner(deal.get("partner_id")) if deal.get("partner_id") else None
    if not partner:
        partner = {
            "id": None,
            "name": deal.get("contact_name") or deal.get("company_name") or deal.get("name") or "",
            "email": deal.get("email"),
        }
    status, message = enroll_partner_in_nurture(
        partner, service, deal_id=deal["id"],
        record_type=deal.get("type"), lost_reason=reason["name"],
    )
    if status != "skipped":
        log_activity(
            deal.get("partner_id"), "system",
            f"Nurture enrollment {'succeeded' if status == 'sent' else 'failed'}: {message}",
            deal_id=deal["id"],
        )
    return {"status": status, "message": message}


def restore_deal(deal_id):
    deal = get_deal(deal_id)
    if not deal:
        return _fail("not_found")
    if deal.get("merged_into_id"):
        return _fail("merged")
    if deal.get("active") != 0:
        return _fail("not_lost")
    stage = deal.get("stage")
    stage_meta = pipeline_stages.get_stage(stage) if stage else None
    new_stage = stage
    if deal.get("type") == "opportunity" and stage_meta and stage_meta.get("is_lost"):
        new_stage = pipeline_stages.default_stage_key() or stage
    with get_db() as db:
        db.execute(
            """UPDATE deals
               SET active = 1, lost_reason_id = NULL, lost_note = NULL,
                   closed_at = NULL, stage = ?, updated_at = ?
               WHERE id = ?""",
            (new_stage, _now(), deal_id),
        )
        db.commit()
    log_activity(deal.get("partner_id"), "system", "Restored", deal_id=deal_id)
    return {"ok": True, "record": get_deal(deal_id)}


def _fill_empty_row(db, target, source):
    updates = {}
    for key in _MERGE_FIELDS:
        if _empty(target.get(key)) and not _empty(source.get(key)):
            updates[key] = source[key]
            target[key] = source[key]
    if not updates:
        return
    updates["updated_at"] = _now()
    set_clause = ", ".join(f"{key} = ?" for key in updates)
    db.execute(
        f"UPDATE deals SET {set_clause} WHERE id = ?",
        (*updates.values(), target["id"]),
    )


def merge_leads(target_id, source_ids):
    if not source_ids or len(source_ids) > 10:
        return _fail("invalid_merge")
    target = get_deal(target_id)
    if not target:
        return _fail("not_found")
    if target.get("active") == 0 or target.get("merged_into_id"):
        return _fail("invalid_merge")
    if target.get("type") not in ("lead", "opportunity"):
        return _fail("invalid_merge")
    sources = []
    for source_id in source_ids:
        if int(source_id) == int(target_id):
            return _fail("invalid_merge")
        source = get_deal(source_id)
        if not source:
            return _fail("not_found")
        if source.get("type") != "lead" or source.get("active") == 0 or source.get("merged_into_id"):
            return _fail("invalid_merge")
        sources.append(source)
    reason = get_lost_reason_by_name("Duplicate")
    if not reason:
        return _fail("invalid_lost_reason")
    sources.sort(key=lambda row: (row.get("created_at") or "", row["id"]))
    now = _now()
    with get_db() as db:
        for source in sources:
            _fill_empty_row(db, target, source)
            if target.get("partner_id"):
                db.execute(
                    """UPDATE activities SET deal_id = ?, partner_id = COALESCE(partner_id, ?)
                       WHERE deal_id = ?""",
                    (target["id"], target["partner_id"], source["id"]),
                )
                db.execute(
                    """UPDATE delegated_tasks SET deal_id = ?, partner_id = COALESCE(partner_id, ?)
                       WHERE deal_id = ?""",
                    (target["id"], target["partner_id"], source["id"]),
                )
            else:
                db.execute("UPDATE activities SET deal_id = ? WHERE deal_id = ?", (target["id"], source["id"]))
                db.execute(
                    "UPDATE delegated_tasks SET deal_id = ? WHERE deal_id = ?",
                    (target["id"], source["id"]),
                )
            db.execute(
                """INSERT OR IGNORE INTO deal_tags (deal_id, tag)
                   SELECT ?, tag FROM deal_tags WHERE deal_id = ?""",
                (target["id"], source["id"]),
            )
            db.execute("DELETE FROM deal_tags WHERE deal_id = ?", (source["id"],))
            db.execute(
                """UPDATE deals
                   SET active = 0, lost_reason_id = ?, merged_into_id = ?,
                       closed_at = ?, updated_at = ?
                   WHERE id = ?""",
                (reason["id"], target["id"], now, now, source["id"]),
            )
        db.commit()
    ids = ", ".join(str(source["id"]) for source in sources)
    log_activity(
        target.get("partner_id"), "system", f"Merged deal records {ids}", deal_id=target["id"],
    )
    return {"ok": True, "target": get_deal(target["id"]), "merged_ids": [source["id"] for source in sources]}


def _copy_contact_fill_empty(db, partner_id, lead):
    partner = get_partner(partner_id)
    if not partner:
        return
    mapping = {
        "email": "email", "phone": "phone", "website": "website", "title": "title",
        "address": "address", "industry": "industry", "team_size": "team_size",
        "preferred_channel": "preferred_channel", "linkedin_url": "linkedin_url",
        "x_url": "x_url", "instagram_url": "instagram_url",
        "facebook_url": "facebook_url", "youtube_url": "youtube_url",
    }
    updates = {}
    for lead_key, partner_key in mapping.items():
        if _empty(partner.get(partner_key)) and not _empty(lead.get(lead_key)):
            updates[partner_key] = lead[lead_key]
    if updates:
        update_partner(partner_id, **updates)


def convert_lead(lead_id, *, partner_action="auto", partner_id=None, service_slug=None,
                 stage=None, value_estimate=None, expected_close=None, probability=None,
                 offer_id=None, next_action=None, next_action_date=None, owner=None,
                 owner_key=None, note=None):
    lead = get_deal(lead_id)
    if not lead:
        return _fail("not_found")
    if lead.get("type") != "lead":
        return _fail("not_a_lead")
    if lead.get("active") == 0:
        return _fail("lead_lost")
    service = None
    if service_slug:
        service = get_service_by_slug(str(service_slug).strip())
        if not service:
            return _fail("invalid_service")
    elif lead.get("service_id"):
        service = get_service(lead["service_id"])
    if not service:
        return _fail("missing_service")
    action = (partner_action or "auto").strip().lower()
    if action not in ("auto", "link", "create"):
        return _fail("invalid")
    created = False
    if action == "link":
        partner = get_partner(partner_id)
        if not partner:
            return _fail("partner_not_found")
        resolved_id = partner["id"]
    elif action == "create":
        resolved_id, created = _create_partners_for_lead(lead)
        if not resolved_id:
            return _fail("invalid")
    else:
        name = lead.get("contact_name") or lead.get("company_name") or lead.get("name") or ""
        is_company = bool(lead.get("company_name")) and not lead.get("contact_name")
        candidates = partner_match_candidates(
            email=lead.get("email"), name=name, phone=lead.get("phone"),
            website=lead.get("website"), is_company=is_company, parent_id=None,
        )
        if len(candidates) > 1:
            return _fail(
                "partner_ambiguous",
                candidates=[{"id": row["id"], "name": row["name"], "email": row.get("email")} for row in candidates],
            )
        if len(candidates) == 1:
            resolved_id = candidates[0]["id"]
        else:
            resolved_id, created = _create_partners_for_lead(lead)
            if not resolved_id:
                return _fail("invalid")
    existing = get_open_deal_for_partner_service(resolved_id, service["id"])
    if existing and existing["id"] != lead["id"]:
        return _fail("duplicate_open_opportunity", deal_id=existing["id"])
    if stage and pipeline_stages.get_stage(stage):
        stage_key = stage
    else:
        stage_key = pipeline_stages.default_stage_key() or "new"
    _copy_contact_fill_empty(None, resolved_id, lead)
    now = _now()
    updates = {
        "partner_id": resolved_id,
        "service_id": service["id"],
        "type": "opportunity",
        "date_conversion": now,
        "stage": stage_key,
        "updated_at": now,
    }
    if value_estimate is not None:
        updates["value_estimate"] = value_estimate
    if expected_close:
        updates["expected_close"] = expected_close
    if probability is not None:
        updates["probability"] = probability
    if offer_id and get_offer(offer_id):
        updates["offer_id"] = offer_id
    if next_action is not None:
        updates["next_action"] = sanitize_text(next_action, max_len=500) or None
    if next_action_date is not None:
        updates["next_action_date"] = next_action_date or None
    if owner or owner_key:
        updates["owner_key"] = clean_owner_key(owner_key or owner)
    set_clause = ", ".join(f"{key} = ?" for key in updates)
    with get_db() as db:
        db.execute(
            f"UPDATE deals SET {set_clause} WHERE id = ?",
            (*updates.values(), lead_id),
        )
        db.execute(
            "UPDATE activities SET partner_id = ? WHERE deal_id = ? AND partner_id IS NULL",
            (resolved_id, lead_id),
        )
        db.execute(
            "UPDATE delegated_tasks SET partner_id = ? WHERE deal_id = ? AND partner_id IS NULL",
            (resolved_id, lead_id),
        )
        db.commit()
    fit = _fit_for(get_deal(lead_id))
    matched = ", ".join(item.get("label") or item["field"] for item in fit["matched"]) or "none"
    body = (
        f"Converted to an opportunity. Fit {fit['score']}/{fit['max']}. "
        f"Matched: {matched}. Partner action: {action}."
    )
    if note:
        body += " " + sanitize_text(note, max_len=1000, allow_newlines=True)
    activity_id = log_activity(resolved_id, "system", body, deal_id=lead_id)
    return {
        "ok": True,
        "opportunity": get_deal(lead_id),
        "partner": get_partner(resolved_id),
        "partner_created": created,
        "activity_id": activity_id,
    }


def _create_partners_for_lead(lead):
    """Create the company and/or person a conversion needs. Returns (id, created)."""
    company_name = (lead.get("company_name") or "").strip()
    contact_name = (lead.get("contact_name") or "").strip()
    created = False
    company_id = None
    if company_name:
        existing = get_company_by_name(company_name)
        if existing:
            company_id = existing["id"]
        else:
            company_id = create_partner(
                company_name, is_company=True, email=lead.get("email") or "",
                phone=lead.get("phone") or "", website=lead.get("website") or "",
                industry=lead.get("industry") or "", team_size=lead.get("team_size"),
            )
            created = True
    if contact_name:
        person_id = create_partner(
            contact_name, is_company=False, parent_id=company_id,
            email=lead.get("email") or "", phone=lead.get("phone") or "",
            website=lead.get("website") or "", title=lead.get("title") or "",
        )
        return person_id, True
    if company_id:
        return company_id, created
    fallback = (lead.get("name") or lead.get("email") or lead.get("website") or "").strip()
    if not fallback:
        return None, False
    person_id = create_partner(
        fallback, is_company=False, email=lead.get("email") or "",
        phone=lead.get("phone") or "", website=lead.get("website") or "",
    )
    return person_id, True
