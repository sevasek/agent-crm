"""MCP tools — the bot's actual work surface.

Each tool returns a dict with `ok` so a bad argument is a recoverable
result, not a protocol error. Destructive pipeline/user/catalog deletes
are not exposed; those stay in the admin UI / stages API key.
"""

from app.mcp.serialize import (
    activity_brief,
    deal_brief,
    delegated_task_brief,
    icp_brief,
    offer_brief,
    partner_brief,
    service_brief,
    stage_brief,
)
from app.mcp.util import (
    DEFAULT_LIMIT,
    MAX_ACTIVITIES,
    MAX_BULK,
    MAX_INGEST,
    MAX_LIMIT,
    as_bool,
    as_int,
    as_number,
    as_text,
    clamp_limit,
    paginate,
)
from app.services import icp as icp_service
from app.services import pipeline_stages
from app.services.activities import VALID_TYPES, list_activities_for_deal, list_activities_for_partner, log_activity
from app.services.call_queue import CALL_QUEUE_LIMIT, list_todays_calls
from app.services.auth import clean_owner_key, is_valid_slug
from app.services.catalog import (
    get_or_create_service,
    get_service,
    get_service_by_slug,
    list_services,
    slug_from_name,
    update_catalog_service,
)
from app.services.deals import (
    CALL_OUTCOMES,
    create_deal,
    get_deal,
    get_open_deal_for_partner_service,
    list_deals,
    record_call_outcome,
    set_deal_stage,
    update_deal_fields,
    validate_parent_link,
)
from app.services.delegated_tasks import (
    default_owner,
    TASK_STATUSES,
    clean_task_status,
    complete_task,
    create_task,
    get_task,
    list_tasks,
    update_task,
)
from app.services.deals import child_deal_summary
from app.services.leads import ingest_lead, sanitize_lead_payload
from app.services.offers import create_offer, find_offer, get_offer, list_offers, update_offer
from app.services.partners import (
    SOCIAL_FIELDS,
    create_partner,
    get_children,
    get_partner,
    get_partner_by_email,
    list_partners,
    update_partner,
)
from app.services.staleness import list_due_deals

_BOT_ACTIVITY_TYPES = tuple(sorted(VALID_TYPES - {"system"}))
_CALL_OUTCOME_KEYS = tuple(key for key, _label, _target in CALL_OUTCOMES)

_PARTNER_WRITE_FIELDS = (
    "name", "email", "phone", "website", "title", "address",
    "preferred_channel", "industry", "team_size", "social_url",
    "owner_key",
    *SOCIAL_FIELDS,
)

_DEAL_WRITE_FIELDS = (
    "source", "value_estimate", "pain_points", "goals",
    "next_action", "next_action_date", "offer_id", "owner_key", "external_ref",
    "parent_deal_id",
)


def _parent_error(error):
    messages = {
        "invalid_parent": "parent_deal_id must be a positive integer or null",
        "parent_not_found": "No deal with that parent_deal_id",
        "parent_cycle": "parent_deal_id would create a cycle",
        "parent_partner_mismatch": "Child deals must share partner_id with the parent",
    }
    return _err(error, messages.get(error, error))


def _child_deals(deal_id):
    return [child_deal_summary(d) for d in list_deals(parent_deal_id=deal_id)]


def _ok(**payload):
    payload["ok"] = True
    return payload


def _err(error, message, **payload):
    payload["ok"] = False
    payload["error"] = error
    payload["message"] = message
    return payload


def _id_arg(arguments, name):
    value = as_int(arguments.get(name))
    if value is None or value < 1:
        return None, _err("invalid_id", f"{name} must be a positive integer")
    return value, None


def _require_partner(partner_id):
    partner = get_partner(partner_id)
    if not partner:
        return None, _err("not_found", f"No partner with id {partner_id}")
    return partner, None


def _require_deal(deal_id):
    deal = get_deal(deal_id)
    if not deal:
        return None, _err("not_found", f"No deal with id {deal_id}")
    return deal, None


def _resolve_service(arguments):
    service_id = as_int(arguments.get("service_id"))
    slug = as_text(arguments.get("service_slug")).strip().lower()
    if service_id:
        service = get_service(service_id)
        if service:
            return service, None
        return None, _err("not_found", f"No service with id {service_id}")
    if slug:
        service = get_service_by_slug(slug)
        if service:
            return service, None
        return None, _err("invalid_service", f"No service with slug {slug!r}")
    return None, _err("service_required", "Pass service_id or service_slug")


def _partner_updates(arguments):
    updates = {}
    if "is_company" in arguments:
        flag = as_bool(arguments.get("is_company"))
        if flag is not None:
            updates["is_company"] = flag
    if "parent_id" in arguments:
        parent_id = arguments.get("parent_id")
        if parent_id in (None, "", 0):
            updates["parent_id"] = None
        else:
            parsed = as_int(parent_id)
            if parsed is None:
                return None, _err("invalid_id", "parent_id must be a positive integer or null")
            updates["parent_id"] = parsed
    for field in _PARTNER_WRITE_FIELDS:
        if field in arguments:
            updates[field] = arguments.get(field)
    return updates, None


def _owner_arg(arguments):
    raw = arguments.get("owner_key")
    if raw in (None, ""):
        raw = arguments.get("owner")
    return clean_owner_key(as_text(raw) if raw not in (None, "") else raw)


def _deal_updates(arguments):
    updates = {}
    for field in _DEAL_WRITE_FIELDS:
        if field not in arguments:
            continue
        if field == "value_estimate":
            raw = arguments.get(field)
            if raw in (None, ""):
                updates[field] = None
            else:
                parsed = as_number(raw)
                if parsed is not None:
                    updates[field] = parsed
        elif field in ("offer_id", "parent_deal_id"):
            raw = arguments.get(field)
            if raw in (None, "", 0, "0"):
                updates[field] = None
            else:
                parsed = as_int(raw)
                if parsed is not None:
                    updates[field] = parsed
        elif field == "owner_key":
            updates[field] = clean_owner_key(as_text(arguments.get(field)))
        else:
            updates[field] = arguments.get(field)
    if "owner" in arguments and "owner_key" not in updates:
        updates["owner_key"] = _owner_arg(arguments)
    return updates


def _fit_for_deal(deal, partner):
    criteria = icp_service.list_criteria(active_only=True)
    if not criteria:
        return None
    return icp_brief(icp_service.score_fit(icp_service.build_matchable_row(partner, deal), criteria))


def search_partners(arguments):
    query = as_text(arguments.get("query")).strip()
    limit = clamp_limit(arguments.get("limit"))
    rows, truncated = paginate(list_partners(search=query), limit)
    partners = []
    for partner in rows:
        brief = partner_brief(partner)
        if partner.get("parent_id"):
            brief["company"] = partner_brief(get_partner(partner["parent_id"]))
        partners.append(brief)
    return _ok(
        partners=partners,
        count=len(partners),
        truncated=truncated,
        query=query or None,
    )


