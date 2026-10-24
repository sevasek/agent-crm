import logging
from datetime import datetime, timezone

from app.database import get_db
from app.services.auth import clean_owner_key, sanitize_text
from app.services.deal_tags import (
    apply_deal_tags, attach_tags, coerce_tag_filter, parse_tag_list, tags_for_deal,
)
from app.services.activities import log_activity
from app.services.catalog import get_service
from app.services.partners import get_partner
from app.services.nurture import SENT as NURTURE_SENT, SKIPPED as NURTURE_SKIPPED, enroll_partner_in_nurture
from app.services.won_webhook import SENT as WON_SENT, SKIPPED as WON_SKIPPED, notify_deal_won
from app.services import pipeline_stages
from app.services.offers import default_offer_id, get_offer

logger = logging.getLogger(__name__)

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
    ("interested", "Warm", {"role": "triggers_nurture"}),
    ("meeting_scheduled", "Meeting Scheduled", {"key": "proposal"}),
    ("won", "Won", {"role": "is_won"}),
    ("not_interested", "Lost", {"role": "is_lost"}),
]
_CALL_OUTCOME_MAP = {key: (label, target) for key, label, target in CALL_OUTCOMES}

# Shared with call_queue recency lookup. Changing this format without updating
# both writer and LIKE pattern would silently zero every recency score.
STAGE_CHANGED_PREFIX = "Stage changed: "
STAGE_CHANGED_SEP = " -> "

# Same shape as a stage change: old value, then new. Slugs, not display names,
# so the line stays stable when a service is renamed.
SERVICE_CHANGED_PREFIX = "Service changed: "
SERVICE_CHANGED_SEP = " -> "

# Sentinel for set_deal_service(offer_id=...): omit the argument to keep the
# current offer. None clears it. A positive id replaces it.
UNSET = object()


def stage_changed_body(old_stage: str, new_stage: str) -> str:
    """Activity body written by set_deal_stage."""
    return f"{STAGE_CHANGED_PREFIX}{old_stage}{STAGE_CHANGED_SEP}{new_stage}"


def service_changed_body(old_service: str, new_service: str) -> str:
    """Activity body written when a deal's service changes."""
    return f"{SERVICE_CHANGED_PREFIX}{old_service}{SERVICE_CHANGED_SEP}{new_service}"


def _offer_service_id(offer):
    if not offer:
        return None
    bound = offer.get("service_id")
    if bound in (None, "", 0):
        return None
    try:
        return int(bound)
    except (TypeError, ValueError):
        return None


def _offer_tied_to_other_service(offer, service_id) -> bool:
    """True when the offer is explicitly tied to some other service.

    An offer with no service_id is not tied to a different service, so it can
    stay on the deal when the caller does not send a replacement.
    """
    bound = _offer_service_id(offer)
    if bound is None:
        return False
    return bound != int(service_id)


def _offer_belongs_to_service(offer, service_id) -> bool:
    bound = _offer_service_id(offer)
    return bound is not None and bound == int(service_id)


def check_offer_for_deal(deal, offer_id):
    """Error code when offer_id can't go on the deal as-is, else None.

    For an offer-only change: the offer must exist and not be tied to a
    service other than the deal's.
    """
    offer = get_offer(offer_id)
    if not offer:
        return "invalid_offer"
    if deal.get("service_id") and _offer_tied_to_other_service(offer, deal["service_id"]):
        return "offer_not_for_service"
    return None


