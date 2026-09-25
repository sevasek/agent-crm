"""Lead ingestion — the automation-facing write path (docs/DATA_MODEL.md → Lead ingest).

One entry point, ingest_lead(), used by the /api/v1/leads endpoint and MCP.
Each call is one lead: find-or-create the partner (email first, else
phone+name, else website+name, else name scoped to parent company).
Company-only and phone-only rows are valid (scraped niche lists often have
no email). If name equals company_name, the deal lands on the company
partner rather than a duplicate person. Fill any still-empty partner (and
deal) fields on a match, then create a deal unless the partner already has
a non-closed deal for the same service.
"""
import math

from app.services.catalog import get_service_by_slug
from app.services.deal_tags import add_deal_tags, tags_from_lead
from app.services.deals import create_deal, get_open_deal_for_partner_service, update_deal_fields
from app.services.offers import get_offer
from app.services.partners import (
    create_partner,
    get_company_by_name,
    get_partner_by_email,
    get_partner_by_phone_and_name,
    get_partner_by_website_and_name,
    get_person_by_name,
    update_partner,
)

# Partner columns an existing-person re-inject may backfill. Name/email are
# omitted on purpose — those are identity, not profile extras, and must not
# clobber an operator edit.
_FILLABLE_FIELDS = (
    "phone", "title", "website", "address", "social_url",
    "linkedin_url", "x_url", "instagram_url", "facebook_url", "youtube_url",
    "preferred_channel", "industry", "team_size", "owner_key",
)

_FILLABLE_DEAL_FIELDS = (
    "source", "value_estimate", "pain_points", "goals",
    "next_action", "next_action_date", "offer_id", "owner_key", "external_ref",
)


def _text(value) -> str:
    """JSON scalars to stripped text; nested objects/bools/None → empty. Never raises."""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value).strip()
    return ""


def _is_empty(value):
    return value is None or value == ""


def _profile_from_lead(lead: dict) -> dict:
    """Coerce fillable fields once so create and fill-empty share the same values."""
    return {
        "phone": _text(lead.get("phone")),
        "title": _text(lead.get("title")),
        "website": _text(lead.get("website")),
        "address": _text(lead.get("address")),
        "social_url": _text(lead.get("social_url")),
        "linkedin_url": _text(lead.get("linkedin_url")),
        "x_url": _text(lead.get("x_url")),
        "instagram_url": _text(lead.get("instagram_url")),
        "facebook_url": _text(lead.get("facebook_url")),
        "youtube_url": _text(lead.get("youtube_url")),
        "preferred_channel": _text(lead.get("preferred_channel")),
        "industry": _text(lead.get("industry")),
        "team_size": lead.get("team_size"),
        "owner_key": _text(lead.get("owner_key")),
    }


def _fill_empty_partner_fields(partner: dict, profile: dict, parent_id) -> None:
    """Copy incoming values onto the partner only where the CRM field is blank.

    Never overwrites a non-empty value — an operator who edited title/phone/etc.
    in the admin UI keeps that edit even if an ETL job re-sends a different one.
    """
    updates = {}
    for field in _FILLABLE_FIELDS:
        incoming = profile.get(field)
        if field == "team_size" and isinstance(incoming, str):
            incoming = incoming.strip()
        if _is_empty(incoming):
            continue
        if _is_empty(partner.get(field)):
            updates[field] = incoming
    if parent_id and _is_empty(partner.get("parent_id")):
        updates["parent_id"] = parent_id
    if updates:
        update_partner(partner["id"], **updates)

# Untrusted JSON from an automation/ETL job. Only these keys are copied through; everything else is dropped.
ALLOWED_LEAD_KEYS = (
    "name", "email", "company_name", "service_slug", "phone", "title",
    "website", "address", "social_url", "linkedin_url", "x_url", "instagram_url",
    "facebook_url", "youtube_url", "preferred_channel", "industry",
    "team_size", "source", "value_estimate", "pain_points", "goals",
    "next_action", "next_action_date", "is_company",
    "owner_key", "offer_id", "external_ref", "tags",
)

_TRUTHY_STRINGS = {"true", "1", "yes", "y"}