def get_partner_tool(arguments):
    partner_id, error = _id_arg(arguments, "partner_id")
    if error:
        return error
    partner, error = _require_partner(partner_id)
    if error:
        return error
    parent = get_partner(partner["parent_id"]) if partner.get("parent_id") else None
    deals = [deal_brief(d) for d in list_deals(partner_id=partner_id)]
    activity_limit = clamp_limit(arguments.get("activity_limit"), default=10, maximum=MAX_ACTIVITIES)
    activities, truncated = paginate(list_activities_for_partner(partner_id), activity_limit)
    return _ok(
        partner=partner_brief(partner),
        company=partner_brief(parent),
        people=[partner_brief(p) for p in get_children(partner_id)],
        deals=deals,
        activities=[activity_brief(a) for a in activities],
        activities_truncated=truncated,
    )


def list_deals_tool(arguments):
    stage = as_text(arguments.get("stage")).strip() or None
    if stage and not pipeline_stages.get_stage(stage):
        return _err("invalid_stage", f"Unknown stage {stage!r}. Call list_catalog.")
    partner_id = as_int(arguments.get("partner_id"))
    if arguments.get("partner_id") not in (None, "") and (partner_id is None or partner_id < 1):
        return _err("invalid_id", "partner_id must be a positive integer")
    owner_key = _owner_arg(arguments)
    service_slug = as_text(arguments.get("service_slug")).strip().lower() or None
    parent_deal_id = as_int(arguments.get("parent_deal_id"))
    if arguments.get("parent_deal_id") not in (None, "") and (parent_deal_id is None or parent_deal_id < 1):
        return _err("invalid_id", "parent_deal_id must be a positive integer")
    due_only = as_bool(arguments.get("due_only"), default=False)
    limit = clamp_limit(arguments.get("limit"))
    if due_only:
        rows = list_due_deals(stage=stage, owner_key=owner_key, service_slug=service_slug)
        if partner_id:
            rows = [d for d in rows if d["partner_id"] == partner_id]
        if parent_deal_id:
            rows = [d for d in rows if d.get("parent_deal_id") == parent_deal_id]
    else:
        rows = list_deals(
            stage=stage, partner_id=partner_id, owner_key=owner_key, service_slug=service_slug,
            parent_deal_id=parent_deal_id,
        )
    sliced, truncated = paginate(rows, limit)
    return _ok(
        deals=[deal_brief(d) for d in sliced],
        count=len(sliced),
        truncated=truncated,
        stage=stage,
        due_only=bool(due_only),
        owner_key=owner_key,
        service_slug=service_slug,
        parent_deal_id=parent_deal_id,
    )


def get_deal_tool(arguments):
    deal_id, error = _id_arg(arguments, "deal_id")
    if error:
        return error
    deal, error = _require_deal(deal_id)
    if error:
        return error
    partner = get_partner(deal["partner_id"])
    service = get_service(deal["service_id"])
    offer = get_offer(deal.get("offer_id"))
    activity_limit = clamp_limit(arguments.get("activity_limit"), default=10, maximum=MAX_ACTIVITIES)
    activities, truncated = paginate(list_activities_for_deal(deal_id), activity_limit)
    tasks = list_tasks(deal_id=deal_id)
    return _ok(
        deal=deal_brief(deal),
        partner=partner_brief(partner),
        service=service_brief(service),
        offer=offer_brief(offer),
        icp=(_fit_for_deal(deal, partner) if partner else None),
        child_deals=_child_deals(deal_id),
        delegated_tasks=[delegated_task_brief(t) for t in tasks],
        activities=[activity_brief(a) for a in activities],
        activities_truncated=truncated,
    )


def list_activities_tool(arguments):
    partner_id = as_int(arguments.get("partner_id"))
    deal_id = as_int(arguments.get("deal_id"))
    if deal_id:
        if not get_deal(deal_id):
            return _err("not_found", f"No deal with id {deal_id}")
        rows = list_activities_for_deal(deal_id)
    elif partner_id:
        if not get_partner(partner_id):
            return _err("not_found", f"No partner with id {partner_id}")
        rows = list_activities_for_partner(partner_id)
    else:
        return _err("id_required", "Pass partner_id or deal_id")
    limit = clamp_limit(arguments.get("limit"), default=10, maximum=MAX_ACTIVITIES)
    sliced, truncated = paginate(rows, limit)
    return _ok(
        activities=[activity_brief(a) for a in sliced],
        count=len(sliced),
        truncated=truncated,
    )


def get_call_queue_tool(arguments):
    limit = clamp_limit(arguments.get("limit"), default=CALL_QUEUE_LIMIT, maximum=MAX_LIMIT)
    stage = as_text(arguments.get("stage")).strip() or None
    if stage and not pipeline_stages.get_stage(stage):
        return _err("invalid_stage", f"Unknown stage {stage!r}. Call list_catalog.")
    service_slug = as_text(arguments.get("service_slug")).strip().lower() or None
    source_prefix = as_text(arguments.get("source_prefix")).strip() or None
    include_new = as_bool(arguments.get("include_new"), default=False)
    owner_key = _owner_arg(arguments)
    rows = list_todays_calls(
        limit=limit,
        stage=stage,
        service_slug=service_slug,
        source_prefix=source_prefix,
        include_new=bool(include_new),
        owner_key=owner_key,
    )
    calls = []
    for row in rows:
        calls.append({
            "rank": row.get("rank"),
            "score": row.get("score"),
            "reasons": row.get("reasons"),
            "deal": deal_brief(row),
            "partner_name": row.get("partner_name"),
            "partner_phone": row.get("partner_phone"),
            "partner_email": row.get("partner_email"),
            "service_name": row.get("service_name"),
            "days_in_qualified": row.get("days_in_qualified"),
        })
    return _ok(
        calls=calls,
        count=len(calls),
        stage=stage,
        service_slug=service_slug,
        include_new=bool(include_new),
        owner_key=owner_key,
    )


def list_due_followups_tool(arguments):
    stage = as_text(arguments.get("stage")).strip() or None
    if stage and not pipeline_stages.get_stage(stage):
        return _err("invalid_stage", f"Unknown stage {stage!r}. Call list_catalog.")
    owner_key = _owner_arg(arguments)
    service_slug = as_text(arguments.get("service_slug")).strip().lower() or None
    limit = clamp_limit(arguments.get("limit"))
    rows, truncated = paginate(
        list_due_deals(stage=stage, owner_key=owner_key, service_slug=service_slug),
        limit,
    )
    return _ok(
        deals=[deal_brief(d) for d in rows],
        count=len(rows),
        truncated=truncated,
        owner_key=owner_key,
    )


def list_catalog_tool(_arguments):
    return _ok(
        services=[service_brief(s) for s in list_services(active_only=True)],
        stages=[stage_brief(s) for s in pipeline_stages.list_stages()],
        offers=[offer_brief(o) for o in list_offers(active_only=True)],
        call_outcomes=[
            {"key": key, "label": label} for key, label, _target in CALL_OUTCOMES
        ],
        activity_types=list(_BOT_ACTIVITY_TYPES),
        delegated_task_default_owner=default_owner(),
        delegated_task_statuses=list(TASK_STATUSES),
    )


def ingest_leads_tool(arguments):
    leads = arguments.get("leads")
    if not isinstance(leads, list) or not leads:
        return _err("empty_leads_list", "leads must be a non-empty array")
    if len(leads) > MAX_INGEST:
        return _err("too_many_leads", f"Max {MAX_INGEST} leads per call")
    results = []
    for raw in leads:
        if not isinstance(raw, dict):
            results.append({"email": "", "status": "invalid"})
            continue
        lead = sanitize_lead_payload(raw)
        results.append(ingest_lead(lead))
    return _ok(results=results)