def _plan_service_change(deal, service, offer_id=UNSET):
    """Return (plan, error_code). Does not write.

    `offer_id` is UNSET to keep the current offer, None to clear it, or an
    offer id. A kept offer may have no service. An offer tied to a different
    service is `offer_service_mismatch` unless the caller clears it or passes
    an offer whose service_id is the new service (`offer_not_for_service`
    when the passed offer is some other offer). Passing the deal's current
    offer id counts as keeping it.

    The plan never includes stage, tags, or next action.
    """
    if not deal:
        return None, "not_found"
    if not service:
        return None, "invalid_service"
    if not int(service.get("active") or 0):
        return None, "inactive_service"
    same_service = (
        deal.get("service_id") is not None
        and int(deal["service_id"]) == int(service["id"])
    )
    current_offer_id = deal.get("offer_id")
    if offer_id is UNSET:
        if same_service:
            return {"noop": True}, None
        current = get_offer(current_offer_id) if current_offer_id else None
        if _offer_tied_to_other_service(current, service["id"]):
            return None, "offer_service_mismatch"
        return {"service_id": int(service["id"]), "offer_id": UNSET, "log": True}, None
    if offer_id in (None, "", 0, "0"):
        if same_service and not current_offer_id:
            return {"noop": True}, None
        return {
            "service_id": int(service["id"]),
            "offer_id": None,
            "log": not same_service,
        }, None
    try:
        parsed = int(offer_id)
    except (TypeError, ValueError):
        return None, "invalid_offer"
    if parsed < 1:
        return None, "invalid_offer"
    same_offer = current_offer_id is not None and int(current_offer_id) == parsed
    offer = get_offer(parsed)
    if same_offer:
        if _offer_tied_to_other_service(offer, service["id"]):
            return None, "offer_service_mismatch"
        if same_service:
            return {"noop": True}, None
        return {"service_id": int(service["id"]), "offer_id": UNSET, "log": True}, None
    if not offer:
        return None, "invalid_offer"
    if not _offer_belongs_to_service(offer, service["id"]):
        return None, "offer_not_for_service"
    return {
        "service_id": int(service["id"]),
        "offer_id": parsed,
        "log": not same_service,
    }, None


def _write_service_plan(db, deal, service, plan):
    """Write a plan from _plan_service_change on `db`; the caller commits.

    Stage, tags, and next action stay. Returns False when the deal is gone.
    """
    if plan.get("noop"):
        return True
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    columns = ["service_id = ?", "updated_at = ?"]
    params = [plan["service_id"], now]
    if "offer_id" in plan and plan["offer_id"] is not UNSET:
        columns.insert(1, "offer_id = ?")
        params.insert(1, plan["offer_id"])
    params.append(deal["id"])
    cursor = db.execute(
        f"UPDATE deals SET {', '.join(columns)} WHERE id = ?",
        params,
    )
    if cursor.rowcount < 1:
        return False
    if plan.get("log"):
        # Inline rather than log_activity so it shares the UPDATE's transaction.
        old = get_service(deal.get("service_id")) if deal.get("service_id") else None
        old_label = old["slug"] if old else "(none)"
        db.execute(
            """INSERT INTO activities (partner_id, deal_id, type, body)
               VALUES (?, ?, 'system', ?)""",
            (
                deal.get("partner_id") or None,
                deal["id"],
                sanitize_text(service_changed_body(old_label, service["slug"]), max_len=4000),
            ),
        )
    return True


def set_deal_service(deal_id: int, service_id: int, *, offer_id=UNSET):
    """Move a deal to another service. Returns (deal, None) or (None, error).

    The service must exist and be active. See `_plan_service_change` for the
    offer rule. Does not change stage, tags, next action, or next action date.
    Logs `Service changed: {old slug} -> {new slug}` when the service id changes.
    """
    deal = get_deal(deal_id)
    if not deal:
        return None, "not_found"
    _ids, error, _blocked = assign_deals_service([deal], service_id, offer_id=offer_id)
    if error:
        return None, error
    return get_deal(deal_id), None


def assign_deals_service(deals, service_id: int, *, offer_id=UNSET):
    """Apply one service change to many deals in one transaction.

    Returns (updated_ids, error, blocked_ids). Nothing is written on error.
    `blocked_ids` is the deals whose current offer is tied to a different
    service, when `offer_id` was left unset or repeated the current offer.
    Other errors leave it None. Does not change stage, tags, or next action.
    """
    service = get_service(service_id)
    if not service:
        return None, "invalid_service", None
    if not int(service.get("active") or 0):
        return None, "inactive_service", None
    plans = []
    blocked = []
    for deal in deals or []:
        plan, error = _plan_service_change(deal, service, offer_id)
        if error == "offer_service_mismatch":
            blocked.append(deal["id"])
            continue
        if error:
            return None, error, None
        plans.append((deal, plan))
    if blocked:
        return None, "offer_service_mismatch", blocked
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        for deal, plan in plans:
            if not _write_service_plan(db, deal, service, plan):
                db.rollback()
                return None, "not_found", None
        db.commit()
    return [deal["id"] for deal, _plan in plans], None, None


