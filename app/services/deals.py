from datetime import datetime

from app.database import get_db
from app.services.auth import clean_owner_key, sanitize_text
from app.services.deal_tags import (
    apply_deal_tags, attach_tags, coerce_tag_filter, parse_tag_list, tags_for_deal,
)
from app.services.activities import log_activity
from app.services.catalog import get_service
from app.services.partners import get_partner
from app.services.nurture import enroll_partner_in_nurture
from app.services import pipeline_stages
from app.services.offers import default_offer_id, get_offer

# Pipeline stages themselves (new -> contacted -> qualified -> ... -> won/lost)
# are configurable, not hardcoded — see app.services.pipeline_stages. What
# used to switch on stage *names* here now switches on stage *roles*
# (is_qualified_pool, triggers_nurture, is_won, is_lost), so renaming or
# restructuring the pipeline through /admin/stages or /api/v1/stages doesn't
# require touching this file.

# One-tap outcomes for a live call, offered on the "Today's calls" queue.
# Target is resolved at call time (stages can be renamed/reordered/deleted
# after this list is defined):
#   None            -> leave the stage as-is ("no answer": the deal stays in
#                      its qualified-pool stage, so it remains in today's
#                      queue at the same rank/score for a same-day retry).
#   {"role": r}      -> the first configured stage with that role flag set
#                      (won/lost/nurture-trigger), so renaming those stages
#                      or moving the role elsewhere just works.
#   {"key": k}       -> a literal stage key, for outcomes with no dedicated
#                      role (there's no "meeting" role). If that stage no
#                      longer exists, the stage is left unchanged but the
#                      logged activity says so — fails loud, not silently.
CALL_OUTCOMES = [
    ("no_answer", "No answer", None),
    ("interested", "Interested (warm lead)", {"role": "triggers_nurture"}),
    ("meeting_scheduled", "Meeting scheduled", {"key": "proposal"}),
    ("won", "Won — new customer", {"role": "is_won"}),
    ("not_interested", "Not interested", {"role": "is_lost"}),
]
_CALL_OUTCOME_MAP = {key: (label, target) for key, label, target in CALL_OUTCOMES}

# Shared with call_queue recency lookup. Changing this format without updating
# both writer and LIKE pattern would silently zero every recency score.
STAGE_CHANGED_PREFIX = "Stage changed: "
STAGE_CHANGED_SEP = " -> "


def stage_changed_body(old_stage: str, new_stage: str) -> str:
    """Activity body written by set_deal_stage."""
    return f"{STAGE_CHANGED_PREFIX}{old_stage}{STAGE_CHANGED_SEP}{new_stage}"


def stage_changed_to_like(new_stage: str) -> str:
    """SQL LIKE that matches stage_changed_body(..., new_stage)."""
    return f"{STAGE_CHANGED_PREFIX}%{STAGE_CHANGED_SEP}{new_stage}"


def _resolve_call_outcome_target(target):
    if not target:
        return None
    if "key" in target:
        return target["key"] if pipeline_stages.get_stage(target["key"]) else None
    role = target["role"]
    for stage in pipeline_stages.list_stages():
        if stage[role]:
            return stage["key"]
    return None


