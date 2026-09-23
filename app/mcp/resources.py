"""MCP resources and prompts — grounding catalogs plus operator playbooks."""

from app.mcp import INSTRUCTIONS
from app.mcp.serialize import offer_brief, service_brief, stage_brief
from app.mcp.tools import _CALL_OUTCOME_KEYS
from app.mcp.util import dumps
from app.services import pipeline_stages
from app.services.catalog import list_services
from app.services.deals import CALL_OUTCOMES
from app.services.offers import list_offers

RESOURCES = [
    {
        "uri": "crm://instructions",
        "name": "Bot operating rules",
        "description": "How a bot should use this CRM: search-before-create, ingest vs create, what not to do.",
        "mimeType": "text/plain",
        "reader": lambda: INSTRUCTIONS,
    },
    {
        "uri": "crm://catalog/services",
        "name": "Service catalog",
        "description": "Active services (slug is what ingest_leads needs).",
        "mimeType": "application/json",
        "reader": lambda: dumps([service_brief(s) for s in list_services(active_only=True)]),
    },
    {
        "uri": "crm://catalog/stages",
        "name": "Pipeline stages",
        "description": "Configured deal stages and role flags. deals.stage stores the key.",
        "mimeType": "application/json",
        "reader": lambda: dumps([stage_brief(s) for s in pipeline_stages.list_stages()]),
    },
    {
        "uri": "crm://catalog/offers",
        "name": "Offers",
        "description": "Pitches a bot can quote on a call (pitch, proof, price anchor).",
        "mimeType": "application/json",
        "reader": lambda: dumps([offer_brief(o) for o in list_offers(active_only=True)]),
    },
    {
        "uri": "crm://catalog/call-outcomes",
        "name": "Call outcomes",
        "description": "Valid record_call_outcome keys.",
        "mimeType": "application/json",
        "reader": lambda: dumps([
            {"key": key, "label": label} for key, label, _target in CALL_OUTCOMES
        ]),
    },
]

RESOURCE_BY_URI = {item["uri"]: item for item in RESOURCES}

PROMPTS = [
    {
        "name": "work_the_queue",
        "description": "Work today's call queue: brief the next lead and log the outcome after the call.",
        "arguments": [],
        "messages": [
            {
                "role": "user",
                "content": {
                    "type": "text",
                    "text": (
                        "Work today's CRM call queue. "
                        "1) get_call_queue. 2) For the top deal, get_deal and brief me: who, offer, "
                        "pain/goals, ICP, last activities, what to say. "
                        "3) After I report the call, record_call_outcome with a short note. "
                        "Do not invent a number or an outcome."
                    ),
                },
            }
        ],
    },
    {
        "name": "qualify_lead",
        "description": "Pull everything needed to qualify one deal before outreach.",
        "arguments": [
            {
                "name": "deal_id",
                "description": "Deal id to qualify",
                "required": True,
            }
        ],
        "messages": [
            {
                "role": "user",
                "content": {
                    "type": "text",
                    "text": (
                        "Qualify deal {{deal_id}}. get_deal, then list_catalog if you need stage/offer names. "
                        "Summarize ICP fit, missing contact fields, suggested next_action, "
                        "and whether they belong in the call queue. Do not change stage unless I ask."
                    ),
                },
            }
        ],
    },
    {
        "name": "log_a_call",
        "description": "Log a call that already happened against a deal.",
        "arguments": [
            {
                "name": "deal_id",
                "description": "Deal id the call was about",
                "required": True,
            },
            {
                "name": "outcome",
                "description": "One of: " + ", ".join(_CALL_OUTCOME_KEYS),
                "required": True,
            },
            {
                "name": "note",
                "description": "What was said / next step",
                "required": False,
            },
        ],
        "messages": [
            {
                "role": "user",
                "content": {
                    "type": "text",
                    "text": (
                        "A call already happened on deal {{deal_id}}. "
                        "Record outcome {{outcome}} with note: {{note}}. "
                        "If the deal is missing, search_partners instead of guessing an id."
                    ),
                },
            }
        ],
    },
]

PROMPT_BY_NAME = {item["name"]: item for item in PROMPTS}


def list_resource_defs():
    return [
        {
            "uri": item["uri"],
            "name": item["name"],
            "description": item["description"],
            "mimeType": item["mimeType"],
        }
        for item in RESOURCES
    ]


def read_resource(uri: str):
    item = RESOURCE_BY_URI.get(uri)
    if not item:
        return None
    text = item["reader"]()
    return {
        "contents": [
            {
                "uri": item["uri"],
                "mimeType": item["mimeType"],
                "text": text if isinstance(text, str) else str(text),
            }
        ]
    }


def list_prompt_defs():
    return [
        {
            "name": item["name"],
            "description": item["description"],
            "arguments": item["arguments"],
        }
        for item in PROMPTS
    ]


def get_prompt(name: str, arguments=None):
    item = PROMPT_BY_NAME.get(name)
    if not item:
        return None
    arguments = arguments or {}
    messages = []
    for message in item["messages"]:
        text = message["content"]["text"]
        for key, value in arguments.items():
            text = text.replace("{{" + str(key) + "}}", str(value or ""))
        # Drop leftover placeholders rather than send "{{note}}" to the bot.
        for arg in item["arguments"]:
            text = text.replace("{{" + arg["name"] + "}}", "")
        messages.append({
            "role": message["role"],
            "content": {"type": "text", "text": text},
        })
    return {"description": item["description"], "messages": messages}
