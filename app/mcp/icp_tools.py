"""MCP wrappers for ICP fit criteria. Same rules as /definitions/icp."""
from app.mcp.tools import _err, _id_arg, _ok
from app.mcp.util import as_bool, as_int, as_text
from app.services.icp import (
    ERROR_MESSAGES,
    MATCHABLE_FIELDS,
    OPERATORS,
    TAG_FIELDS,
    create_criterion,
    delete_criterion,
    list_criteria,
    update_criterion,
)

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

_FIELD_HELP = (
    "One of the list_catalog icp_fields keys. "
    "Tag rules: company_tag (the linked company, or the parent company of a person) "
    "or deal_tag (the lead or deal itself), with operator has_tag and the tag slug "
    "(google-ads, campaign:spring)."
)


def _brief(row):
    if not row:
        return None
    return {
        "id": row["id"],
        "label": row.get("label"),
        "field": row["field"],
        "operator": row["operator"],
        "value": row.get("value"),
        "weight": row["weight"],
        "active": bool(row.get("active")),
    }


def _fail(error):
    return _err(error, ERROR_MESSAGES.get(error, error))


def _catalog():
    return {
        "fields": [{"key": key, "label": label} for key, label in MATCHABLE_FIELDS.items()],
        "operators": [{"key": key, "label": label} for key, label in OPERATORS.items()],
        "tag_fields": sorted(TAG_FIELDS),
    }


def _weight(arguments, *, default):
    if "weight" not in arguments or arguments.get("weight") in (None, ""):
        if default is None:
            return None, _fail("invalid_weight")
        return default, None
    parsed = as_int(arguments.get("weight"))
    if parsed is None:
        return None, _fail("invalid_weight")
    return parsed, None


def _text_value(arguments):
    value = arguments.get("value")
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def list_icp_criteria_tool(arguments):
    active_only = as_bool(arguments.get("active_only"), default=False)
    rows = list_criteria(active_only=bool(active_only))
    return _ok(criteria=[_brief(row) for row in rows], count=len(rows), **_catalog())


def create_icp_criterion_tool(arguments):
    field = as_text(arguments.get("field")).strip()
    operator = as_text(arguments.get("operator")).strip()
    if not field:
        return _fail("invalid_field")
    if not operator:
        return _fail("invalid_operator")
    weight, error = _weight(arguments, default=1)
    if error:
        return error
    active = as_bool(arguments.get("active"), default=True)
    label = as_text(arguments.get("label")) if "label" in arguments else ""
    row, fail = create_criterion(
        field, operator, _text_value(arguments),
        weight=weight, label=label, active=bool(active),
    )
    if fail:
        return _fail(fail)
    return _ok(criterion=_brief(row))


def update_icp_criterion_tool(arguments):
    criterion_id, error = _id_arg(arguments, "criterion_id")
    if error:
        return error
    fields = {}
    if "field" in arguments:
        fields["field"] = as_text(arguments.get("field")).strip()
    if "operator" in arguments:
        fields["operator"] = as_text(arguments.get("operator")).strip()
    if "value" in arguments:
        fields["value"] = _text_value(arguments)
    if "label" in arguments:
        fields["label"] = as_text(arguments.get("label"))
    if "weight" in arguments:
        weight, error = _weight(arguments, default=None)
        if error:
            return error
        fields["weight"] = weight
    if "active" in arguments:
        active = as_bool(arguments.get("active"), default=None)
        if active is None:
            return _fail("invalid_value")
        fields["active"] = active
    row, fail = update_criterion(criterion_id, **fields)
    if fail:
        return _fail(fail)
    return _ok(criterion=_brief(row))


def delete_icp_criterion_tool(arguments):
    criterion_id, error = _id_arg(arguments, "criterion_id")
    if error:
        return error
    _ok_flag, fail = delete_criterion(criterion_id)
    if fail:
        return _fail(fail)
    return _ok(deleted=True, criterion_id=criterion_id)


ICP_TOOLS = [
    {
        "name": "list_icp_criteria",
        "description": (
            "ICP fit rules. A lead or deal's fit score is the sum of weight over "
            "every active rule that matches. Includes inactive rules unless "
            "active_only is true. fields and operators are the allowlists. "
            "Tag rules use field company_tag or deal_tag and operator has_tag."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "active_only": {
                    "type": "boolean",
                    "description": "Default false. True returns only rules that score.",
                },
            },
        },
        "annotations": {"title": "List ICP criteria", **_READ},
        "handler": list_icp_criteria_tool,
    },
    {
        "name": "create_icp_criterion",
        "description": (
            "Add one ICP fit rule. weight is the points added when it matches. "
            "For a tag, field is company_tag (linked company, or the parent company "
            "when the partner is a person) or deal_tag (the lead or deal itself), "
            "operator is has_tag, and value is one tag slug such as google-ads. "
            "One rule per tag; the score sums them with every other matching rule. "
            "Profile fields keep their existing operators (is_not_null, boolean, "
            "fuzzy_match, eq, gt, gte, lt, lte)."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["field", "operator"],
            "properties": {
                "field": {"type": "string", "description": _FIELD_HELP},
                "operator": {
                    "type": "string",
                    "description": "has_tag for company_tag and deal_tag. See list_icp_criteria operators.",
                },
                "value": {
                    "type": "string",
                    "description": (
                        "Tag slug for has_tag (google-ads, campaign:spring). "
                        "true or false for boolean. A number for eq/gt/gte/lt/lte. "
                        "Text for fuzzy_match. Ignored for is_not_null."
                    ),
                },
                "weight": {"type": "integer", "description": "Points added on a match. Default 1."},
                "label": {"type": "string", "description": "Name shown beside the fit score."},
                "active": {"type": "boolean", "description": "Default true. False keeps the rule but does not score it."},
            },
        },
        "annotations": {"title": "Create ICP criterion", **_WRITE, "idempotentHint": False},
        "handler": create_icp_criterion_tool,
    },
    {
        "name": "update_icp_criterion",
        "description": (
            "Change an ICP fit rule. Omitted fields stay as they are. "
            "Tag rules still require field company_tag or deal_tag with operator has_tag "
            "and a tag slug. Weight works the same as any other rule."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["criterion_id"],
            "properties": {
                "criterion_id": {"type": "integer", "minimum": 1},
                "field": {"type": "string", "description": _FIELD_HELP},
                "operator": {"type": "string"},
                "value": {"type": "string"},
                "weight": {"type": "integer"},
                "label": {"type": "string"},
                "active": {"type": "boolean"},
            },
        },
        "annotations": {"title": "Update ICP criterion", **_WRITE},
        "handler": update_icp_criterion_tool,
    },
    {
        "name": "delete_icp_criterion",
        "description": "Delete one ICP fit rule. Fit scores stop including it.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["criterion_id"],
            "properties": {
                "criterion_id": {"type": "integer", "minimum": 1},
            },
        },
        "annotations": {
            "title": "Delete ICP criterion",
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "handler": delete_icp_criterion_tool,
    },
]