def service_change_message(error, *, service=None, offer_id=None, deal_ids=None) -> str:
    """Operator-facing text for a set_deal_service / assign_deals_service error."""
    slug = (service or {}).get("slug")
    target = f"{slug!r}" if slug else "the new service"
    if error == "invalid_service":
        return "That service does not exist."
    if error == "inactive_service":
        if slug:
            return f"Service {slug!r} is not active."
        return "That service is not active."
    if error == "invalid_offer":
        if offer_id:
            return f"No offer with id {offer_id}."
        return "That offer does not exist."
    if error == "offer_not_for_service":
        if offer_id:
            return (
                f"Offer {offer_id} does not belong to service {target}. "
                "Pass an offer for that service, or clear the offer."
            )
        return f"That offer does not belong to service {target}."
    if error == "offer_service_mismatch":
        if deal_ids:
            listed = ", ".join(str(deal_id) for deal_id in deal_ids)
            return (
                f"Deals {listed} have an offer that belongs to a different service. "
                f"Pass an offer for {target}, or clear the offer. Nothing was changed."
            )
        return (
            "This deal's offer belongs to a different service. "
            f"Pass an offer for {target}, or clear the offer."
        )
    if error == "not_found":
        return "That deal was not found."
    return error or "Could not change the service."


def stage_changed_to_like(new_stage: str) -> str:
    """SQL LIKE that matches stage_changed_body(..., new_stage)."""
    return f"{STAGE_CHANGED_PREFIX}%{STAGE_CHANGED_SEP}{new_stage}"