def create_deal(partner_id: int, service_id: int, source: str = "", value_estimate=None,
                 next_action: str = "", next_action_date: str = "", pain_points: str = "", goals: str = "",
                 created_note: str = "Deal created", stage: str = None, offer_id: int = None,
                 owner_key: str = "", external_ref: str = "", parent_deal_id=None,
                 attach_default_offer: bool = True, tags=None):
    if not stage or not pipeline_stages.get_stage(stage):
        stage = pipeline_stages.default_stage_key()
    if offer_id and get_offer(offer_id):
        pass
    elif attach_default_offer:
        offer_id = default_offer_id()
    else:
        offer_id = None
    cleaned_parent, _parent_error = validate_parent_link(None, parent_deal_id, partner_id)
    with get_db() as db:
        db.execute("""
            INSERT INTO deals (
                partner_id, service_id, stage, source, value_estimate,
                pain_points, goals, next_action, next_action_date, offer_id,
                owner_key, external_ref, parent_deal_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            partner_id, service_id, stage,
            sanitize_text(source, max_len=120) or None,
            value_estimate if value_estimate is not None else None,
            sanitize_text(pain_points, max_len=2000, allow_newlines=True) or None,
            sanitize_text(goals, max_len=2000, allow_newlines=True) or None,
            sanitize_text(next_action, max_len=500) or None,
            next_action_date or None,
            offer_id,
            clean_owner_key(owner_key),
            sanitize_text(external_ref, max_len=200) or None,
            cleaned_parent,
        ))
        db.commit()
        deal_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    cleaned_tags, _tag_error = parse_tag_list(tags) if tags else (None, None)
    if cleaned_tags:
        apply_deal_tags(deal_id, replace=cleaned_tags)
    log_activity(partner_id, "system", created_note, deal_id=deal_id)
    return deal_id


def get_deal(deal_id: int):
    with get_db() as db:
        row = db.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
        if not row:
            return None
        deal = dict(row)
    deal["tags"] = tags_for_deal(deal_id)
    return deal


def get_open_deal_for_partner_service(partner_id: int, service_id: int):
    """Most recent non-closed deal this partner already has for this service, if any —
    used by the lead-ingestion API to avoid piling up duplicate deals when an ETL job
    re-submits the same lead on a later run."""
    closed = pipeline_stages.closed_stage_keys()
    with get_db() as db:
        if closed:
            placeholders = ",".join("?" * len(closed))
            query = f"""SELECT * FROM deals
                WHERE partner_id = ? AND service_id = ? AND stage NOT IN ({placeholders})
                ORDER BY created_at DESC LIMIT 1"""
            params = (partner_id, service_id, *closed)
        else:
            query = """SELECT * FROM deals
                WHERE partner_id = ? AND service_id = ?
                ORDER BY created_at DESC LIMIT 1"""
            params = (partner_id, service_id)
        row = db.execute(query, params).fetchone()
        if not row:
            return None
        deal = dict(row)
    deal["tags"] = tags_for_deal(deal["id"])
    return deal


def list_deals(stage: str = None, partner_id: int = None, owner_key: str = None,
               service_id: int = None, service_slug: str = None, deal_ids=None,
               parent_deal_id: int = None, tags=None):
    wanted_tags, impossible = coerce_tag_filter(tags)
    if impossible:
        return []
    query = """
        SELECT deals.*, partners.name AS partner_name, services.name AS service_name,
               services.slug AS service_slug
        FROM deals
        JOIN partners ON partners.id = deals.partner_id
        JOIN services ON services.id = deals.service_id
        WHERE 1=1
    """
    params = []
    for tag in wanted_tags:
        query += (
            " AND EXISTS (SELECT 1 FROM deal_tags"
            " WHERE deal_tags.deal_id = deals.id AND deal_tags.tag = ?)"
        )
        params.append(tag)
    if stage:
        query += " AND deals.stage = ?"
        params.append(stage)
    if partner_id:
        query += " AND deals.partner_id = ?"
        params.append(partner_id)
    owner = clean_owner_key(owner_key)
    if owner:
        query += " AND deals.owner_key = ?"
        params.append(owner)
    if service_id:
        query += " AND deals.service_id = ?"
        params.append(service_id)
    slug = (service_slug or "").strip().lower()
    if slug:
        query += " AND services.slug = ?"
        params.append(slug)
    if parent_deal_id:
        query += " AND deals.parent_deal_id = ?"
        params.append(parent_deal_id)
    if deal_ids:
        ids = [int(i) for i in deal_ids if i]
        if ids:
            placeholders = ",".join("?" * len(ids))
            query += f" AND deals.id IN ({placeholders})"
            params.extend(ids)
        else:
            return []
    query += " ORDER BY deals.updated_at DESC"
    with get_db() as db:
        rows = db.execute(query, params).fetchall()
        deals = [dict(r) for r in rows]
    return attach_tags(deals)


def validate_parent_link(deal_id, parent_deal_id, partner_id):
    """Return (cleaned_parent_id, error_code). None/empty unlinks.

    Rejects self-parent, missing parent, partner mismatch, and cycles.
    """
    if parent_deal_id in (None, "", 0, "0"):
        return None, None
    try:
        parent_id = int(parent_deal_id)
    except (TypeError, ValueError):
        return None, "invalid_parent"
    if parent_id < 1:
        return None, "invalid_parent"
    if deal_id and parent_id == int(deal_id):
        return None, "parent_cycle"
    parent = get_deal(parent_id)
    if not parent:
        return None, "parent_not_found"
    if partner_id and parent["partner_id"] != partner_id:
        return None, "parent_partner_mismatch"
    seen = {int(deal_id)} if deal_id else set()
    current = parent
    while current:
        cid = current["id"]
        if cid in seen:
            return None, "parent_cycle"
        seen.add(cid)
        next_parent = current.get("parent_deal_id")
        if not next_parent:
            break
        current = get_deal(next_parent)
        if not current:
            break
    return parent_id, None


def update_deal_fields(deal_id: int, **fields):
    allowed = {
        "source", "value_estimate", "pain_points", "goals", "next_action", "next_action_date",
        "offer_id", "owner_key", "external_ref", "parent_deal_id",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if "source" in updates:
        updates["source"] = sanitize_text(updates["source"], max_len=120) or None
    if "pain_points" in updates:
        updates["pain_points"] = sanitize_text(updates["pain_points"], max_len=2000, allow_newlines=True) or None
    if "goals" in updates:
        updates["goals"] = sanitize_text(updates["goals"], max_len=2000, allow_newlines=True) or None
    if "next_action" in updates:
        updates["next_action"] = sanitize_text(updates["next_action"], max_len=500) or None
    if "external_ref" in updates:
        updates["external_ref"] = sanitize_text(updates["external_ref"], max_len=200) or None
    if "owner_key" in updates:
        updates["owner_key"] = clean_owner_key(updates["owner_key"])
    if "offer_id" in updates:
        updates["offer_id"] = updates["offer_id"] if get_offer(updates["offer_id"]) else None
    if "parent_deal_id" in updates:
        deal = get_deal(deal_id)
        if not deal:
            return False
        cleaned, error = validate_parent_link(deal_id, updates["parent_deal_id"], deal["partner_id"])
        if error:
            return False
        updates["parent_deal_id"] = cleaned
    if not updates:
        return False
    updates["updated_at"] = datetime.utcnow().isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [deal_id]
    with get_db() as db:
        cursor = db.execute(f"UPDATE deals SET {set_clause} WHERE id = ?", values)
        db.commit()
        return cursor.rowcount > 0


def set_deal_stage(deal_id: int, new_stage: str) -> bool:
    """Move a deal to a new stage. Fires nurture enrollment when the target
    stage has `triggers_nurture` set (not on every save), sets `closed_at`
    when it has `is_won` or `is_lost` set, and logs the transition."""
    stage_meta = pipeline_stages.get_stage(new_stage)
    if not stage_meta:
        return False

    deal = get_deal(deal_id)
    if not deal:
        return False
    old_stage = deal["stage"]
    if old_stage == new_stage:
        return True

    now = datetime.utcnow().isoformat()
    closed_at = now if (stage_meta["is_won"] or stage_meta["is_lost"]) else None
    with get_db() as db:
        db.execute(
            "UPDATE deals SET stage = ?, updated_at = ?, closed_at = ? WHERE id = ?",
            (new_stage, now, closed_at, deal_id),
        )
        db.commit()

    log_activity(deal["partner_id"], "system", stage_changed_body(old_stage, new_stage), deal_id=deal_id)

    if stage_meta["triggers_nurture"]:
        partner = get_partner(deal["partner_id"])
        service = get_service(deal["service_id"])
        success, message = enroll_partner_in_nurture(partner, service, deal_id=deal_id)
        log_activity(
            deal["partner_id"],
            "system",
            f"Nurture enrollment {'succeeded' if success else 'failed'}: {message}",
            deal_id=deal_id,
        )

    return True


def record_call_outcome(deal_id: int, outcome: str, note: str = "") -> bool:
    """Log the result of a live call and move the deal's stage accordingly.

    One tap from the call queue: logs a `call` activity (with an optional
    note as light-touch qualification detail) and, per CALL_OUTCOMES, moves
    the deal to the matching stage (resolved dynamically — see
    _resolve_call_outcome_target). "No answer" is the exception — it leaves
    the stage unchanged, so it remains in today's queue (same rank/score)
    for a same-day retry rather than dropping off until tomorrow.
    """
    if outcome not in _CALL_OUTCOME_MAP:
        return False
    deal = get_deal(deal_id)
    if not deal:
        return False

    label, target = _CALL_OUTCOME_MAP[outcome]
    target_stage = _resolve_call_outcome_target(target)

    body = f"Call outcome: {label}"
    if target and not target_stage and "key" in target:
        # The literal stage this outcome targets (e.g. "proposal" for
        # meeting_scheduled — there's no dedicated role for it) was renamed
        # or deleted. Fail loud in the timeline rather than silently
        # no-opping the stage move.
        body += f" (configured target stage '{target['key']}' no longer exists — stage left unchanged)"
    note = sanitize_text(note, max_len=1000, allow_newlines=True)
    if note:
        body += f" — {note}"
    log_activity(deal["partner_id"], "call", body, deal_id=deal_id)

    if target_stage and target_stage != deal["stage"]:
        set_deal_stage(deal_id, target_stage)

    return True


def child_deal_summary(deal: dict) -> dict:
    """Compact child row for get_deal / set_deal_stage."""
    if not deal:
        return None
    service = get_service(deal.get("service_id")) if deal.get("service_id") else None
    offer = get_offer(deal.get("offer_id"))
    return {
        "id": deal.get("id"),
        "service_slug": (service or {}).get("slug") or deal.get("service_slug"),
        "service_name": (service or {}).get("name") or deal.get("service_name"),
        "stage": deal.get("stage"),
        "offer": (
            {
                "id": offer.get("id"),
                "name": offer.get("name"),
                "price": offer.get("price"),
                "price_anchor": offer.get("price_anchor"),
            }
            if offer
            else None
        ),
        "parent_deal_id": deal.get("parent_deal_id"),
        "source": deal.get("source"),
        "next_action": deal.get("next_action"),
    }
