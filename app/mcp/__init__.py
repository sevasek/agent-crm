"""Bot-facing MCP server for this CRM.

Agents (Grok, Cursor, any MCP client) talk JSON-RPC over Streamable HTTP at POST /mcp.
This is the agent-layer interface: tools with side-effect annotations,
search-before-write guidance, and compact structured results. AI judgment
stays out of the admin UI — see docs/SCOPE.md.
"""

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = (
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)
SERVER_NAME = "crm"
SERVER_VERSION = "1.0.0"

# Sent on initialize. Bots read this once; keep it short and imperative.
INSTRUCTIONS = (
    "This is a leads/pipeline CRM for a single operator. "
    "Partners are people or companies; deals are pursuits of a service; "
    "activities are the timeline. Offers are priced packages attached to deals. "
    "Search before creating — never invent ids. "
    "New inbound leads: ingest_leads (idempotent on partner+service; email optional). "
    "Do not pair create_partner+create_deal for the same inbound lead. "
    "Log human work with log_activity or record_call_outcome so the timeline "
    "is the system of record. "
    "Read list_catalog before set_deal_stage; after a live call prefer "
    "record_call_outcome. "
    "Follow-on deals link to their parent with parent_deal_id (same partner); "
    "get_deal returns child_deals. "
    "Campaign calling: get_call_queue with include_new=true or "
    "stage=new and service_slug. Label a campaign with deal tags "
    "(list_tags; list_deals tags= matches every tag). ingest_leads merges tags; "
    "update_deal tags replaces, add_tags/remove_tags merge; "
    "bulk_update_deals add_tags/remove_tags covers a list of deal_ids. "
    "Assign work with set_deal_owner (e.g. alice, bob). "
    "Promote a list with bulk_update_deals rather than one round-trip each. "
    "Hand work to another agent with create_delegated_task (owner defaults to "
    "DELEGATE_DEFAULT_OWNER, 'agent'); that agent polls "
    "list_delegated_tasks(owner=agent, status=delegated) and "
    "complete_delegated_task writes the deal timeline. "
    "update_partner defaults to fill-empty so a re-run cannot clobber operator "
    "edits; set fill_empty_only=false only to correct a wrong value. "
    "You cannot delete records or reshape the pipeline. "
    "Add a missing catalog service with create_service (idempotent on slug). "
    "Change or deactivate a service with update_service. "
    "Debug task webhook delivery with get_delegated_task "
    "(notified_at, last_attempt_at, last_error). "
    "If a tool returns ok=false, fix the arguments or search again — do not guess."
)