def _as_bool(value) -> bool:
    """JSON scalars to a bool; anything else (None, dict, garbage) is False.

    is_company describes the lead itself (e.g. a company-only lead with no
    named contact), not the separate company_name parent record.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in _TRUTHY_STRINGS
    return False


def _as_text(value) -> str:
    """JSON scalars become strings; nested objects/arrays are treated as missing.

    Downstream helpers call sanitize_text / .strip() and would raise or stringify
    a dict via its keys if we passed a nested object through.
    """
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    return ""


# sqlite3 INTEGER (and huge-int REAL binds) are signed 64-bit.
_SQLITE_INT_MIN = -(2**63)
_SQLITE_INT_MAX = 2**63 - 1


def _in_sqlite_int_range(n: int) -> bool:
    return _SQLITE_INT_MIN <= n <= _SQLITE_INT_MAX


def _as_number(value, integer=False):
    """int/float or numeric string; anything else (dict, bool, garbage) → None.

    Never raises. Non-finite floats (NaN/Inf, including JSON Infinity and
    float("1e400")) and ints outside SQLite's signed 64-bit range are None.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if _in_sqlite_int_range(value) else None
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        if not integer:
            return value
        try:
            converted = int(value)
        except (OverflowError, ValueError):
            return None
        return converted if _in_sqlite_int_range(converted) else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            if integer:
                converted = int(text, 10)
                return converted if _in_sqlite_int_range(converted) else None
            number = float(text)
            if not math.isfinite(number):
                return None
            if number.is_integer():
                converted = int(number)
                if not _in_sqlite_int_range(converted):
                    return None
                return converted
            return number
        except (ValueError, OverflowError):
            return None
    return None


def sanitize_lead_payload(lead) -> dict:
    """Allowlist + coerce an untrusted lead object. Unknown keys dropped. Never raises."""
    if not isinstance(lead, dict):
        lead = {}
    cleaned = {}
    for key in ALLOWED_LEAD_KEYS:
        raw = lead.get(key)
        if key == "value_estimate":
            cleaned[key] = _as_number(raw)
        elif key in ("team_size", "offer_id"):
            cleaned[key] = _as_number(raw, integer=True)
        elif key == "is_company":
            cleaned[key] = _as_bool(raw)
        elif key == "tags":
            # None when omitted or not a list: a re-run leaves existing tags alone.
            # A list is merged; invalid entries are dropped, never a traceback.
            cleaned[key] = tags_from_lead(raw) if "tags" in lead else None
        else:
            cleaned[key] = _as_text(raw)
    return cleaned


def _find_or_create_company(company_name: str):
    company = get_company_by_name(company_name)
    if company:
        return company["id"]
    return create_partner(company_name, is_company=True)


def _match_existing_partner(*, email, name, phone, website, is_company, parent_id):
    """Identity waterfall: email, then phone+name, then website+name, then name+parent."""
    if email:
        partner = get_partner_by_email(email)
        if partner:
            return partner
    if phone:
        partner = get_partner_by_phone_and_name(name, phone, is_company=is_company)
        if partner:
            return partner
    if website:
        partner = get_partner_by_website_and_name(name, website, is_company=is_company)
        if partner:
            return partner
    return get_person_by_name(name, parent_id=parent_id, is_company=is_company)


def _deal_payload(lead: dict) -> dict:
    offer_id = lead.get("offer_id")
    if offer_id and not get_offer(offer_id):
        offer_id = None
    return {
        "source": lead.get("source") or "",
        "value_estimate": lead.get("value_estimate"),
        "pain_points": lead.get("pain_points") or "",
        "goals": lead.get("goals") or "",
        "next_action": lead.get("next_action") or "",
        "next_action_date": lead.get("next_action_date") or "",
        "offer_id": offer_id,
        "owner_key": lead.get("owner_key") or "",
        "external_ref": lead.get("external_ref") or "",
    }


def _fill_empty_deal_fields(deal: dict, incoming: dict) -> None:
    updates = {}
    for field in _FILLABLE_DEAL_FIELDS:
        value = incoming.get(field)
        if _is_empty(value):
            continue
        if _is_empty(deal.get(field)):
            updates[field] = value
    if updates:
        update_deal_fields(deal["id"], **updates)