def create_partner_tool(arguments):
    name = as_text(arguments.get("name")).strip()
    if not name:
        return _err("name_required", "name is required")
    email = as_text(arguments.get("email")).strip()
    if email:
        existing = get_partner_by_email(email)
        if existing:
            return _ok(
                status="existing",
                partner=partner_brief(existing),
                message="A partner with this email already exists. Search first; update_partner to change fields.",
            )
    updates, error = _partner_updates(arguments)
    if error:
        return error
    partner_id = create_partner(
        name,
        is_company=bool(updates.get("is_company")),
        parent_id=updates.get("parent_id"),
        email=updates.get("email") or "",
        phone=updates.get("phone") or "",
        website=updates.get("website") or "",
        title=updates.get("title") or "",
        address=updates.get("address") or "",
        social_url=updates.get("social_url") or "",
        preferred_channel=updates.get("preferred_channel") or "",
        industry=updates.get("industry") or "",
        team_size=updates.get("team_size"),
        linkedin_url=updates.get("linkedin_url") or "",
        x_url=updates.get("x_url") or "",
        instagram_url=updates.get("instagram_url") or "",
        facebook_url=updates.get("facebook_url") or "",
        youtube_url=updates.get("youtube_url") or "",
        owner_key=updates.get("owner_key") or "",
    )
    return _ok(status="created", partner=partner_brief(get_partner(partner_id)))


def update_partner_tool(arguments):
    partner_id, error = _id_arg(arguments, "partner_id")
    if error:
        return error
    partner, error = _require_partner(partner_id)
    if error:
        return error
    updates, error = _partner_updates(arguments)
    if error:
        return error
    updates.pop("partner_id", None)
    if not updates:
        return _err("no_fields", "Pass at least one partner field to change")
    fill_empty_only = as_bool(arguments.get("fill_empty_only"), default=True)
    if fill_empty_only:
        applied = {}
        for field, value in updates.items():
            if field in ("is_company", "parent_id"):
                # Identity flags: only fill when the current value is the default empty.
                if field == "parent_id" and not partner.get("parent_id") and value:
                    applied[field] = value
                continue
            current = partner.get(field)
            if current is None or current == "":
                if value not in (None, ""):
                    applied[field] = value
        updates = applied
        if not updates:
            return _ok(
                status="unchanged",
                partner=partner_brief(partner),
                message="fill_empty_only=true and no empty fields matched. Pass fill_empty_only=false to overwrite.",
            )
    update_partner(partner_id, **updates)
    return _ok(status="updated", partner=partner_brief(get_partner(partner_id)))


def create_deal_tool(arguments):
    partner_id, error = _id_arg(arguments, "partner_id")
    if error:
        return error
    partner, error = _require_partner(partner_id)
    if error:
        return error
    service, error = _resolve_service(arguments)
    if error:
        return error
    existing = get_open_deal_for_partner_service(partner_id, service["id"])
    if existing:
        return _ok(
            status="duplicate_open_deal",
            deal=deal_brief(existing),
            message="This partner already has an open deal for that service. Update that deal instead of creating another.",
        )
    stage = as_text(arguments.get("stage")).strip() or None
    if stage and not pipeline_stages.get_stage(stage):
        return _err("invalid_stage", f"Unknown stage {stage!r}. Call list_catalog.")
    parent_deal_id = None
    if "parent_deal_id" in arguments and arguments.get("parent_deal_id") not in (None, "", 0, "0"):
        parent_deal_id = as_int(arguments.get("parent_deal_id"))
        if parent_deal_id is None or parent_deal_id < 1:
            return _err("invalid_id", "parent_deal_id must be a positive integer")
        cleaned, error = validate_parent_link(None, parent_deal_id, partner_id)
        if error:
            return _parent_error(error)
        parent_deal_id = cleaned
    deal_id = create_deal(
        partner_id,
        service["id"],
        source=as_text(arguments.get("source")),
        value_estimate=as_number(arguments.get("value_estimate")),
        next_action=as_text(arguments.get("next_action")),
        next_action_date=as_text(arguments.get("next_action_date")).strip() or "",
        pain_points=as_text(arguments.get("pain_points")),
        goals=as_text(arguments.get("goals")),
        created_note="Deal created (MCP)",
        stage=stage,
        offer_id=as_int(arguments.get("offer_id")),
        owner_key=_owner_arg(arguments) or as_text(arguments.get("owner_key")),
        external_ref=as_text(arguments.get("external_ref")),
        parent_deal_id=parent_deal_id,
    )
    return _ok(status="created", deal=deal_brief(get_deal(deal_id)), partner=partner_brief(partner))


def update_deal_tool(arguments):
    deal_id, error = _id_arg(arguments, "deal_id")
    if error:
        return error
    deal, error = _require_deal(deal_id)
    if error:
        return error
    updates = _deal_updates(arguments)
    if not updates:
        return _err("no_fields", "Pass at least one deal field to change (not stage — use set_deal_stage)")
    if "parent_deal_id" in updates:
        cleaned, error = validate_parent_link(deal_id, updates["parent_deal_id"], deal["partner_id"])
        if error:
            return _parent_error(error)
        updates["parent_deal_id"] = cleaned
    update_deal_fields(deal_id, **updates)
    return _ok(status="updated", deal=deal_brief(get_deal(deal_id)))


def set_deal_stage_tool(arguments):
    deal_id, error = _id_arg(arguments, "deal_id")
    if error:
        return error
    deal, error = _require_deal(deal_id)
    if error:
        return error
    stage = as_text(arguments.get("stage")).strip()
    if not stage:
        return _err("stage_required", "stage is required (a pipeline key from list_catalog)")
    if not pipeline_stages.get_stage(stage):
        return _err("invalid_stage", f"Unknown stage {stage!r}. Call list_catalog.")
    if not set_deal_stage(deal_id, stage):
        return _err("stage_unchanged", "Could not move the deal to that stage")
    return _ok(
        status="updated",
        deal=deal_brief(get_deal(deal_id)),
        child_deals=_child_deals(deal_id),
    )


def record_call_outcome_tool(arguments):
    deal_id, error = _id_arg(arguments, "deal_id")
    if error:
        return error
    deal, error = _require_deal(deal_id)
    if error:
        return error
    outcome = as_text(arguments.get("outcome")).strip()
    if outcome not in _CALL_OUTCOME_KEYS:
        return _err(
            "invalid_outcome",
            f"outcome must be one of: {', '.join(_CALL_OUTCOME_KEYS)}",
        )
    note = as_text(arguments.get("note"))
    if not record_call_outcome(deal_id, outcome, note=note):
        return _err("outcome_failed", "Could not record that call outcome")
    return _ok(status="recorded", deal=deal_brief(get_deal(deal_id)), outcome=outcome)


