"""MCP names and fields the live sevasek bots already call.

The live tool list was read from the crm.sevasek.com MCP connection on
2026-10-07 (27 tools). agent-crm adds list_tags and the stage-automation
tools. Live names and their argument names stay. Extra properties are
allowed. A missing live property is not.
"""
from app.mcp.tools import TOOLS
from app.services.leads import ALLOWED_LEAD_KEYS

# input property names on the live server. required flags are noted only
# where the live schema required them.
LIVE_TOOL_PROPS = {
    "bulk_update_deals": ["deal_ids", "next_action", "next_action_date", "owner", "owner_key", "service_slug", "set_stage", "stage"],
    "complete_delegated_task": ["result_notes", "task_id"],
    "create_deal": ["external_ref", "goals", "next_action", "next_action_date", "offer_id", "owner", "owner_key", "pain_points", "parent_deal_id", "partner_id", "service_id", "service_slug", "source", "stage", "value_estimate"],
    "create_delegated_task": ["brief", "created_by", "deal_id", "due_date", "owner", "owner_key", "status", "title"],
    "create_offer": ["active", "currency", "description", "is_default", "name", "pitch", "price", "price_anchor", "proof_point", "service_id", "service_slug"],
    "create_partner": ["address", "email", "facebook_url", "industry", "instagram_url", "is_company", "linkedin_url", "name", "owner_key", "parent_id", "phone", "preferred_channel", "social_url", "team_size", "title", "website", "x_url", "youtube_url"],
    "create_service": ["description", "name", "nurture_list_slug", "slug"],
    "get_call_queue": ["include_new", "limit", "owner", "owner_key", "service_slug", "source_prefix", "stage"],
    "get_deal": ["activity_limit", "deal_id"],
    "get_delegated_task": ["task_id"],
    "get_partner": ["activity_limit", "partner_id"],
    "ingest_leads": ["leads"],
    "list_activities": ["deal_id", "limit", "partner_id"],
    "list_catalog": [],
    "list_deals": ["due_only", "limit", "owner", "owner_key", "parent_deal_id", "partner_id", "service_slug", "stage"],
    "list_delegated_tasks": ["deal_id", "due_only", "limit", "owner", "owner_key", "status"],
    "list_due_followups": ["limit", "owner", "owner_key", "service_slug", "stage"],
    "log_activity": ["body", "deal_id", "partner_id", "type"],
    "record_call_outcome": ["deal_id", "note", "outcome"],
    "search_partners": ["limit", "query"],
    "set_deal_owner": ["deal_id", "owner", "owner_key"],
    "set_deal_stage": ["deal_id", "stage"],
    "update_deal": ["deal_id", "external_ref", "goals", "next_action", "next_action_date", "offer_id", "owner", "owner_key", "pain_points", "parent_deal_id", "source", "value_estimate"],
    "update_delegated_task": ["brief", "due_date", "owner", "owner_key", "result_notes", "status", "task_id", "title"],
    "update_offer": ["active", "currency", "description", "is_default", "name", "offer_id", "pitch", "price", "price_anchor", "proof_point", "service_id", "service_slug"],
    "update_partner": ["address", "email", "facebook_url", "fill_empty_only", "industry", "instagram_url", "is_company", "linkedin_url", "name", "owner_key", "parent_id", "partner_id", "phone", "preferred_channel", "social_url", "team_size", "title", "website", "x_url", "youtube_url"],
    "update_service": ["active", "description", "name", "nurture_list_slug", "service_id", "slug"],
}

LIVE_INGEST_LEAD_FIELDS = {
    "name", "email", "company_name", "service_slug", "phone", "title",
    "website", "address", "social_url", "linkedin_url", "x_url", "instagram_url",
    "facebook_url", "youtube_url", "preferred_channel", "industry", "team_size",
    "source", "value_estimate", "pain_points", "goals", "next_action",
    "next_action_date", "is_company", "owner_key", "offer_id", "external_ref",
}


STAGE_AUTOMATION_TOOLS = {
    "list_stage_automations",
    "create_stage_automation",
    "update_stage_automation",
    "delete_stage_automation",
}


def test_tool_list_keeps_live_tools_and_adds_stage_automations():
    names = [tool["name"] for tool in TOOLS]
    assert len(names) == len(set(names))
    assert "list_tags" in names
    # 27 live tools, plus list_tags, plus stage automations. Live argument
    # names are pinned below and are not renamed.
    # Live tools stay. New lead tools are additive.
    assert set(LIVE_TOOL_PROPS) | {"list_tags"} | STAGE_AUTOMATION_TOOLS <= set(names)
    assert len(names) >= 32


def test_live_tool_arguments_are_accepted():
    by_name = {tool["name"]: tool for tool in TOOLS}
    for name, props in LIVE_TOOL_PROPS.items():
        schema_props = set(by_name[name]["inputSchema"]["properties"])
        missing = [prop for prop in props if prop not in schema_props]
        assert missing == [], f"{name} is missing {missing}"


def test_ingest_accepts_every_live_lead_field():
    assert LIVE_INGEST_LEAD_FIELDS <= set(ALLOWED_LEAD_KEYS)


def test_list_catalog_includes_delegated_task_owners_when_configured(client, db, monkeypatch):
    from tests.test_mcp_tools import _enable, call

    _enable(monkeypatch)
    payload, is_error = call(client, "list_catalog")
    assert is_error is False
    assert "delegated_task_owners" not in payload
    assert payload["delegated_task_statuses"] == ["proposed", "delegated", "done", "cancelled"]

    monkeypatch.setenv(
        "DELEGATED_TASK_OWNERS",
        "paul, bethany, willow, joe, unassigned",
    )
    payload, _is_error = call(client, "list_catalog")
    assert payload["delegated_task_owners"] == [
        "paul", "bethany", "willow", "joe", "unassigned",
    ]
