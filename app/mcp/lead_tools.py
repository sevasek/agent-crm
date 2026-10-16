"""MCP wrappers for the lead tools."""
from app.mcp.serialize import deal_brief, partner_brief
from app.mcp.tools import (
    _err,
    _id_arg,
    _ok,
    _owner_arg,
    _parse_tags_arg,
    _tag_filter_arg,
    _tag_ops_arg,
)
from app.mcp.util import as_bool, as_int, as_number, as_text, clamp_limit
from app.services.deals import get_deal, mark_lost, restore_deal
from app.services.lead_records import (
    convert_lead,
    create_lead,
    find_duplicates,
    get_lead,
    list_leads,
    merge_leads,
    update_lead,
)
from app.services.lost_reasons import (
    create_lost_reason,
    list_lost_reasons,
    update_lost_reason,
)


_LEAD_CREATE_FIELDS = (
    "company_name", "contact_name", "name", "email", "phone", "website",
    "title", "address", "linkedin_url", "x_url", "instagram_url",
    "facebook_url", "youtube_url", "industry", "preferred_channel",
    "source", "pain_points", "goals", "next_action", "next_action_date",
    "external_ref", "expected_close", "service_slug",
)

_LEAD_ERROR_MESSAGES = {
    "invalid": "A lead needs an identity (email, phone, website, LinkedIn, company, or partner).",
    "invalid_service": "Unknown service_slug.",
    "partner_not_found": "No partner with that id.",
    "not_found": "No deal record with that id.",
    "not_a_lead": "That record is not a lead.",
    "lead_lost": "That lead is lost.",
    "already_lost": "That record is already lost.",
    "invalid_lost_reason": "Unknown lost reason.",
    "not_lost": "That record is not lost.",
    "merged": "A merged record cannot be restored.",
    "invalid_merge": "Invalid merge (opportunity as source, lost record, or same id).",
    "missing_service": "convert_lead needs a service_slug or a service on the lead.",
    "partner_ambiguous": "More than one partner matches. Pass partner_id or partner_action=create.",
    "duplicate_open_opportunity": "That partner already has an open opportunity for this service.",
    "identity_required": "Conversion needs enough identity to create or link a partner.",
    "duplicate_name": "A lost reason with that name already exists.",
    "name_required": "name is required.",
}


def _lead_err(error, extra=None):
    payload = extra if isinstance(extra, dict) else {}
    return _err(error, _LEAD_ERROR_MESSAGES.get(error, error), **payload)


def _kwargs_from_args(arguments, keys):
    out = {}
    for key in keys:
        if key not in arguments:
            continue
        if key in ("team_size", "probability", "priority", "partner_id"):
            out[key] = as_int(arguments.get(key))
        elif key == "value_estimate":
            out[key] = as_number(arguments.get(key))
        else:
            out[key] = as_text(arguments.get(key))
    return out


def create_lead_tool(arguments):
    kwargs = _kwargs_from_args(arguments, _LEAD_CREATE_FIELDS)
    if "team_size" in arguments:
        kwargs["team_size"] = as_int(arguments.get("team_size"))
    if "value_estimate" in arguments:
        kwargs["value_estimate"] = as_number(arguments.get("value_estimate"))
    if "probability" in arguments:
        kwargs["probability"] = as_int(arguments.get("probability"))
    if "priority" in arguments:
        kwargs["priority"] = as_int(arguments.get("priority"))
    if "partner_id" in arguments and arguments.get("partner_id") not in (None, "", 0, "0"):
        partner_id, error = _id_arg(arguments, "partner_id")
        if error:
            return error
        kwargs["partner_id"] = partner_id
    kwargs["owner"] = _owner_arg(arguments)
    if "tags" in arguments:
        tags, error = _parse_tags_arg(arguments.get("tags"))
        if error:
            return error
        kwargs["tags"] = tags
    lead, error = create_lead(**kwargs)
    if error:
        return _lead_err(error)
    duplicates, _dup_err = find_duplicates(lead_id=lead["id"])
    return _ok(lead=deal_brief(lead), possible_duplicates=duplicates or [])


