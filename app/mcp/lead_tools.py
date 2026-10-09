"""MCP tools for leads. Existing deal tools stay; these are additive."""
from app.mcp.util import as_int, as_text
from app.services.lead_records import (
    convert_lead,
    create_lead,
    create_lost_reason,
    find_duplicates,
    get_lead,
    list_leads,
    list_lost_reasons,
    mark_lost,
    merge_leads,
    restore_deal,
    update_lead,
    update_lost_reason,
)


def _ok(**payload):
    payload["ok"] = True
    return payload


def _err(error, message, **extra):
    extra["ok"] = False
    extra["error"] = error
    extra["message"] = message
    return extra


def _from(result, key):
    if not result.get("ok"):
        return _err(result.get("error") or "invalid", result.get("error") or "invalid", **{
            k: v for k, v in result.items() if k not in ("ok", "error")
        })
    return _ok(**{k: v for k, v in result.items() if k != "ok"})


def create_lead_tool(arguments):
    return _from(create_lead(**arguments), "lead")


def update_lead_tool(arguments):
    lead_id = as_int(arguments.get("lead_id"))
    if not lead_id:
        return _err("invalid_id", "lead_id must be a positive integer")
    fields = {key: value for key, value in arguments.items() if key != "lead_id"}
    return _from(update_lead(lead_id, **fields), "lead")


def get_lead_tool(arguments):
    lead_id = as_int(arguments.get("lead_id"))
    if not lead_id:
        return _err("invalid_id", "lead_id must be a positive integer")
    return _from(get_lead(lead_id), "lead")


def list_leads_tool(arguments):
    return _from(list_leads(
        query=arguments.get("query"),
        owner=arguments.get("owner") or arguments.get("owner_key"),
        service_slug=arguments.get("service_slug"),
        tags=arguments.get("tags"),
        source_prefix=arguments.get("source_prefix"),
        min_fit=arguments.get("min_fit"),
        due_only=bool(arguments.get("due_only")),
        include_lost=bool(arguments.get("include_lost")),
        sort=arguments.get("sort") or "updated",
        limit=arguments.get("limit") or 50,
    ), "leads")


def find_duplicates_tool(arguments):
    lead_id = as_int(arguments.get("lead_id")) if arguments.get("lead_id") not in (None, "") else None
    fields = {key: value for key, value in arguments.items() if key != "lead_id"}
    return _from(find_duplicates(lead_id=lead_id, **fields), "matches")


def convert_lead_tool(arguments):
    lead_id = as_int(arguments.get("lead_id"))
    if not lead_id:
        return _err("invalid_id", "lead_id must be a positive integer")
    fields = {key: value for key, value in arguments.items() if key != "lead_id"}
    result = convert_lead(lead_id, **fields)
    if not result.get("ok"):
        return _err(result.get("error") or "invalid", result.get("error") or "invalid", **{
            k: v for k, v in result.items() if k not in ("ok", "error")
        })
    return _ok(
        opportunity=result["opportunity"],
        partner=result["partner"],
        partner_created=result["partner_created"],
        activity_id=result["activity_id"],
    )


def mark_lost_tool(arguments):
    deal_id = as_int(arguments.get("deal_id"))
    if not deal_id:
        return _err("invalid_id", "deal_id must be a positive integer")
    return _from(mark_lost(
        deal_id,
        lost_reason_id=arguments.get("lost_reason_id"),
        lost_reason=arguments.get("lost_reason"),
        note=arguments.get("note"),
    ), "record")


def restore_deal_tool(arguments):
    deal_id = as_int(arguments.get("deal_id"))
    if not deal_id:
        return _err("invalid_id", "deal_id must be a positive integer")
    return _from(restore_deal(deal_id), "record")


def merge_leads_tool(arguments):
    target_id = as_int(arguments.get("target_id"))
    source_ids = arguments.get("source_ids") or []
    if not target_id:
        return _err("invalid_id", "target_id must be a positive integer")
    return _from(merge_leads(target_id, source_ids), "target")


def list_lost_reasons_tool(_arguments):
    return _ok(lost_reasons=list_lost_reasons())


def create_lost_reason_tool(arguments):
    return _from(create_lost_reason(
        as_text(arguments.get("name")),
        triggers_nurture=bool(arguments.get("triggers_nurture")),
    ), "lost_reason")


def update_lost_reason_tool(arguments):
    reason_id = as_int(arguments.get("lost_reason_id") or arguments.get("id"))
    if not reason_id:
        return _err("invalid_id", "lost_reason_id must be a positive integer")
    return _from(update_lost_reason(
        reason_id,
        name=arguments.get("name"),
        triggers_nurture=arguments.get("triggers_nurture"),
        active=arguments.get("active"),
    ), "lost_reason")


