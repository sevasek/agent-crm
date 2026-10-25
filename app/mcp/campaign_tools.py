"""MCP wrappers for campaigns. Same service and metrics path as the pages."""
from app.mcp.tools import _err, _ok
from app.mcp.util import as_int, as_text
from app.services.campaign_metrics_adapter import (
    PERIODS,
    metrics_for_campaign,
    summary_from_metrics,
)
from app.services.campaigns import (
    ERROR_MESSAGES,
    STATUSES,
    create_campaign,
    get_campaign,
    list_campaign_deals,
    archive_campaign,
    list_campaigns,
    unarchive_campaign,
    update_campaign,
)
from app.services.catalog import get_service_by_slug

_READ = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
_WRITE = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}

_TEXT_FIELDS = ("audience", "angle", "opener", "channel", "status", "start_date", "deadline")
_TARGET_FIELDS = (
    "target_leads",
    "target_contacts",
    "target_conversations",
    "target_advanced",
    "target_won",
    "target_won_value",
)


def _fail(code):
    return _err(code, ERROR_MESSAGES.get(code, code))


def _resolve_campaign(arguments):
    if arguments.get("campaign_id") is not None:
        number = as_int(arguments.get("campaign_id"))
        if number is None or number < 1:
            return None, _err("invalid_id", "campaign_id must be a positive integer")
        campaign = get_campaign(number)
        if not campaign:
            return None, _fail("not_found")
        return campaign, None
    slug = as_text(arguments.get("slug")).strip()
    if not slug:
        return None, _err("id_required", "Pass campaign_id or slug")
    campaign = get_campaign(slug=slug)
    if not campaign:
        return None, _fail("not_found")
    return campaign, None


def _apply_links(arguments, fields):
    """Copy service_id / offer_id into fields. Return an error code or None."""
    slug = ""
    if "service_slug" in arguments:
        slug = as_text(arguments.get("service_slug")).strip().lower()
    if slug:
        service = get_service_by_slug(slug)
        if not service:
            return "invalid_service"
        fields["service_id"] = service["id"]
    elif "service_id" in arguments:
        fields["service_id"] = arguments.get("service_id")
    elif "service_slug" in arguments:
        fields["service_id"] = None
    if "offer_id" in arguments:
        fields["offer_id"] = arguments.get("offer_id")
    return None


def _collect_fields(arguments, *, creating):
    fields = {}
    if "name" in arguments or creating:
        fields["name"] = arguments.get("name")
    if "slug" in arguments:
        fields["slug"] = arguments.get("slug")
    if "new_slug" in arguments:
        fields["slug"] = arguments.get("new_slug")
    for key in _TEXT_FIELDS:
        if key in arguments:
            fields[key] = arguments.get(key)
    for key in _TARGET_FIELDS:
        if key in arguments:
            fields[key] = arguments.get(key)
    error = _apply_links(arguments, fields)
    if error:
        return None, error
    return fields, None


def list_campaigns_tool(arguments):
    status = as_text(arguments.get("status")).strip().lower() or None
    if status and status not in STATUSES:
        return _fail("invalid_status")
    rows = []
    include_archived = bool(arguments.get("include_archived"))
    for campaign in list_campaigns(status=status, include_archived=include_archived):
        summary = summary_from_metrics(metrics_for_campaign(campaign))
        rows.append({
            "id": campaign["id"],
            "name": campaign["name"],
            "slug": campaign["slug"],
            "tag": campaign["tag"],
            "status": campaign["status"],
            "channel": campaign["channel"],
            "start_date": campaign["start_date"],
            "deadline": campaign["deadline"],
            "win_loss": {
                "won": summary["won"],
                "lost": summary["lost"],
                "disqualified": summary["disqualified"],
                "open": summary["open"],
                "label": summary["win_loss_label"],
                "win_rate": summary["win_rate"],
            },
            "pct_complete": summary["pct_complete"],
            "pace": summary["pace"],
        })
    return _ok(campaigns=rows, count=len(rows))


def get_campaign_tool(arguments):
    campaign, error = _resolve_campaign(arguments)
    if error:
        return error
    period = as_text(arguments.get("period")).strip().lower() or None
    if period and period not in PERIODS:
        return _err("invalid_period", "period must be week, month, or year")
    metrics = metrics_for_campaign(campaign, period=period)
    deals = []
    for deal in list_campaign_deals(campaign["slug"]):
        deals.append({
            "id": deal["id"],
            "type": deal.get("type"),
            "name": deal.get("display_name"),
            "stage": deal.get("stage"),
            "outcome": deal.get("outcome"),
            "active": deal.get("active"),
            "value_estimate": deal.get("value_estimate"),
            "partner_id": deal.get("partner_id"),
            "service_id": deal.get("service_id"),
        })
    return _ok(campaign=campaign, metrics=metrics, deals=deals)


def archive_campaign_tool(arguments):
    campaign, error = _resolve_campaign(arguments)
    if error:
        return error
    if arguments.get("unarchive"):
        updated, error = unarchive_campaign(campaign["id"])
        if error:
            return _fail(error)
        return _ok(campaign=updated, closed_deal_ids=[])
    updated, closed, error = archive_campaign(
        campaign["id"], close_untouched=bool(arguments.get("close_untouched")),
    )
    if error:
        return _fail(error)
    return _ok(campaign=updated, closed_deal_ids=closed)