def _resolve_call_outcome_target(target):
    # More than one stage may share a role (nurture/won/lost); the first by
    # `position` order wins — that's the contract, not an implementation
    # detail, if you ever configure more than one.
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
                 attach_default_offer: bool = True, tags=None,
                 probability=None, priority=None, expected_close=""):
    if not stage or not pipeline_stages.get_stage(stage):
        stage = pipeline_stages.default_stage_key()
    if offer_id and get_offer(offer_id):
        pass
    elif attach_default_offer:
        offer_id = default_offer_id()
    else:
        offer_id = None
    cleaned_parent, _parent_error = validate_parent_link(None, parent_deal_id, partner_id)
    try:
        priority_value = int(priority) if priority not in (None, "") else 0
    except (TypeError, ValueError):
        priority_value = 0
    if priority_value < 0 or priority_value > 3:
        priority_value = 0
    try:
        probability_value = int(probability) if probability not in (None, "") else None
    except (TypeError, ValueError):
        probability_value = None
    if probability_value is not None and (probability_value < 0 or probability_value > 100):
        probability_value = None
    with get_db() as db:
        max_row = db.execute(
            "SELECT MAX(board_position) AS m FROM deals WHERE stage = ?",
            (stage,),
        ).fetchone()
        next_pos = (max_row["m"] if max_row["m"] is not None else -1) + 1
        db.execute("""
            INSERT INTO deals (
                partner_id, service_id, stage, source, value_estimate,
                pain_points, goals, next_action, next_action_date, offer_id,
                owner_key, external_ref, parent_deal_id,
                probability, priority, expected_close, board_position
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            probability_value,
            priority_value,
            sanitize_text(expected_close, max_len=32) or None,
            next_pos,
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
    """Most recent open deal this partner already has for this service, if any —
    used by the lead-ingestion API to avoid piling up duplicate deals when an ETL job
    re-submits the same lead on a later run.

    Open is active = 1 and the stage is not is_won.
    """
    won = pipeline_stages.won_stage_keys()
    with get_db() as db:
        query = """SELECT * FROM deals
            WHERE partner_id = ? AND service_id = ? AND active = 1"""
        params = [partner_id, service_id]
        if won:
            placeholders = ",".join("?" * len(won))
            query += f" AND stage NOT IN ({placeholders})"
            params.extend(won)
        query += " ORDER BY created_at DESC LIMIT 1"
        row = db.execute(query, params).fetchone()
        if not row:
            return None
        deal = dict(row)
    deal["tags"] = tags_for_deal(deal["id"])
    return deal


def list_deals(stage: str = None, partner_id: int = None, owner_key: str = None,
               service_id: int = None, service_slug: str = None, deal_ids=None,
               parent_deal_id: int = None, tags=None, type: str = "opportunity",
               include_lost: bool = False, order: str = None):
    wanted_tags, impossible = coerce_tag_filter(tags)
    if impossible:
        return []
    record_type = type if type in ("opportunity", "lead", "all") else "opportunity"
    # LEFT JOIN so a lead with a null partner/service is not dropped (§5.8).
    query = """
        SELECT deals.*, partners.name AS partner_name, services.name AS service_name,
               services.slug AS service_slug,
               lost_reasons.name AS lost_reason_name,
               lost_reasons.is_disqualification AS is_disqualification
        FROM deals
        LEFT JOIN partners ON partners.id = deals.partner_id
        LEFT JOIN services ON services.id = deals.service_id
        LEFT JOIN lost_reasons ON lost_reasons.id = deals.lost_reason_id
        WHERE 1=1
    """
    params = []
    if record_type != "all":
        query += " AND deals.type = ?"
        params.append(record_type)
    if not include_lost:
        query += " AND deals.active = 1"
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
    if order == "board":
        query += " ORDER BY deals.board_position ASC, deals.id ASC"
    else:
        query += " ORDER BY deals.updated_at DESC"
    with get_db() as db:
        rows = db.execute(query, params).fetchall()
        deals = [dict(r) for r in rows]
    deals = attach_tags(deals)
    from app.services.deal_outcome import attach_outcomes
    return attach_outcomes(deals)


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


def list_parent_candidates(partner_id, exclude_deal_id=None):
    """This partner's other deals that would be a valid parent.

    Used by the admin deal form picker. Self-parent, partner mismatch, and
    cycles are already rejected by validate_parent_link.
    """
    if not partner_id:
        return []
    rows = list_deals(partner_id=partner_id)
    out = []
    for row in rows:
        _cleaned, error = validate_parent_link(exclude_deal_id, row["id"], partner_id)
        if error:
            continue
        out.append(row)
    return out


def update_deal_fields(deal_id: int, **fields):
    allowed = {
        "source", "value_estimate", "pain_points", "goals", "next_action", "next_action_date",
        "offer_id", "owner_key", "external_ref", "parent_deal_id",
        "probability", "priority", "expected_close",
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
    if "probability" in updates:
        try:
            value = int(updates["probability"]) if updates["probability"] not in (None, "") else None
        except (TypeError, ValueError):
            value = None
        if value is not None and (value < 0 or value > 100):
            value = None
        updates["probability"] = value
    if "priority" in updates:
        try:
            value = int(updates["priority"]) if updates["priority"] not in (None, "") else 0
        except (TypeError, ValueError):
            value = 0
        if value < 0 or value > 3:
            value = 0
        updates["priority"] = value
    if "expected_close" in updates:
        updates["expected_close"] = sanitize_text(updates["expected_close"] or "", max_len=32) or None
    if not updates:
        return False
    updates["updated_at"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [deal_id]
    with get_db() as db:
        cursor = db.execute(f"UPDATE deals SET {set_clause} WHERE id = ?", values)
        db.commit()
        return cursor.rowcount > 0


def _lost_reason_was_supplied(lost_reason_id, lost_reason) -> bool:
    if lost_reason_id not in (None, "", 0, "0"):
        return True
    return bool(lost_reason)


def mark_lost(deal_id: int, *, lost_reason_id=None, lost_reason=None, note=""):
    """Archive a lead or opportunity. Returns (deal, None) or (None, error).

    A second call with a different reason updates `lost_reason_id` and leaves
    `closed_at` as it was. The same reason returns `already_lost`. An empty
    note on that change keeps the existing note. Nurture runs only on the
    first archive, not when the reason alone changes.
    """
    from app.services.lost_reasons import resolve_lost_reason

    deal = get_deal(deal_id)
    if not deal:
        return None, "not_found"
    supplied = _lost_reason_was_supplied(lost_reason_id, lost_reason)
    reason = resolve_lost_reason(lost_reason_id, lost_reason) if supplied else None
    already = not deal.get("active", 1)
    if already:
        # A merged source keeps its Duplicate reason.
        if not supplied or deal.get("merged_into_id"):
            return None, "already_lost"
        if not reason:
            return None, "invalid_lost_reason"
        if deal.get("lost_reason_id") == reason["id"]:
            return None, "already_lost"
        return _change_lost_reason(deal, reason, note)
    if not reason:
        return None, "invalid_lost_reason"
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    note_text = sanitize_text(note or "", max_len=2000, allow_newlines=True) or None
    with get_db() as db:
        db.execute(
            """UPDATE deals
               SET active = 0, lost_reason_id = ?, lost_note = ?,
                   closed_at = ?, updated_at = ?
               WHERE id = ?""",
            (reason["id"], note_text, now, now, deal_id),
        )
        db.commit()
    verb = "disqualified" if reason.get("is_disqualification") else "lost"
    log_activity(
        deal.get("partner_id"),
        "system",
        f"Marked {verb}: {reason['name']}" + (f" — {note_text}" if note_text else ""),
        deal_id=deal_id,
    )
    nurture_result = None
    if reason.get("triggers_nurture"):
        from app.services.catalog import get_service
        from app.services.nurture import SENT as NURTURE_SENT, SKIPPED as NURTURE_SKIPPED
        from app.services.nurture import enroll_partner_in_nurture
        partner = get_partner(deal["partner_id"]) if deal.get("partner_id") else None
        service = get_service(deal["service_id"]) if deal.get("service_id") else None
        if not service:
            message = "Nurture enrollment failed: no service on this record"
            log_activity(deal.get("partner_id"), "system", message, deal_id=deal_id)
            nurture_result = {"status": "failed", "message": message}
        else:
            status, message = enroll_partner_in_nurture(
                partner, service, deal_id=deal_id, deal=deal, lost_reason=reason["name"],
            )
            if status != NURTURE_SKIPPED:
                log_activity(
                    deal.get("partner_id"),
                    "system",
                    f"Nurture enrollment {'succeeded' if status == NURTURE_SENT else 'failed'}: {message}",
                    deal_id=deal_id,
                )
            nurture_result = {"status": status, "message": message}
    updated = get_deal(deal_id)
    if nurture_result:
        updated["nurture"] = nurture_result
    return updated, None


def _change_lost_reason(deal, reason, note):
    """Replace the reason on an already-lost deal. `closed_at` stays put."""
    from app.services.lost_reasons import get_lost_reason

    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    note_text = sanitize_text(note or "", max_len=2000, allow_newlines=True) or None
    if note_text:
        sql = """UPDATE deals
                 SET lost_reason_id = ?, lost_note = ?, updated_at = ?
                 WHERE id = ?"""
        params = (reason["id"], note_text, now, deal["id"])
    else:
        sql = """UPDATE deals
                 SET lost_reason_id = ?, updated_at = ?
                 WHERE id = ?"""
        params = (reason["id"], now, deal["id"])
    with get_db() as db:
        db.execute(sql, params)
        db.commit()
    previous = get_lost_reason(deal["lost_reason_id"]) if deal.get("lost_reason_id") else None
    previous_name = previous["name"] if previous else "none"
    body = f"Lost reason changed from {previous_name} to {reason['name']}"
    if note_text:
        body += f" — {note_text}"
    log_activity(deal.get("partner_id"), "system", body, deal_id=deal["id"])
    return get_deal(deal["id"]), None


def restore_deal(deal_id: int):
    """Re-open a lost record. Merged records cannot be restored."""
    deal = get_deal(deal_id)
    if not deal:
        return None, "not_found"
    if deal.get("active", 1):
        return None, "not_lost"
    if deal.get("merged_into_id"):
        return None, "merged"
    stage = deal.get("stage")
    if deal.get("type") == "opportunity":
        meta = pipeline_stages.get_stage(stage)
        if meta and meta["is_lost"]:
            stage = pipeline_stages.default_stage_key()
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    with get_db() as db:
        db.execute(
            """UPDATE deals
               SET active = 1, lost_reason_id = NULL, lost_note = NULL,
                   closed_at = NULL, stage = ?, updated_at = ?
               WHERE id = ?""",
            (stage, now, deal_id),
        )
        db.commit()
    log_activity(deal.get("partner_id"), "system", "Restored from lost", deal_id=deal_id)
    return get_deal(deal_id), None


def _coerce_board_position(position):
    """Optional 0-based index from a form field or API. None means 'append'."""
    if position is None or isinstance(position, bool):
        return None
    if isinstance(position, str) and not position.strip():
        return None
    try:
        return int(position)
    except (TypeError, ValueError):
        return None


def _apply_board_order(db, deal_id: int, stage: str, position=None, active=1) -> None:
    """Rewrite board_position for the cards visible on the board in `stage`.

    The board renders one visibility class per view — open opportunities by
    default, lost ones behind `?lost=1`, never leads — so only that class is
    renumbered. Hidden rows sharing the stage must not shift the order the
    user actually sees, otherwise a drop lands a slot off.
    """
    rows = db.execute(
        "SELECT id FROM deals WHERE stage = ? AND id != ? "
        "AND type = 'opportunity' AND active = ? "
        "ORDER BY board_position ASC, id ASC",
        (stage, deal_id, active),
    ).fetchall()
    ids = [row[0] for row in rows]
    if position is None:
        ids.append(deal_id)
    else:
        pos = max(0, min(int(position), len(ids)))
        ids.insert(pos, deal_id)
    for index, sibling_id in enumerate(ids):
        db.execute(
            "UPDATE deals SET board_position = ? WHERE id = ?",
            (index, sibling_id),
        )


def _compact_board_order(db, stage: str, active=1) -> None:
    rows = db.execute(
        "SELECT id FROM deals WHERE stage = ? "
        "AND type = 'opportunity' AND active = ? "
        "ORDER BY board_position ASC, id ASC",
        (stage, active),
    ).fetchall()
    for index, row in enumerate(rows):
        db.execute(
            "UPDATE deals SET board_position = ? WHERE id = ?",
            (index, row[0]),
        )


def set_deal_stage(deal_id: int, new_stage: str, position=None) -> bool:
    """Move a deal to a new stage. Fires nurture enrollment when the target
    stage has `triggers_nurture` set (not on every save), fires the deal-won
    webhook when entering `is_won` from a non-won stage, sets `closed_at`
    when it has `is_won` or `is_lost` set, and logs the transition.

    `position` is an optional 0-based index in the destination column. A
    same-stage call with a position only reorders the board (no activity,
    no automations). A stage change without a position appends the card.
    """
    stage_meta = pipeline_stages.get_stage(new_stage)
    if not stage_meta:
        return False

    deal = get_deal(deal_id)
    if not deal:
        return False
    old_stage = deal["stage"]
    parsed_position = _coerce_board_position(position)
    stage_changed = old_stage != new_stage
    if not stage_changed and parsed_position is None:
        return True

    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    if stage_changed:
        closed_at = now if (stage_meta["is_won"] or stage_meta["is_lost"]) else None
        with get_db() as db:
            db.execute(
                "UPDATE deals SET stage = ?, updated_at = ?, closed_at = ? WHERE id = ?",
                (new_stage, now, closed_at, deal_id),
            )
            _apply_board_order(
                db, deal_id, new_stage, parsed_position, active=deal.get("active", 1),
            )
            _compact_board_order(db, old_stage, active=deal.get("active", 1))
            db.commit()
    else:
        with get_db() as db:
            _apply_board_order(
                db, deal_id, new_stage, parsed_position, active=deal.get("active", 1),
            )
            db.commit()
        return True

    log_activity(deal["partner_id"], "system", stage_changed_body(old_stage, new_stage), deal_id=deal_id)

    if stage_meta["is_lost"] and deal.get("active", 1):
        marked, lost_error = mark_lost(deal_id, lost_reason="Lost stage (migrated)")
        if lost_error and lost_error not in ("already_lost",):
            logger.warning("is_lost shim failed for deal %s: %s", deal_id, lost_error)
        if marked:
            deal = marked

    if stage_meta["triggers_nurture"]:
        partner = get_partner(deal["partner_id"])
        service = get_service(deal["service_id"])
        status, message = enroll_partner_in_nurture(partner, service, deal_id=deal_id)
        if status != NURTURE_SKIPPED:
            log_activity(
                deal["partner_id"],
                "system",
                f"Nurture enrollment {'succeeded' if status == NURTURE_SENT else 'failed'}: {message}",
                deal_id=deal_id,
            )

    old_meta = pipeline_stages.get_stage(old_stage)
    already_won = bool(old_meta and old_meta["is_won"])
    if stage_meta["is_won"] and not already_won:
        partner = get_partner(deal["partner_id"])
        service = get_service(deal["service_id"])
        offer = get_offer(deal.get("offer_id"))
        status, message = notify_deal_won(
            partner, service, deal, new_stage, offer=offer,
        )
        if status != WON_SKIPPED:
            log_activity(
                deal["partner_id"],
                "system",
                f"Deal-won webhook {'succeeded' if status == WON_SENT else 'failed'}: {message}",
                deal_id=deal_id,
            )

    # After the stage is committed, same as nurture and the won webhook.
    # No automations are seeded, so this is a no-op until an operator adds one.
    try:
        from app.services.stage_automations import apply_stage_automations
        apply_stage_automations(deal, old_stage, new_stage)
    except Exception:
        logger.exception("Stage automations failed for deal %s", deal_id)

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

    if outcome == "not_interested":
        if deal.get("active", 1):
            marked, lost_error = mark_lost(deal_id, lost_reason="Not a fit", note=note)
            if lost_error and lost_error not in ("already_lost",):
                logger.warning("not_interested mark_lost failed for deal %s: %s", deal_id, lost_error)
        return True

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