def log_activity_tool(arguments):
    partner_id, error = _id_arg(arguments, "partner_id")
    if error:
        return error
    partner, error = _require_partner(partner_id)
    if error:
        return error
    activity_type = as_text(arguments.get("type")).strip().lower() or "note"
    if activity_type not in _BOT_ACTIVITY_TYPES:
        return _err(
            "invalid_type",
            f"type must be one of: {', '.join(_BOT_ACTIVITY_TYPES)} (system is reserved for the app)",
        )
    body = as_text(arguments.get("body")).strip()
    if not body:
        return _err("body_required", "body is required")
    deal_id = as_int(arguments.get("deal_id"))
    if arguments.get("deal_id") not in (None, "") and (deal_id is None or deal_id < 1):
        return _err("invalid_id", "deal_id must be a positive integer")
    if deal_id:
        deal = get_deal(deal_id)
        if not deal:
            return _err("not_found", f"No deal with id {deal_id}")
        if deal["partner_id"] != partner_id:
            return _err("deal_mismatch", "That deal does not belong to this partner")
    activity_id = log_activity(partner_id, activity_type, body, deal_id=deal_id)
    rows = list_activities_for_partner(partner_id)
    match = next((a for a in rows if a["id"] == activity_id), None)
    return _ok(status="logged", activity=activity_brief(match) or {"id": activity_id})


def create_service_tool(arguments):
    name = as_text(arguments.get("name")).strip()
    if not name:
        return _err("name_required", "name is required")
    slug = as_text(arguments.get("slug")).strip().lower() or slug_from_name(name) or ""
    if not is_valid_slug(slug):
        return _err(
            "invalid_slug",
            "slug must be 2–50 lowercase letters, digits, and hyphens (or omit it to derive from name)",
        )
    description = as_text(arguments.get("description"))
    nurture_list_slug = as_text(arguments.get("nurture_list_slug"))
    service, created = get_or_create_service(
        name, slug, description=description, nurture_list_slug=nurture_list_slug,
    )
    return _ok(
        status="created" if created else "existing",
        service=service_brief(service),
    )


def update_service_tool(arguments):
    service_id, error = _id_arg(arguments, "service_id")
    if error:
        return error
    fields = {}
    if "name" in arguments:
        fields["name"] = as_text(arguments.get("name"))
    if "slug" in arguments:
        fields["slug"] = as_text(arguments.get("slug"))
    if "description" in arguments:
        fields["description"] = as_text(arguments.get("description"))
    if "nurture_list_slug" in arguments:
        fields["nurture_list_slug"] = as_text(arguments.get("nurture_list_slug"))
    if "active" in arguments:
        flag = as_bool(arguments.get("active"))
        if flag is not None:
            fields["active"] = flag
    if not fields:
        return _err(
            "no_fields",
            "Pass name, slug, description, active, and/or nurture_list_slug",
        )
    service, error = update_catalog_service(service_id, **fields)
    if error:
        messages = {
            "not_found": f"No service with id {service_id}",
            "name_required": "name is required",
            "invalid_slug": "slug must be 2–50 lowercase letters, digits, and hyphens",
            "slug_conflict": "Another service already uses that slug",
        }
        return _err(error, messages.get(error, error))
    return _ok(status="updated", service=service_brief(service))


def create_offer_tool(arguments):
    name = as_text(arguments.get("name")).strip()
    if not name:
        return _err("name_required", "name is required")
    service = None
    if arguments.get("service_id") not in (None, "") or as_text(arguments.get("service_slug")).strip():
        service, error = _resolve_service(arguments)
        if error:
            return error
    price = arguments.get("price")
    if price in (None, ""):
        price = None
    else:
        price = as_number(price)
    currency = as_text(arguments.get("currency")).strip().upper() or "USD"
    service_id = service["id"] if service else None
    already = find_offer(name, service_id=service_id)
    offer, error = create_offer(
        name,
        pitch=as_text(arguments.get("pitch")),
        proof_point=as_text(arguments.get("proof_point")),
        price_anchor=as_text(arguments.get("price_anchor")),
        is_default=bool(as_bool(arguments.get("is_default"), default=False)),
        active=as_bool(arguments.get("active"), default=True),
        service_id=service_id,
        price=price,
        currency=currency,
        description=as_text(arguments.get("description")),
    )
    if error:
        return _err(error, "Could not create that offer")
    return _ok(status="existing" if already else "created", offer=offer_brief(offer))


def update_offer_tool(arguments):
    offer_id, error = _id_arg(arguments, "offer_id")
    if error:
        return error
    fields = {}
    if "name" in arguments:
        fields["name"] = as_text(arguments.get("name"))
    for text_field in ("pitch", "proof_point", "price_anchor", "description", "currency"):
        if text_field in arguments:
            fields[text_field] = as_text(arguments.get(text_field))
    if "price" in arguments:
        raw = arguments.get("price")
        fields["price"] = None if raw in (None, "") else as_number(raw)
    if "active" in arguments:
        flag = as_bool(arguments.get("active"))
        if flag is not None:
            fields["active"] = flag
    if "is_default" in arguments:
        flag = as_bool(arguments.get("is_default"))
        if flag is not None:
            fields["is_default"] = flag
    if arguments.get("service_id") not in (None, "") or as_text(arguments.get("service_slug")).strip():
        service, error = _resolve_service(arguments)
        if error:
            return error
        fields["service_id"] = service["id"]
    elif "service_id" in arguments and arguments.get("service_id") in (None, "", 0):
        fields["service_id"] = None
    if not fields:
        return _err("no_fields", "Pass at least one offer field to change")
    offer, error = update_offer(offer_id, **fields)
    if error:
        messages = {
            "not_found": f"No offer with id {offer_id}",
            "name_required": "name is required",
            "invalid_service": "Unknown service",
        }
        return _err(error, messages.get(error, error))
    return _ok(status="updated", offer=offer_brief(offer))


def set_deal_owner_tool(arguments):
    deal_id, error = _id_arg(arguments, "deal_id")
    if error:
        return error
    deal, error = _require_deal(deal_id)
    if error:
        return error
    if "owner_key" not in arguments and "owner" not in arguments:
        return _err("owner_required", "Pass owner_key (e.g. alice, bob, sales-agent)")
    owner_key = _owner_arg(arguments)
    update_deal_fields(deal_id, owner_key=owner_key)
    return _ok(status="updated", deal=deal_brief(get_deal(deal_id)))


