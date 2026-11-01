"""MCP tools for partner metric snapshots.

Recording appends a row. It never updates an older snapshot. Listing reads
that history, newest first, with optional key and date filters.
"""
from app.mcp.tools import _err, _id_arg, _ok
from app.mcp.util import as_bool, as_text, clamp_limit
from app.services.partner_metrics import (
    ERROR_MESSAGES,
    LIST_DEFAULT,
    LIST_MAX,
    MAX_BATCH,
    latest_snapshots,
    list_snapshots,
    record_snapshot,
    record_snapshots,
)
from app.services.partners import get_partner

_READ = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
_WRITE = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": False,
}

_METRIC_KEY = {
    "type": "string",
    "description": (
        "Free-form slug: lowercase letters, digits, hyphens "
        "(google-rating, google-review-count, ads-in-library). Not a fixed list."
    ),
}
_SOURCE = {
    "type": "string",
    "description": "Where the reading came from, as a slug (google-maps, website-check, ad-library).",
}
_CAPTURED_AT = {
    "type": "string",
    "description": (
        "When the reading was taken. ISO date or timestamp "
        "(2026-08-01 or 2026-08-01T15:04:00Z). Omit to use the current time. "
        "A date alone is midnight UTC."
    ),
}
_SNAPSHOT_FIELDS = {
    "metric_key": _METRIC_KEY,
    "numeric_value": {
        "type": "number",
        "description": "The number (4.8, 312). Omit only when text_value is set.",
    },
    "text_value": {
        "type": "string",
        "description": "Optional short note stored with the number. A snapshot needs a number, text, or both.",
    },
    "source": _SOURCE,
    "captured_at": _CAPTURED_AT,
}


def _fail(error, **extra):
    return _err(error, ERROR_MESSAGES.get(error, error), **extra)


def _partner_or_error(partner_id):
    if not get_partner(partner_id):
        return _fail("not_found", partner_id=partner_id)
    return None


def record_partner_metric_tool(arguments):
    partner_id, error = _id_arg(arguments, "partner_id")
    if error:
        return error
    missing = _partner_or_error(partner_id)
    if missing:
        return missing
    row, fail = record_snapshot(
        partner_id,
        as_text(arguments.get("metric_key")),
        as_text(arguments.get("source")),
        numeric_value=arguments.get("numeric_value"),
        text_value=arguments.get("text_value"),
        captured_at=arguments.get("captured_at"),
    )
    if fail:
        return _fail(fail)
    return _ok(snapshot=row)


def record_partner_metrics_tool(arguments):
    snapshots = arguments.get("snapshots")
    if not isinstance(snapshots, list) or not snapshots:
        return _fail("empty_snapshots")
    if len(snapshots) > MAX_BATCH:
        return _fail("too_many_snapshots")
    rows, fail = record_snapshots(snapshots)
    if fail:
        payload = {"index": fail.get("index")}
        if fail.get("partner_id"):
            payload["partner_id"] = fail["partner_id"]
        return _fail(fail["error"], **payload)
    return _ok(snapshots=rows, count=len(rows))


def list_partner_metrics_tool(arguments):
    partner_id, error = _id_arg(arguments, "partner_id")
    if error:
        return error
    missing = _partner_or_error(partner_id)
    if missing:
        return missing
    metric_key = None
    if "metric_key" in arguments and arguments.get("metric_key") not in (None, ""):
        metric_key = as_text(arguments.get("metric_key"))
    captured_after = arguments.get("captured_after") if "captured_after" in arguments else None
    captured_before = arguments.get("captured_before") if "captured_before" in arguments else None
    latest_only = as_bool(arguments.get("latest_only"), default=False)
    if "latest_only" in arguments and latest_only is None:
        return _err("invalid_value", "latest_only must be true or false")
    limit = clamp_limit(arguments.get("limit"), default=LIST_DEFAULT, maximum=LIST_MAX)
    latest, fail = latest_snapshots(
        partner_id,
        metric_key=metric_key,
        captured_after=captured_after,
        captured_before=captured_before,
    )
    if fail:
        return _fail(fail)
    if latest_only:
        rows = latest[:limit]
    else:
        rows, fail = list_snapshots(
            partner_id,
            metric_key=metric_key,
            captured_after=captured_after,
            captured_before=captured_before,
            limit=limit,
        )
        if fail:
            return _fail(fail)
    return _ok(
        partner_id=partner_id,
        snapshots=rows,
        latest=latest,
        count=len(rows),
        metric_key=metric_key,
    )


_SNAPSHOT_ITEM = {
    "type": "object",
    "additionalProperties": False,
    "required": ["partner_id", "metric_key", "source"],
    "properties": {
        "partner_id": {"type": "integer", "minimum": 1},
        **_SNAPSHOT_FIELDS,
    },
}


METRIC_TOOLS = [
    {
        "name": "record_partner_metric",
        "description": (
            "Append one intel snapshot on a partner. History is kept: a new "
            "reading does not overwrite an older one. metric_key is a slug you "
            "choose (google-rating, google-review-count). source is where it "
            "came from (google-maps). captured_at is when the reading was taken; "
            "omit it to use now. ICP company_metric rules read the company "
            "partner, or the parent company when the partner is a person, so "
            "record company intel on the company."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["partner_id", "metric_key", "source"],
            "properties": {
                "partner_id": {"type": "integer", "minimum": 1},
                **_SNAPSHOT_FIELDS,
            },
        },
        "annotations": {"title": "Record partner metric", **_WRITE},
        "handler": record_partner_metric_tool,
    },
    {
        "name": "record_partner_metrics",
        "description": (
            "Append many intel snapshots in one call, for one partner or several. "
            "Each item is a partner_id, metric_key, source, and a numeric_value "
            "and/or text_value, with an optional captured_at. The call is all or "
            "nothing: one invalid item writes none of them. Rows are inserted, "
            "never updated. At most 50 snapshots."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["snapshots"],
            "properties": {
                "snapshots": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_BATCH,
                    "items": _SNAPSHOT_ITEM,
                    "description": "Snapshots to append. Each keeps its own partner, key, source, and time.",
                },
            },
        },
        "annotations": {"title": "Record partner metrics", **_WRITE},
        "handler": record_partner_metrics_tool,
    },
    {
        "name": "list_partner_metrics",
        "description": (
            "List one partner's metric snapshots, newest first. Filter with "
            "metric_key and/or captured_after / captured_before (inclusive; a "
            "date alone covers that whole day). latest is the newest row of "
            "each metric key inside the filter. latest_only returns just those rows. "
            "This is the partner's own snapshots. A company-metric fit rule uses "
            "the company, or the parent company of a person."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["partner_id"],
            "properties": {
                "partner_id": {"type": "integer", "minimum": 1},
                "metric_key": _METRIC_KEY,
                "captured_after": {
                    "type": "string",
                    "description": "Inclusive start. Date or timestamp. A date is midnight UTC.",
                },
                "captured_before": {
                    "type": "string",
                    "description": "Inclusive end. A date includes the whole day.",
                },
                "latest_only": {
                    "type": "boolean",
                    "description": "Default false. True returns only the newest snapshot of each metric key.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": LIST_MAX,
                    "description": f"Default {LIST_DEFAULT}, max {LIST_MAX}. Applies to snapshots, not to latest.",
                },
            },
        },
        "annotations": {"title": "List partner metrics", **_READ},
        "handler": list_partner_metrics_tool,
    },
]