def _tool(name, description, handler, properties, required=None):
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": required or [],
            "properties": properties,
        },
        "annotations": {
            "title": name.replace("_", " ").title(),
            "readOnlyHint": name.startswith("list_") or name.startswith("get_") or name.startswith("find_"),
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "handler": handler,
    }


_CONTACT = {
    "company_name": {"type": "string"},
    "contact_name": {"type": "string"},
    "name": {"type": "string"},
    "email": {"type": "string"},
    "phone": {"type": "string"},
    "website": {"type": "string"},
    "title": {"type": "string"},
    "source": {"type": "string"},
    "service_slug": {"type": "string"},
    "tags": {"type": "array", "items": {"type": "string"}},
    "next_action": {"type": "string"},
    "next_action_date": {"type": "string"},
    "owner": {"type": "string"},
    "owner_key": {"type": "string"},
}

LEAD_TOOLS = [
    _tool(
        "create_lead",
        "Create an unqualified lead. It does not create a partner. "
        "Duplicates are returned in possible_duplicates and do not block creation.",
        create_lead_tool,
        _CONTACT,
    ),
    _tool(
        "update_lead",
        "Fill empty fields on a lead by default. Pass fill_empty_only false to overwrite.",
        update_lead_tool,
        {**_CONTACT, "lead_id": {"type": "integer"}, "fill_empty_only": {"type": "boolean"},
         "add_tags": {"type": "array", "items": {"type": "string"}},
         "remove_tags": {"type": "array", "items": {"type": "string"}}},
        ["lead_id"],
    ),
    _tool(
        "get_lead",
        "Lead fields, research notes, fit, possible duplicates, and conversion readiness.",
        get_lead_tool,
        {"lead_id": {"type": "integer"}},
        ["lead_id"],
    ),
    _tool(
        "list_leads",
        "List leads. sort may be fit, created, updated, next_action_date, or last_research.",
        list_leads_tool,
        {
            "query": {"type": "string"},
            "owner": {"type": "string"},
            "service_slug": {"type": "string"},
            "source_prefix": {"type": "string"},
            "due_only": {"type": "boolean"},
            "include_lost": {"type": "boolean"},
            "sort": {"type": "string"},
            "limit": {"type": "integer"},
        },
    ),
    _tool(
        "find_duplicates",
        "Strong and weak matches among open leads, opportunities, and partners.",
        find_duplicates_tool,
        {**_CONTACT, "lead_id": {"type": "integer"}},
    ),
    _tool(
        "convert_lead",
        "Call get_lead first. Check conversion_readiness and possible_duplicates. "
        "Turns the lead into an opportunity on the same row.",
        convert_lead_tool,
        {
            "lead_id": {"type": "integer"},
            "partner_action": {"type": "string", "enum": ["auto", "link", "create"]},
            "partner_id": {"type": "integer"},
            "service_slug": {"type": "string"},
            "stage": {"type": "string"},
            "value_estimate": {"type": "number"},
            "expected_close": {"type": "string"},
            "note": {"type": "string"},
            "owner": {"type": "string"},
        },
        ["lead_id"],
    ),
    _tool(
        "mark_lost",
        "Mark a lead or an opportunity lost with a lost reason. The row is kept.",
        mark_lost_tool,
        {
            "deal_id": {"type": "integer"},
            "lost_reason": {"type": "string"},
            "lost_reason_id": {"type": "integer"},
            "note": {"type": "string"},
        },
        ["deal_id"],
    ),
    _tool(
        "restore_deal",
        "Undo a loss that was a mistake. A merged record cannot be restored.",
        restore_deal_tool,
        {"deal_id": {"type": "integer"}},
        ["deal_id"],
    ),
    _tool(
        "merge_leads",
        "Move data from source leads onto the target. Sources become lost with merged_into_id. Nothing is deleted.",
        merge_leads_tool,
        {
            "target_id": {"type": "integer"},
            "source_ids": {"type": "array", "items": {"type": "integer"}},
        },
        ["target_id", "source_ids"],
    ),
    _tool("list_lost_reasons", "Operator-configured reasons a lead or opportunity was lost.", list_lost_reasons_tool, {}),
    _tool(
        "create_lost_reason",
        "Add a lost reason. triggers_nurture sends the nurture webhook when a record is lost with this reason.",
        create_lost_reason_tool,
        {"name": {"type": "string"}, "triggers_nurture": {"type": "boolean"}},
        ["name"],
    ),
    _tool(
        "update_lost_reason",
        "Rename a lost reason, change triggers_nurture, or deactivate it.",
        update_lost_reason_tool,
        {
            "lost_reason_id": {"type": "integer"},
            "name": {"type": "string"},
            "triggers_nurture": {"type": "boolean"},
            "active": {"type": "boolean"},
        },
        ["lost_reason_id"],
    ),
]