def create_campaign_tool(arguments):
    name = as_text(arguments.get("name")).strip()
    if not name:
        return _fail("name_required")
    fields, error = _collect_fields(arguments, creating=True)
    if error:
        return _fail(error)
    fields["name"] = name
    created, error = create_campaign(name, **{key: value for key, value in fields.items() if key != "name"})
    if error:
        return _fail(error)
    return _ok(campaign=created)


def update_campaign_tool(arguments):
    campaign, error = _resolve_campaign(arguments)
    if error:
        return error
    fields, error = _collect_fields(arguments, creating=False)
    if error:
        return _fail(error)
    # Lookup slug must not be written back as a rename. Rename is new_slug.
    if "slug" in fields and "new_slug" not in arguments:
        fields.pop("slug", None)
    if "name" in fields and fields["name"] is None:
        return _fail("name_required")
    updated, error = update_campaign(campaign["id"], **fields)
    if error:
        return _fail(error)
    return _ok(campaign=updated)


_TARGET_PROPS = {
    "target_leads": {"type": ["integer", "null"], "minimum": 0},
    "target_contacts": {"type": ["integer", "null"], "minimum": 0},
    "target_conversations": {"type": ["integer", "null"], "minimum": 0},
    "target_advanced": {"type": ["integer", "null"], "minimum": 0},
    "target_won": {"type": ["integer", "null"], "minimum": 0},
    "target_won_value": {"type": ["number", "null"], "minimum": 0},
}
_SHARED_PROPS = {
    "name": {"type": "string"},
    "audience": {"type": ["string", "null"]},
    "angle": {"type": ["string", "null"]},
    "opener": {"type": ["string", "null"], "description": "Shared opener or call script."},
    "channel": {"type": ["string", "null"], "description": "phone, email, or text."},
    "status": {"type": "string", "enum": list(STATUSES)},
    "start_date": {"type": ["string", "null"], "description": "YYYY-MM-DD."},
    "deadline": {"type": ["string", "null"], "description": "YYYY-MM-DD."},
    "service_id": {"type": ["integer", "null"], "minimum": 1},
    "service_slug": {"type": ["string", "null"]},
    "offer_id": {"type": ["integer", "null"], "minimum": 1},
    **_TARGET_PROPS,
}

CAMPAIGN_TOOLS = [
    {
        "name": "list_campaigns",
        "description": (
            "Campaigns, with win/loss, percent complete, and pace. "
            "A deal is in a campaign when it is tagged campaign:<slug>. "
            "Win/loss excludes disqualified deals. Percent complete is 0–100. "
            "Pace is ahead, on_track, behind, or null when there is nothing to compare. "
            "Archived campaigns are left out unless status=archived or include_archived=true."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "status": {"type": "string", "enum": list(STATUSES)},
                "include_archived": {"type": "boolean"},
            },
        },
        "annotations": {"title": "List campaigns", **_READ},
        "handler": list_campaigns_tool,
    },
    {
        "name": "get_campaign",
        "description": (
            "One campaign, including results, win/loss, targets, pacing, and "
            "its deals. Each figure is {value, formula, inputs, note}; percents "
            "are 0–100. period=week|month|year adds the period breakdown. "
            "Omit period and periods is null. Same calculation as the campaign page."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "campaign_id": {"type": "integer", "minimum": 1},
                "slug": {"type": "string"},
                "period": {"type": "string", "enum": list(PERIODS)},
            },
        },
        "annotations": {"title": "Get campaign", **_READ},
        "handler": get_campaign_tool,
    },
    {
        "name": "create_campaign",
        "description": (
            "Create a campaign. slug is optional and defaults from the name. "
            "Deals already tagged campaign:<slug> are in it. Does not copy "
            "the pitch onto deals; lead-specific notes stay on the deal."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["name"],
            "properties": {
                "slug": {"type": "string"},
                **_SHARED_PROPS,
            },
        },
        "annotations": {
            "title": "Create campaign",
            **_WRITE,
            "idempotentHint": False,
        },
        "handler": create_campaign_tool,
    },
    {
        "name": "update_campaign",
        "description": (
            "Update a campaign. Pass campaign_id or slug to identify it. "
            "new_slug renames it and rewrites the campaign: tag on every linked deal. "
            "Null clears an optional field. Omitted fields stay as they are."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "campaign_id": {"type": "integer", "minimum": 1},
                "slug": {"type": "string", "description": "Current slug, used to find the campaign."},
                "new_slug": {"type": "string"},
                **_SHARED_PROPS,
            },
        },
        "annotations": {"title": "Update campaign", **_WRITE},
        "handler": update_campaign_tool,
    },
    {
        "name": "archive_campaign",
        "description": (
            "Archive a campaign (hidden from list_campaigns by default). "
            "close_untouched=true also marks its open records whose timeline is "
            "only system entries lost with the reason Campaign cancelled; records "
            "anyone worked on stay open. unarchive=true sets status back to paused. "
            "Pass campaign_id or slug."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "campaign_id": {"type": "integer", "minimum": 1},
                "slug": {"type": "string"},
                "close_untouched": {"type": "boolean"},
                "unarchive": {"type": "boolean"},
            },
        },
        "annotations": {"title": "Archive campaign", **_WRITE},
        "handler": archive_campaign_tool,
    },
]