def update_lead_tool(arguments):
    lead_id, error = _id_arg(arguments, "lead_id")
    if error:
        return error
    fields = _kwargs_from_args(arguments, _LEAD_CREATE_FIELDS)
    for key in ("team_size", "value_estimate", "probability", "priority", "partner_id"):
        if key in arguments:
            if key == "value_estimate":
                fields[key] = as_number(arguments.get(key))
            elif key == "partner_id" and arguments.get(key) in (None, "", 0, "0"):
                fields[key] = None
            else:
                fields[key] = as_int(arguments.get(key))
    if "owner" in arguments or "owner_key" in arguments:
        fields["owner"] = _owner_arg(arguments)
    fill_empty_only = as_bool(arguments.get("fill_empty_only"), default=True)
    tag_ops, error = _tag_ops_arg(arguments)
    if error:
        return error
    lead, fail = update_lead(
        lead_id,
        fill_empty_only=bool(fill_empty_only),
        tags=tag_ops.get("replace") if tag_ops else None,
        add_tags=tag_ops.get("add") if tag_ops else None,
        remove_tags=tag_ops.get("remove") if tag_ops else None,
        **fields,
    )
    if fail:
        return _lead_err(fail)
    return _ok(lead=deal_brief(lead))


def get_lead_tool(arguments):
    lead_id, error = _id_arg(arguments, "lead_id")
    if error:
        return error
    lead = get_lead(lead_id)
    if not lead:
        deal = get_deal(lead_id)
        if not deal:
            return _lead_err("not_found")
        return _lead_err("not_a_lead")
    return _ok(lead=lead)


def list_leads_tool(arguments):
    tag_filter, error = _tag_filter_arg(arguments)
    if error:
        return error
    rows = list_leads(
        query=as_text(arguments.get("query")),
        owner=_owner_arg(arguments),
        service_slug=as_text(arguments.get("service_slug")).strip().lower(),
        tags=tag_filter,
        source_prefix=as_text(arguments.get("source_prefix")),
        min_fit=as_int(arguments.get("min_fit")),
        due_only=bool(as_bool(arguments.get("due_only"), default=False)),
        include_lost=bool(as_bool(arguments.get("include_lost"), default=False)),
        sort=as_text(arguments.get("sort")).strip() or "updated",
        limit=clamp_limit(arguments.get("limit"), default=50, maximum=50),
    )
    return _ok(leads=rows, count=len(rows))


def find_duplicates_tool(arguments):
    lead_id = None
    if arguments.get("lead_id") not in (None, ""):
        lead_id, error = _id_arg(arguments, "lead_id")
        if error:
            return error
    fields = _kwargs_from_args(
        arguments,
        ("company_name", "contact_name", "name", "email", "phone", "website", "linkedin_url"),
    )
    matches, fail = find_duplicates(lead_id=lead_id, **fields)
    if fail:
        return _lead_err(fail)
    return _ok(matches=matches or [])


def convert_lead_tool(arguments):
    lead_id, error = _id_arg(arguments, "lead_id")
    if error:
        return error
    partner_id = None
    if arguments.get("partner_id") not in (None, "", 0, "0"):
        partner_id, error = _id_arg(arguments, "partner_id")
        if error:
            return error
    result, fail = convert_lead(
        lead_id,
        partner_action=as_text(arguments.get("partner_action")).strip() or "auto",
        partner_id=partner_id,
        service_slug=as_text(arguments.get("service_slug")),
        stage=as_text(arguments.get("stage")).strip() or None,
        value_estimate=as_number(arguments.get("value_estimate")),
        expected_close=as_text(arguments.get("expected_close")) or None,
        probability=as_int(arguments.get("probability")),
        offer_id=as_int(arguments.get("offer_id")),
        next_action=as_text(arguments.get("next_action")) or None,
        next_action_date=as_text(arguments.get("next_action_date")) or None,
        owner=_owner_arg(arguments),
        note=as_text(arguments.get("note")),
    )
    if fail:
        extra = result if isinstance(result, dict) else None
        return _lead_err(fail, extra)
    return _ok(
        opportunity=deal_brief(result.get("opportunity") or get_deal(lead_id)),
        partner=partner_brief(result.get("partner")),
        partner_created=bool(result.get("partner_created")),
        activity_id=result.get("activity_id"),
    )


def mark_lost_tool(arguments):
    deal_id, error = _id_arg(arguments, "deal_id")
    if error:
        return error
    reason_id = as_int(arguments.get("lost_reason_id"))
    deal, fail = mark_lost(
        deal_id,
        lost_reason_id=reason_id,
        lost_reason=as_text(arguments.get("lost_reason")) or None,
        note=as_text(arguments.get("note")),
    )
    if fail:
        return _lead_err(fail)
    payload = {"deal": deal_brief(deal)}
    if deal.get("nurture") is not None:
        payload["nurture"] = deal["nurture"]
    return _ok(**payload)


def restore_deal_tool(arguments):
    deal_id, error = _id_arg(arguments, "deal_id")
    if error:
        return error
    deal, fail = restore_deal(deal_id)
    if fail:
        return _lead_err(fail)
    return _ok(deal=deal_brief(deal))