def bulk_update_deals_tool(arguments):
    raw_ids = arguments.get("deal_ids")
    deal_ids = []
    if isinstance(raw_ids, list):
        for item in raw_ids:
            parsed = as_int(item)
            if parsed is not None and parsed >= 1:
                deal_ids.append(parsed)
        deal_ids = deal_ids[:MAX_BULK]
    elif raw_ids not in (None, ""):
        return _err("invalid_ids", "deal_ids must be an array of positive integers")

    stage_filter = as_text(arguments.get("stage")).strip() or None
    if stage_filter and not pipeline_stages.get_stage(stage_filter):
        return _err("invalid_stage", f"Unknown stage {stage_filter!r}. Call list_catalog.")
    service_slug = as_text(arguments.get("service_slug")).strip().lower() or None
    owner_key = _owner_arg(arguments)

    if not deal_ids and not service_slug:
        return _err(
            "filter_required",
            "Pass deal_ids or service_slug (optionally with stage / owner) so this cannot touch the whole pipeline.",
        )

    set_stage = as_text(arguments.get("set_stage")).strip() or None
    if set_stage and not pipeline_stages.get_stage(set_stage):
        return _err("invalid_stage", f"Unknown set_stage {set_stage!r}. Call list_catalog.")
    next_action = arguments.get("next_action") if "next_action" in arguments else None
    next_action_date = arguments.get("next_action_date") if "next_action_date" in arguments else None
    if next_action is not None:
        next_action = as_text(next_action)
    if next_action_date is not None:
        next_action_date = as_text(next_action_date).strip()[:10] or None
    if not set_stage and next_action is None and next_action_date is None:
        return _err("no_fields", "Pass set_stage and/or next_action / next_action_date")

    rows = list_deals(
        stage=stage_filter,
        owner_key=owner_key,
        service_slug=service_slug,
        deal_ids=deal_ids or None,
    )
    truncated = len(rows) > MAX_BULK
    rows = rows[:MAX_BULK]
    updated = []
    for deal in rows:
        if set_stage:
            set_deal_stage(deal["id"], set_stage)
        fields = {}
        if next_action is not None:
            fields["next_action"] = next_action
        if next_action_date is not None:
            fields["next_action_date"] = next_action_date
        if fields:
            update_deal_fields(deal["id"], **fields)
        updated.append(deal["id"])
    return _ok(
        status="updated",
        updated_ids=updated,
        count=len(updated),
        truncated=truncated,
    )


def _require_task(task_id):
    task = get_task(task_id)
    if not task:
        return None, _err("not_found", f"No delegated task with id {task_id}")
    return task, None


def create_delegated_task_tool(arguments):
    deal_id, error = _id_arg(arguments, "deal_id")
    if error:
        return error
    deal, error = _require_deal(deal_id)
    if error:
        return error
    title = as_text(arguments.get("title")).strip()
    if not title:
        return _err("title_required", "title is required")
    owner = arguments.get("owner_key")
    if owner in (None, ""):
        owner = arguments.get("owner")
    task, error, created = create_task(
        deal_id,
        title,
        brief=as_text(arguments.get("brief")),
        owner=owner,
        status=as_text(arguments.get("status")).strip() or None,
        due_date=as_text(arguments.get("due_date")).strip() or None,
        created_by=as_text(arguments.get("created_by")).strip() or "agent",
    )
    if error == "invalid_owner":
        return _err("invalid_owner", "owner must be a slug: letters, digits, hyphens")
    if error == "invalid_status":
        return _err("invalid_status", f"status must be one of: {', '.join(TASK_STATUSES)}")
    if error:
        return _err(error, "Could not create that task")
    return _ok(
        status="created" if created else "existing",
        task=delegated_task_brief(task),
        webhook=task.get("webhook"),
        partner=partner_brief(get_partner(deal["partner_id"])),
    )


def list_delegated_tasks_tool(arguments):
    deal_id = as_int(arguments.get("deal_id"))
    if arguments.get("deal_id") not in (None, "") and (deal_id is None or deal_id < 1):
        return _err("invalid_id", "deal_id must be a positive integer")
    owner = arguments.get("owner_key")
    if owner in (None, ""):
        owner = arguments.get("owner")
    status = as_text(arguments.get("status")).strip() or None
    if status:
        if not clean_task_status(status):
            return _err("invalid_status", f"status must be one of: {', '.join(TASK_STATUSES)}")
    due_only = as_bool(arguments.get("due_only"), default=False)
    limit = clamp_limit(arguments.get("limit"))
    rows = list_tasks(owner=owner, status=status, deal_id=deal_id, due_only=bool(due_only))
    sliced, truncated = paginate(rows, limit)
    return _ok(
        tasks=[delegated_task_brief(t) for t in sliced],
        count=len(sliced),
        truncated=truncated,
        owner=owner or None,
        status=status,
        due_only=bool(due_only),
    )


def get_delegated_task_tool(arguments):
    task_id, error = _id_arg(arguments, "task_id")
    if error:
        return error
    task, error = _require_task(task_id)
    if error:
        return error
    return _ok(
        task=delegated_task_brief(task),
        partner=partner_brief(get_partner(task["partner_id"])),
        deal=deal_brief(get_deal(task["deal_id"])),
    )


def update_delegated_task_tool(arguments):
    task_id, error = _id_arg(arguments, "task_id")
    if error:
        return error
    _, error = _require_task(task_id)
    if error:
        return error
    fields = {}
    if "title" in arguments:
        fields["title"] = as_text(arguments.get("title"))
    if "brief" in arguments:
        fields["brief"] = as_text(arguments.get("brief"))
    if "result_notes" in arguments:
        fields["result_notes"] = as_text(arguments.get("result_notes"))
    if "due_date" in arguments:
        fields["due_date"] = as_text(arguments.get("due_date"))
    if "status" in arguments:
        fields["status"] = as_text(arguments.get("status"))
    if "owner" in arguments or "owner_key" in arguments:
        owner = arguments.get("owner_key")
        if owner in (None, ""):
            owner = arguments.get("owner")
        fields["owner"] = owner
    if not fields:
        return _err("no_fields", "Pass status, due_date, brief, result_notes, and/or owner")
    task, error = update_task(task_id, **fields)
    if error == "invalid_owner":
        return _err("invalid_owner", "owner must be a slug: letters, digits, hyphens")
    if error == "invalid_status":
        return _err("invalid_status", f"status must be one of: {', '.join(TASK_STATUSES)}")
    if error:
        return _err(error, "Could not update that task")
    return _ok(status="updated", task=delegated_task_brief(task), webhook=task.get("webhook"))


def complete_delegated_task_tool(arguments):
    task_id, error = _id_arg(arguments, "task_id")
    if error:
        return error
    _, error = _require_task(task_id)
    if error:
        return error
    task, error = complete_task(task_id, result_notes=as_text(arguments.get("result_notes")))
    if error:
        return _err(error, "Could not complete that task")
    return _ok(status="done", task=delegated_task_brief(task))


# Schema fragments reused in the tool list. Bots read descriptions to decide.
_LIMIT_PROP = {
    "type": "integer",
    "minimum": 1,
    "maximum": MAX_LIMIT,
    "description": f"Max rows to return (default {DEFAULT_LIMIT}, max {MAX_LIMIT}).",
}
_ACTIVITY_LIMIT_PROP = {
    "type": "integer",
    "minimum": 1,
    "maximum": MAX_ACTIVITIES,
    "description": f"Max timeline rows (default 10, max {MAX_ACTIVITIES}).",
}
_OWNER_PROP = {
    "type": "string",
    "description": "Assignee slug (alice, bob, sales-agent). Letters, digits, hyphens.",
}

