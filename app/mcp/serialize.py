"""Compact partner/deal/activity dicts for tool results — skip noise, keep ids."""

from app.mcp.util import json_safe
from app.services.partners import SOCIAL_FIELDS, listed_social_links, primary_social_url
from app.services.staleness import activity_display_body, annotate_deal


_PARTNER_FIELDS = (
    "id", "is_company", "parent_id", "name", "email", "phone", "website",
    "title", "address", "preferred_channel", "industry", "team_size",
    "owner_key",
    *SOCIAL_FIELDS, "created_at", "updated_at",
)

_DEAL_FIELDS = (
    "id", "partner_id", "service_id", "stage", "source", "value_estimate",
    "pain_points", "goals", "next_action", "next_action_date", "offer_id",
    "owner_key", "external_ref", "parent_deal_id",
    "created_at", "updated_at", "closed_at",
    "partner_name", "service_name", "service_slug",
    "tags",
)


def _pick(row, fields):
    if not row:
        return None
    out = {}
    for field in fields:
        if field in row:
            out[field] = row[field]
    if "is_company" in out:
        out["is_company"] = bool(out["is_company"])
    return json_safe(out)


def partner_brief(partner):
    if not partner:
        return None
    out = _pick(partner, _PARTNER_FIELDS)
    out["social_url"] = primary_social_url(partner)
    out["social_links"] = [
        {"label": label, "url": url} for label, url in listed_social_links(partner)
    ]
    return out


def deal_brief(deal):
    if not deal:
        return None
    annotated = annotate_deal(deal)
    out = _pick(annotated, _DEAL_FIELDS)
    out["tags"] = list(annotated.get("tags") or [])
    out["due_status"] = annotated.get("due_status")
    out["days_overdue"] = annotated.get("days_overdue")
    if annotated.get("action_date"):
        out["action_date"] = annotated["action_date"].isoformat()
    return out


def activity_brief(activity):
    if not activity:
        return None
    return json_safe({
        "id": activity.get("id"),
        "partner_id": activity.get("partner_id"),
        "deal_id": activity.get("deal_id"),
        "type": activity.get("type"),
        "body": activity_display_body(activity.get("body") or ""),
        "occurred_at": activity.get("occurred_at"),
    })


def stage_brief(stage):
    if not stage:
        return None
    return json_safe({
        "key": stage.get("key"),
        "label": stage.get("label"),
        "position": stage.get("position"),
        "is_default": bool(stage.get("is_default")),
        "is_qualified_pool": bool(stage.get("is_qualified_pool")),
        "triggers_nurture": bool(stage.get("triggers_nurture")),
        "is_won": bool(stage.get("is_won")),
        "is_lost": bool(stage.get("is_lost")),
    })


def service_brief(service):
    if not service:
        return None
    return json_safe({
        "id": service.get("id"),
        "name": service.get("name"),
        "slug": service.get("slug"),
        "description": service.get("description"),
        "nurture_list_slug": service.get("nurture_list_slug"),
        "active": bool(service.get("active")),
    })


def offer_brief(offer):
    if not offer:
        return None
    return json_safe({
        "id": offer.get("id"),
        "name": offer.get("name"),
        "service_id": offer.get("service_id"),
        "price": offer.get("price"),
        "currency": offer.get("currency") or "USD",
        "description": offer.get("description"),
        "pitch": offer.get("pitch"),
        "proof_point": offer.get("proof_point"),
        "price_anchor": offer.get("price_anchor"),
        "is_default": bool(offer.get("is_default")),
        "active": bool(offer.get("active")),
    })


def icp_brief(fit):
    if not fit:
        return None
    return json_safe({
        "score": fit.get("score"),
        "max": fit.get("max"),
        "matched": [c.get("label") or c.get("field") for c in (fit.get("matched") or [])],
        "unmatched": [c.get("label") or c.get("field") for c in (fit.get("unmatched") or [])],
    })


def delegated_task_brief(task):
    if not task:
        return None
    notified_at = task.get("webhook_notified_at") or None
    delivery = {
        "notified": bool(notified_at),
        "notified_at": notified_at,
        "last_attempt_at": task.get("webhook_last_attempt_at") or None,
        "last_error": task.get("webhook_last_error") or None,
    }
    live = task.get("webhook")
    if isinstance(live, dict) and live.get("reason"):
        delivery["reason"] = live.get("reason")
    return json_safe({
        "id": task.get("id"),
        "deal_id": task.get("deal_id"),
        "partner_id": task.get("partner_id"),
        "title": task.get("title"),
        "brief": task.get("brief"),
        "owner": task.get("owner"),
        "status": task.get("status"),
        "due_date": task.get("due_date"),
        "created_by": task.get("created_by"),
        "result_notes": task.get("result_notes"),
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
        "webhook": delivery,
    })