def merge_leads_tool(arguments):
    target_id, error = _id_arg(arguments, "target_id")
    if error:
        return error
    raw = arguments.get("source_ids")
    if not isinstance(raw, list) or not raw:
        return _err("invalid_merge", "source_ids must be a non-empty array (max 10).")
    source_ids = []
    for item in raw[:10]:
        parsed = as_int(item)
        if parsed is None or parsed < 1:
            return _err("invalid_merge", "source_ids must be positive integers.")
        source_ids.append(parsed)
    result, fail = merge_leads(target_id, source_ids)
    if fail:
        return _lead_err(fail)
    return _ok(
        target=deal_brief(result.get("target") or get_deal(target_id)),
        merged_ids=result.get("merged_ids") or source_ids,
    )


def list_lost_reasons_tool(_arguments):
    rows = list_lost_reasons()
    return _ok(lost_reasons=rows, count=len(rows))


def create_lost_reason_tool(arguments):
    name = as_text(arguments.get("name")).strip()
    reason, fail = create_lost_reason(
        name,
        triggers_nurture=bool(as_bool(arguments.get("triggers_nurture"), default=False)),
        active=as_bool(arguments.get("active"), default=True),
    )
    if fail:
        return _lead_err(fail)
    return _ok(lost_reason=reason)


def update_lost_reason_tool(arguments):
    reason_id, error = _id_arg(arguments, "lost_reason_id")
    if error:
        return error
    fields = {}
    if "name" in arguments:
        fields["name"] = as_text(arguments.get("name"))
    if "triggers_nurture" in arguments:
        fields["triggers_nurture"] = as_bool(arguments.get("triggers_nurture"))
    if "active" in arguments:
        fields["active"] = as_bool(arguments.get("active"))
    reason, fail = update_lost_reason(reason_id, **fields)
    if fail:
        return _lead_err(fail)
    return _ok(lost_reason=reason)


_RW = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": False,
}
_RO = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
_LEAD_CONTACT_PROPS = {
    "company_name": {"type": "string"},
    "contact_name": {"type": "string"},
    "name": {"type": "string"},
    "email": {"type": "string"},
    "phone": {"type": "string"},
    "website": {"type": "string"},
    "title": {"type": "string"},
    "address": {"type": "string"},
    "linkedin_url": {"type": "string"},
    "x_url": {"type": "string"},
    "instagram_url": {"type": "string"},
    "facebook_url": {"type": "string"},
    "youtube_url": {"type": "string"},
    "industry": {"type": "string"},
    "team_size": {"type": "integer", "minimum": 0},
    "preferred_channel": {"type": "string"},
    "source": {"type": "string"},
    "tags": {"type": "array", "items": {"type": "string"}},
    "pain_points": {"type": "string"},
    "goals": {"type": "string"},
    "value_estimate": {"type": "number"},
    "probability": {"type": "integer", "minimum": 0, "maximum": 100},
    "priority": {"type": "integer", "minimum": 0, "maximum": 3},
    "expected_close": {"type": "string"},
    "next_action": {"type": "string"},
    "next_action_date": {"type": "string"},
    "owner": {"type": "string"},
    "owner_key": {"type": "string"},
    "external_ref": {"type": "string"},
    "service_slug": {"type": "string"},
    "partner_id": {"type": "integer", "minimum": 1},
}