def ingest_lead(lead: dict) -> dict:
    """Returns {"email": ..., "status": ...} — status is one of:
    created / existing_partner_new_deal / duplicate_open_deal / invalid / invalid_service.
    Successful statuses also include partner_id and deal_id.
    Never raises on bad input — a malformed item in a batch should not sink the batch.
    """
    lead = sanitize_lead_payload(lead)
    email = (lead.get("email") or "").strip()
    company_name = _text(lead.get("company_name"))
    name = (lead.get("name") or "").strip() or company_name
    service_slug = (lead.get("service_slug") or "").strip()
    is_company = bool(lead.get("is_company"))
    profile = _profile_from_lead(lead)
    phone = profile["phone"]
    website = profile["website"]

    if not name:
        return {"email": email, "status": "invalid"}

    service = get_service_by_slug(service_slug) if service_slug else None
    if not service:
        return {"email": email, "status": "invalid_service"}

    # Name is not enough on its own: without a contact handle or a company
    # identity, name-only ingest always created duplicate people.
    if not email and not phone and not website and not company_name:
        return {"email": email, "status": "invalid"}

    # Scraped company rows often send company_name (+ phone) with no person.
    # Landing the deal on the company avoids a duplicate person of the same name.
    same_as_company = bool(company_name) and name.lower() == company_name.lower()
    if same_as_company:
        is_company = True
        company_name_for_parent = ""
    else:
        company_name_for_parent = company_name

    partner = _match_existing_partner(
        email=email, name=name, phone=phone, website=website,
        is_company=is_company, parent_id=None,
    )

    parent_id = None
    if company_name_for_parent and (partner is None or _is_empty(partner.get("parent_id"))):
        parent_id = _find_or_create_company(company_name_for_parent)
    elif partner and partner.get("parent_id"):
        parent_id = partner["parent_id"]

    if partner is None:
        partner = get_person_by_name(name, parent_id=parent_id, is_company=is_company)

    partner_is_new = partner is None
    if partner_is_new:
        partner_id = create_partner(
            name, is_company=is_company, parent_id=parent_id, email=email,
            phone=profile["phone"], website=profile["website"],
            title=profile["title"],
            address=profile["address"], social_url=profile["social_url"],
            linkedin_url=profile["linkedin_url"], x_url=profile["x_url"],
            instagram_url=profile["instagram_url"], facebook_url=profile["facebook_url"],
            youtube_url=profile["youtube_url"],
            preferred_channel=profile["preferred_channel"],
            industry=profile["industry"], team_size=profile["team_size"],
            owner_key=profile.get("owner_key") or "",
        )
    else:
        partner_id = partner["id"]
        _fill_empty_partner_fields(partner, profile, parent_id)

    deal_fields = _deal_payload(lead)
    if not partner_is_new:
        existing_deal = get_open_deal_for_partner_service(partner_id, service["id"])
        if existing_deal:
            _fill_empty_deal_fields(existing_deal, deal_fields)
            # tags omitted (None) leaves the set alone. A list merges.
            if lead.get("tags"):
                add_deal_tags(existing_deal["id"], lead["tags"])
            return {
                "email": email,
                "status": "duplicate_open_deal",
                "partner_id": partner_id,
                "deal_id": existing_deal["id"],
            }

    deal_id = create_deal(
        partner_id, service["id"],
        source=deal_fields["source"],
        value_estimate=deal_fields["value_estimate"],
        pain_points=deal_fields["pain_points"],
        goals=deal_fields["goals"],
        next_action=deal_fields["next_action"],
        next_action_date=deal_fields["next_action_date"],
        created_note="Deal created (lead ingest)",
        offer_id=deal_fields["offer_id"],
        owner_key=deal_fields["owner_key"],
        external_ref=deal_fields["external_ref"],
        tags=lead.get("tags") or None,
    )
    return {
        "email": email,
        "status": "created" if partner_is_new else "existing_partner_new_deal",
        "partner_id": partner_id,
        "deal_id": deal_id,
    }