TOOLS = [
    {
        "name": "search_partners",
        "description": (
            "Find partners (people or companies) by name, email, phone, or website. "
            "Nested people under a company are included. Call this before create_partner. "
            "Empty query lists partners (capped). Results include parent company when set."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string", "description": "Name or email substring. Empty = list."},
                "limit": _LIMIT_PROP,
            },
        },
        "annotations": {
            "title": "Search partners",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "handler": search_partners,
    },
    {
        "name": "get_partner",
        "description": (
            "Full partner record: profile, parent company, people, deals, recent activities. "
            "Use after search_partners when you have an id."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["partner_id"],
            "properties": {
                "partner_id": {"type": "integer", "minimum": 1},
                "activity_limit": _ACTIVITY_LIMIT_PROP,
            },
        },
        "annotations": {
            "title": "Get partner",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "handler": get_partner_tool,
    },
    {
        "name": "list_deals",
        "description": (
            "List deals, optionally filtered by pipeline stage key, partner_id, "
            "owner / owner_key (alice, bob, sales-agent), service_slug, and "
            "parent_deal_id (follow-on deals). "
            "due_only=true is the follow-up queue (next_action_date today or earlier)."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "stage": {"type": "string", "description": "Pipeline stage key from list_catalog."},
                "partner_id": {"type": "integer", "minimum": 1},
                "owner": _OWNER_PROP,
                "owner_key": _OWNER_PROP,
                "service_slug": {"type": "string", "description": "Filter by catalog service slug."},
                "parent_deal_id": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Only child deals of this parent (HC ladder).",
                },
                "due_only": {"type": "boolean", "description": "Only deals whose next action is due/overdue."},
                "limit": _LIMIT_PROP,
            },
        },
        "annotations": {
            "title": "List deals",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "handler": list_deals_tool,
    },
    {
        "name": "get_deal",
        "description": (
            "One deal plus partner, service, offer, ICP fit score, delegated_tasks, "
            "child_deals (id, service slug, stage, offer), "
            "and recent activities. The talk track for a call lives here "
            "(pain_points, goals, next_action). parent_deal_id is on the deal."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["deal_id"],
            "properties": {
                "deal_id": {"type": "integer", "minimum": 1},
                "activity_limit": _ACTIVITY_LIMIT_PROP,
            },
        },
        "annotations": {
            "title": "Get deal",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "handler": get_deal_tool,
    },
    {
        "name": "list_activities",
        "description": "Timeline for a partner or a deal, newest first.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "partner_id": {"type": "integer", "minimum": 1},
                "deal_id": {"type": "integer", "minimum": 1},
                "limit": _ACTIVITY_LIMIT_PROP,
            },
        },
        "annotations": {
            "title": "List activities",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "handler": list_activities_tool,
    },
    {
        "name": "get_call_queue",
        "description": (
            "Today's calls: deals with a dialable phone. Default is qualified-pool only, "
            "ranked by the call-priority score. For ingested niche lists still at stage=new, "
            "pass include_new=true and/or stage=new plus service_slug "
            "(e.g. dead-lead-reactivation). Optional filters: source_prefix, owner."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_LIMIT,
                    "description": f"Max rows (default {CALL_QUEUE_LIMIT}, max {MAX_LIMIT}).",
                },
                "stage": {"type": "string", "description": "Replace the default qualified-pool with this stage key."},
                "service_slug": {"type": "string"},
                "source_prefix": {"type": "string", "description": "deals.source starts with this (case-insensitive)."},
                "include_new": {
                    "type": "boolean",
                    "description": "Also include the default stage (usually new) alongside the qualified pool.",
                },
                "owner": _OWNER_PROP,
                "owner_key": _OWNER_PROP,
            },
        },
        "annotations": {
            "title": "Today's call queue",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "handler": get_call_queue_tool,
    },
    {
        "name": "list_due_followups",
        "description": "Open deals whose next_action_date is today or earlier, oldest date first.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "stage": {"type": "string"},
                "owner": _OWNER_PROP,
                "owner_key": _OWNER_PROP,
                "service_slug": {"type": "string"},
                "limit": _LIMIT_PROP,
            },
        },
        "annotations": {
            "title": "Due follow-ups",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "handler": list_due_followups_tool,
    },
    {
        "name": "list_catalog",
        "description": (
            "Services, pipeline stages (with role flags), offers, call-outcome keys, "
            "and allowed activity types. Call this before set_deal_stage or record_call_outcome. "
            "is_won / is_lost flag the terminal stages."
        ),
        "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}},
        "annotations": {
            "title": "List catalog",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "handler": list_catalog_tool,
    },
    {
        "name": "ingest_leads",
        "description": (
            "Idempotent lead ingest (same rules as POST /api/v1/leads). "
            "Find-or-create partner (email, else phone+name, else website+name), "
            "skip a duplicate open deal for the same service, fill empty fields on re-run. "
            "Email is optional — company+phone scraped rows are valid. "
            "If name equals company_name, the deal is attached to the company partner. "
            "Preferred path for new inbound leads."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["leads"],
            "properties": {
                "leads": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_INGEST,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "email": {"type": "string"},
                            "company_name": {"type": "string"},
                            "service_slug": {"type": "string"},
                            "phone": {"type": "string"},
                            "title": {"type": "string"},
                            "website": {"type": "string"},
                            "address": {"type": "string"},
                            "social_url": {"type": "string"},
                            "linkedin_url": {"type": "string"},
                            "x_url": {"type": "string"},
                            "instagram_url": {"type": "string"},
                            "facebook_url": {"type": "string"},
                            "youtube_url": {"type": "string"},
                            "preferred_channel": {"type": "string"},
                            "industry": {"type": "string"},
                            "team_size": {"type": "integer"},
                            "source": {"type": "string"},
                            "value_estimate": {"type": "number"},
                            "pain_points": {"type": "string"},
                            "goals": {"type": "string"},
                            "next_action": {"type": "string"},
                            "next_action_date": {"type": "string"},
                            "is_company": {"type": "boolean"},
                            "owner_key": _OWNER_PROP,
                            "offer_id": {"type": "integer", "minimum": 1},
                            "external_ref": {
                                "type": "string",
                                "description": "Freeform project/engagement ref (e.g. acme-childcare-2026). Does not clobber pain_points/goals. Timeline notes: log_activity type=note.",
                            },
                        },
                    },
                },
            },
        },
        "annotations": {
            "title": "Ingest leads",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "handler": ingest_leads_tool,
    },
    {
        "name": "create_partner",
        "description": (
            "Create a partner after search_partners found nothing. "
            "If email already exists, returns the existing row instead of duplicating. "
            "For inbound leads prefer ingest_leads."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["name"],
            "properties": {
                "name": {"type": "string"},
                "is_company": {"type": "boolean"},
                "parent_id": {"type": "integer", "minimum": 1},
                "email": {"type": "string"},
                "phone": {"type": "string"},
                "website": {"type": "string"},
                "title": {"type": "string"},
                "address": {"type": "string"},
                "preferred_channel": {"type": "string", "enum": ["email", "phone", "text"]},
                "industry": {"type": "string"},
                "team_size": {"type": "integer", "minimum": 0},
                "social_url": {"type": "string"},
                "linkedin_url": {"type": "string"},
                "x_url": {"type": "string"},
                "instagram_url": {"type": "string"},
                "facebook_url": {"type": "string"},
                "youtube_url": {"type": "string"},
                "owner_key": _OWNER_PROP,
            },
        },
        "annotations": {
            "title": "Create partner",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
        "handler": create_partner_tool,
    },
    {
        "name": "update_partner",
        "description": (
            "Update a partner profile. fill_empty_only defaults true: only blank "
            "fields are written, so a bot re-run cannot clobber an operator edit. "
            "Set fill_empty_only=false only when correcting a wrong value."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["partner_id"],
            "properties": {
                "partner_id": {"type": "integer", "minimum": 1},
                "fill_empty_only": {
                    "type": "boolean",
                    "description": "Default true. False overwrites existing values.",
                },
                "name": {"type": "string"},
                "is_company": {"type": "boolean"},
                "parent_id": {"type": ["integer", "null"]},
                "email": {"type": "string"},
                "phone": {"type": "string"},
                "website": {"type": "string"},
                "title": {"type": "string"},
                "address": {"type": "string"},
                "preferred_channel": {"type": "string"},
                "industry": {"type": "string"},
                "team_size": {"type": "integer", "minimum": 0},
                "social_url": {"type": "string"},
                "linkedin_url": {"type": "string"},
                "x_url": {"type": "string"},
                "instagram_url": {"type": "string"},
                "facebook_url": {"type": "string"},
                "youtube_url": {"type": "string"},
                "owner_key": _OWNER_PROP,
            },
        },
        "annotations": {
            "title": "Update partner",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "handler": update_partner_tool,
    },
    {
        "name": "create_deal",
        "description": (
            "Open a deal for an existing partner + service. If an open deal already "
            "exists for that pair, returns it as duplicate_open_deal instead of creating a second. "
            "parent_deal_id links a follow-on (must share partner_id; cycles rejected). "
            "For brand-new inbound leads prefer ingest_leads. "
            "Do not invent Automation prices — attach an existing offer_id or leave empty."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["partner_id"],
            "properties": {
                "partner_id": {"type": "integer", "minimum": 1},
                "service_id": {"type": "integer", "minimum": 1},
                "service_slug": {"type": "string"},
                "stage": {"type": "string"},
                "source": {"type": "string"},
                "value_estimate": {"type": "number"},
                "pain_points": {"type": "string"},
                "goals": {"type": "string"},
                "next_action": {"type": "string"},
                "next_action_date": {"type": "string", "description": "YYYY-MM-DD"},
                "offer_id": {"type": "integer", "minimum": 1},
                "owner": _OWNER_PROP,
                "owner_key": _OWNER_PROP,
                "external_ref": {"type": "string", "description": "Freeform engagement/project ref. Does not clobber pain_points/goals."},
                "parent_deal_id": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Parent deal id (same partner). Used for HC → Automation/Support children.",
                },
            },
        },
        "annotations": {
            "title": "Create deal",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "handler": create_deal_tool,
    },
    {
        "name": "update_deal",
        "description": (
            "Edit deal fields (source, value, pain_points, goals, next_action, offer, "
            "owner_key, external_ref, parent_deal_id). Does not change stage — use set_deal_stage, "
            "set_deal_owner, record_call_outcome, or bulk_update_deals. "
            "parent_deal_id null unlinks. Child must share the parent's partner_id; cycles are rejected."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["deal_id"],
            "properties": {
                "deal_id": {"type": "integer", "minimum": 1},
                "source": {"type": "string"},
                "value_estimate": {"type": "number"},
                "pain_points": {"type": "string"},
                "goals": {"type": "string"},
                "next_action": {"type": "string"},
                "next_action_date": {"type": "string"},
                "offer_id": {"type": "integer", "minimum": 1},
                "owner": _OWNER_PROP,
                "owner_key": _OWNER_PROP,
                "external_ref": {"type": "string"},
                "parent_deal_id": {
                    "type": ["integer", "null"],
                    "description": "Parent deal id, or null to unlink.",
                },
            },
        },
        "annotations": {
            "title": "Update deal",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "handler": update_deal_tool,
    },
    {
        "name": "set_deal_stage",
        "description": (
            "Move a deal to a pipeline stage key from list_catalog. "
            "Entering a nurture-trigger stage POSTs the partner to NURTURE_WEBHOOK_URL "
            "(no-op if unconfigured). After a live call, prefer record_call_outcome."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["deal_id", "stage"],
            "properties": {
                "deal_id": {"type": "integer", "minimum": 1},
                "stage": {"type": "string"},
            },
        },
        "annotations": {
            "title": "Set deal stage",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "handler": set_deal_stage_tool,
    },
    {
        "name": "record_call_outcome",
        "description": (
            "Log a live-call result and move the deal per the configured outcomes "
            "(no_answer, interested, meeting_scheduled, won, not_interested). "
            "no_answer leaves the stage so the deal stays in today's queue."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["deal_id", "outcome"],
            "properties": {
                "deal_id": {"type": "integer", "minimum": 1},
                "outcome": {"type": "string", "enum": list(_CALL_OUTCOME_KEYS)},
                "note": {"type": "string", "description": "Optional qualification detail appended to the activity."},
            },
        },
        "annotations": {
            "title": "Record call outcome",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
        "handler": record_call_outcome_tool,
    },
    {
        "name": "log_activity",
        "description": (
            "Append a timeline entry (call, email, meeting, or note). "
            "system is reserved for the app. Always log work you did so the operator can see it."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["partner_id", "body"],
            "properties": {
                "partner_id": {"type": "integer", "minimum": 1},
                "deal_id": {"type": "integer", "minimum": 1},
                "type": {"type": "string", "enum": list(_BOT_ACTIVITY_TYPES)},
                "body": {"type": "string"},
            },
        },
        "annotations": {
            "title": "Log activity",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
        "handler": log_activity_tool,
    },
    {
        "name": "create_service",
        "description": (
            "Add a catalog service (an operator-defined thing this business sells or "
            "delivers, e.g. a consulting package or a support retainer). Idempotent on "
            "slug: re-calling returns the existing row. Slug is derived from name if "
            "omitted. Does not delete or deactivate."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["name"],
            "properties": {
                "name": {"type": "string"},
                "slug": {
                    "type": "string",
                    "description": "URL slug. Derived from name if omitted (ai-concierge).",
                },
                "description": {"type": "string"},
                "nurture_list_slug": {
                    "type": "string",
                    "description": "Optional list slug sent to the nurture webhook.",
                },
            },
        },
        "annotations": {
            "title": "Create service",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "handler": create_service_tool,
    },
    {
        "name": "update_service",
        "description": (
            "Update a catalog service's name, slug, description, active flag, or "
            "nurture_list_slug. service_id from list_catalog or create_service. "
            "active=false hides it from list_catalog (does not delete). "
            "Changing slug does not rewrite existing deals (they store service_id)."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["service_id"],
            "properties": {
                "service_id": {"type": "integer", "minimum": 1},
                "name": {"type": "string"},
                "slug": {
                    "type": "string",
                    "description": "URL slug. 2–50 lowercase letters, digits, hyphens. Must stay unique.",
                },
                "description": {"type": "string"},
                "active": {
                    "type": "boolean",
                    "description": "false hides the service from list_catalog. Does not delete.",
                },
                "nurture_list_slug": {
                    "type": "string",
                    "description": "List slug sent to the nurture webhook. Empty clears it.",
                },
            },
        },
        "annotations": {
            "title": "Update service",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "handler": update_service_tool,
    },
    {
        "name": "create_offer",
        "description": (
            "Create a priced offer (USD) linked to a service. Idempotent on name+service: "
            "re-calling with the same name returns the existing row. "
            "list_catalog.offers is the read path. Attach with create_deal / update_deal offer_id."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["name"],
            "properties": {
                "name": {"type": "string"},
                "service_id": {"type": "integer", "minimum": 1},
                "service_slug": {"type": "string"},
                "price": {"type": "number", "description": "Price in USD. Omit for TBD placeholders."},
                "currency": {"type": "string", "description": "ISO currency. Default USD."},
                "description": {"type": "string"},
                "pitch": {"type": "string"},
                "proof_point": {"type": "string"},
                "price_anchor": {"type": "string", "description": "Display string (e.g. $350). Auto-filled from price if omitted."},
                "is_default": {"type": "boolean"},
                "active": {"type": "boolean"},
            },
        },
        "annotations": {
            "title": "Create offer",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "handler": create_offer_tool,
    },
    {
        "name": "update_offer",
        "description": "Update an offer's name, service, USD price, description, pitch, or active flag.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["offer_id"],
            "properties": {
                "offer_id": {"type": "integer", "minimum": 1},
                "name": {"type": "string"},
                "service_id": {"type": "integer", "minimum": 1},
                "service_slug": {"type": "string"},
                "price": {"type": "number"},
                "currency": {"type": "string"},
                "description": {"type": "string"},
                "pitch": {"type": "string"},
                "proof_point": {"type": "string"},
                "price_anchor": {"type": "string"},
                "is_default": {"type": "boolean"},
                "active": {"type": "boolean"},
            },
        },
        "annotations": {
            "title": "Update offer",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "handler": update_offer_tool,
    },
    {
        "name": "set_deal_owner",
        "description": (
            "Assign a deal to a human or bot (owner_key: alice, bob, sales-agent). "
            "Empty owner_key unassigns. Same field as update_deal(owner_key=...)."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["deal_id"],
            "properties": {
                "deal_id": {"type": "integer", "minimum": 1},
                "owner": _OWNER_PROP,
                "owner_key": _OWNER_PROP,
            },
        },
        "annotations": {
            "title": "Set deal owner",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "handler": set_deal_owner_tool,
    },
    {
        "name": "bulk_update_deals",
        "description": (
            "Set stage and/or next_action + next_action_date on up to "
            f"{MAX_BULK} deals. Pass deal_ids, or filter by service_slug "
            "(optionally stage and owner). Uses set_deal_stage so nurture side effects fire. "
            "Example: service_slug=dead-lead-reactivation, stage=new, set_stage=contacted, "
            "next_action_date=today."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "deal_ids": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 1},
                    "maxItems": MAX_BULK,
                },
                "service_slug": {"type": "string"},
                "stage": {"type": "string", "description": "Filter: current stage key."},
                "owner": _OWNER_PROP,
                "owner_key": _OWNER_PROP,
                "set_stage": {"type": "string", "description": "Move matching deals to this stage key."},
                "next_action": {"type": "string"},
                "next_action_date": {"type": "string", "description": "YYYY-MM-DD"},
            },
        },
        "annotations": {
            "title": "Bulk update deals",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "handler": bulk_update_deals_tool,
    },
    {
        "name": "create_delegated_task",
        "description": (
            "Hand deal-scoped work to another agent or a human. deal_id required; "
            "owner defaults to DELEGATE_DEFAULT_OWNER (agent) and status to delegated so that agent can poll it. "
            "Does not need the partner to have an email. "
            "Idempotent on deal_id+title+owner while the task is still open. "
            "When owner is the default owner and status=delegated, POSTs TASK_WEBHOOK_URL once "
            "(task_id in the body for ack)."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["deal_id", "title"],
            "properties": {
                "deal_id": {"type": "integer", "minimum": 1},
                "title": {"type": "string"},
                "brief": {"type": "string"},
                "owner": {
                    "type": "string",
                    "description": "Assignee slug (e.g. alice, sales-agent). Defaults to DELEGATE_DEFAULT_OWNER (agent).",
                },
                "owner_key": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": list(TASK_STATUSES),
                    "description": "proposed | delegated | done | cancelled. Default delegated when owner is the default owner.",
                },
                "due_date": {"type": "string", "description": "YYYY-MM-DD"},
                "created_by": {"type": "string", "description": "Who created it (default agent)."},
            },
        },
        "annotations": {
            "title": "Create delegated task",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "handler": create_delegated_task_tool,
    },
    {
        "name": "list_delegated_tasks",
        "description": (
            "List deal-scoped delegated tasks. The default-owner agent polls with its owner slug and "
            "status=delegated. due_only=true is due_date today or earlier. "
            "Each task includes webhook delivery (notified, notified_at, "
            "last_attempt_at, last_error) so you can debug webhook delivery without SQL."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "owner": {"type": "string"},
                "owner_key": {"type": "string"},
                "status": {"type": "string", "enum": list(TASK_STATUSES)},
                "deal_id": {"type": "integer", "minimum": 1},
                "due_only": {"type": "boolean"},
                "limit": _LIMIT_PROP,
            },
        },
        "annotations": {
            "title": "List delegated tasks",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "handler": list_delegated_tasks_tool,
    },
    {
        "name": "get_delegated_task",
        "description": (
            "Full delegated-task row including webhook delivery log "
            "(notified, notified_at, last_attempt_at, last_error). "
            "Use this when a webhook notify looks stuck — no SQL needed. "
            "Also returns the partner and deal."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["task_id"],
            "properties": {
                "task_id": {"type": "integer", "minimum": 1},
            },
        },
        "annotations": {
            "title": "Get delegated task",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "handler": get_delegated_task_tool,
    },
    {
        "name": "update_delegated_task",
        "description": (
            "Update a delegated task's status, due_date, brief, result_notes, or owner. "
            "Moving status to delegated with the default owner fires the task webhook once."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["task_id"],
            "properties": {
                "task_id": {"type": "integer", "minimum": 1},
                "status": {"type": "string", "enum": list(TASK_STATUSES)},
                "due_date": {"type": "string"},
                "brief": {"type": "string"},
                "result_notes": {"type": "string"},
                "owner": {"type": "string"},
                "owner_key": {"type": "string"},
                "title": {"type": "string"},
            },
        },
        "annotations": {
            "title": "Update delegated task",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "handler": update_delegated_task_tool,
    },
    {
        "name": "complete_delegated_task",
        "description": (
            "Mark a delegated task done, store result_notes, and log a note on the deal "
            "timeline so the operator sees it. Idempotent if already done."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["task_id"],
            "properties": {
                "task_id": {"type": "integer", "minimum": 1},
                "result_notes": {"type": "string"},
            },
        },
        "annotations": {
            "title": "Complete delegated task",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "handler": complete_delegated_task_tool,
    },
]


TOOL_BY_NAME = {tool["name"]: tool for tool in TOOLS}


def list_tool_defs():
    defs = []
    for tool in TOOLS:
        defs.append({
            "name": tool["name"],
            "description": tool["description"],
            "inputSchema": tool["inputSchema"],
            "annotations": tool["annotations"],
        })
    return defs


def call_tool(name, arguments):
    tool = TOOL_BY_NAME.get(name)
    if not tool:
        return None, f"Unknown tool {name!r}. Use tools/list."
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return _err("invalid_arguments", "Tool arguments must be a JSON object"), None
    try:
        return tool["handler"](arguments), None
    except Exception:
        return _err("server_error", "The tool failed internally. Retry with the same arguments once; then stop."), None