LEAD_TOOLS = [
    {
        "name": "create_lead",
        "description": (
            "Create a lead (unqualified signal). Never creates a partner. "
            "Creates the lead even when duplicates exist; check possible_duplicates."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": _LEAD_CONTACT_PROPS,
        },
        "annotations": {"title": "Create lead", **_RW, "idempotentHint": False},
        "handler": create_lead_tool,
    },
    {
        "name": "update_lead",
        "description": "Update a lead. fill_empty_only defaults true.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["lead_id"],
            "properties": {
                "lead_id": {"type": "integer", "minimum": 1},
                "fill_empty_only": {"type": "boolean"},
                "add_tags": {"type": "array", "items": {"type": "string"}},
                "remove_tags": {"type": "array", "items": {"type": "string"}},
                **_LEAD_CONTACT_PROPS,
            },
        },
        "annotations": {"title": "Update lead", **_RW, "idempotentHint": True},
        "handler": update_lead_tool,
    },
    {
        "name": "get_lead",
        "description": (
            "One lead plus research notes, fit, possible_duplicates, and "
            "conversion_readiness. Call this before convert_lead."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["lead_id"],
            "properties": {"lead_id": {"type": "integer", "minimum": 1}},
        },
        "annotations": {"title": "Get lead", **_RO},
        "handler": get_lead_tool,
    },
    {
        "name": "list_leads",
        "description": "List leads. Default hides lost records.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string"},
                "owner": {"type": "string"},
                "owner_key": {"type": "string"},
                "service_slug": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "source_prefix": {"type": "string"},
                "min_fit": {"type": "integer"},
                "due_only": {"type": "boolean"},
                "include_lost": {"type": "boolean"},
                "sort": {"type": "string", "enum": ["fit", "created", "updated", "next_action_date", "last_research"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
        },
        "annotations": {"title": "List leads", **_RO},
        "handler": list_leads_tool,
    },
    {
        "name": "find_duplicates",
        "description": "Strong and weak matches against open leads, opportunities, and partners.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "lead_id": {"type": "integer", "minimum": 1},
                "company_name": {"type": "string"},
                "contact_name": {"type": "string"},
                "name": {"type": "string"},
                "email": {"type": "string"},
                "phone": {"type": "string"},
                "website": {"type": "string"},
                "linkedin_url": {"type": "string"},
            },
        },
        "annotations": {"title": "Find duplicates", **_RO},
        "handler": find_duplicates_tool,
    },
    {
        "name": "convert_lead",
        "description": (
            "Call get_lead first. Check conversion_readiness and possible_duplicates. "
            "This turns a lead into an opportunity on the same id."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["lead_id"],
            "properties": {
                "lead_id": {"type": "integer", "minimum": 1},
                "partner_action": {"type": "string", "enum": ["auto", "link", "create"]},
                "partner_id": {"type": "integer", "minimum": 1},
                "service_slug": {"type": "string"},
                "stage": {"type": "string"},
                "value_estimate": {"type": "number"},
                "expected_close": {"type": "string"},
                "probability": {"type": "integer"},
                "offer_id": {"type": "integer", "minimum": 1},
                "next_action": {"type": "string"},
                "next_action_date": {"type": "string"},
                "owner": {"type": "string"},
                "owner_key": {"type": "string"},
                "note": {"type": "string"},
            },
        },
        "annotations": {"title": "Convert lead", **_RW},
        "handler": convert_lead_tool,
    },
    {
        "name": "mark_lost",
        "description": "Archive a lead or opportunity with a lost reason. Lost is not a stage.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["deal_id"],
            "properties": {
                "deal_id": {"type": "integer", "minimum": 1},
                "lost_reason_id": {"type": "integer", "minimum": 1},
                "lost_reason": {"type": "string"},
                "note": {"type": "string"},
            },
        },
        "annotations": {"title": "Mark lost", **_RW},
        "handler": mark_lost_tool,
    },
    {
        "name": "restore_deal",
        "description": "Restore a lost lead or opportunity. Merged records cannot be restored.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["deal_id"],
            "properties": {"deal_id": {"type": "integer", "minimum": 1}},
        },
        "annotations": {"title": "Restore deal", **_RW},
        "handler": restore_deal_tool,
    },
    {
        "name": "merge_leads",
        "description": "Merge source leads into a target lead or opportunity (max 10 sources).",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["target_id", "source_ids"],
            "properties": {
                "target_id": {"type": "integer", "minimum": 1},
                "source_ids": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 10,
                    "items": {"type": "integer", "minimum": 1},
                },
            },
        },
        "annotations": {"title": "Merge leads", **_RW},
        "handler": merge_leads_tool,
    },
    {
        "name": "list_lost_reasons",
        "description": "Operator-configured lost reasons.",
        "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}},
        "annotations": {"title": "List lost reasons", **_RO},
        "handler": list_lost_reasons_tool,
    },
    {
        "name": "create_lost_reason",
        "description": "Add a lost reason.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["name"],
            "properties": {
                "name": {"type": "string"},
                "triggers_nurture": {"type": "boolean"},
                "active": {"type": "boolean"},
            },
        },
        "annotations": {"title": "Create lost reason", **_RW, "idempotentHint": False},
        "handler": create_lost_reason_tool,
    },
    {
        "name": "update_lost_reason",
        "description": "Change a lost reason's name, triggers_nurture, or active flag.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["lost_reason_id"],
            "properties": {
                "lost_reason_id": {"type": "integer", "minimum": 1},
                "name": {"type": "string"},
                "triggers_nurture": {"type": "boolean"},
                "active": {"type": "boolean"},
            },
        },
        "annotations": {"title": "Update lost reason", **_RW, "idempotentHint": True},
        "handler": update_lost_reason_tool,
    },
]
