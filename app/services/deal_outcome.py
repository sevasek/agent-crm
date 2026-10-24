"""Derived outcome of a deal record.

`deal_outcome` is the one helper other code should call, including campaign
metrics and campaign pages. Do not re-derive open / won / lost / disqualified.

A deal is:

- **disqualified** when `active = 0` and its lost reason has
  `is_disqualification` set
- **lost** when `active = 0` otherwise
- **won** when it is still active and its stage is an `is_won` stage
- **open** otherwise

Lost and disqualified take precedence over a won stage. The value is not
stored. It follows the lost reason's current flag, so flagging a reason
later changes the outcome of deals that already use it.

Pass `won_stage_keys` when classifying many deals. `attach_outcomes` does
that, and loads disqualification flags in one query.
"""

from app.database import get_db

OUTCOME_OPEN = "open"
OUTCOME_WON = "won"
OUTCOME_LOST = "lost"
OUTCOME_DISQUALIFIED = "disqualified"


def deal_outcome(deal, *, won_stage_keys=None) -> str:
    """Return `open`, `won`, `lost`, or `disqualified` for one deal dict.

    The lost reason is read from `deal["lost_reason"]` when that value is a
    `lost_reasons` row, otherwise from `deal["is_disqualification"]` when a
    query already joined the flag, otherwise by `deal["lost_reason_id"]`.
    """
    if not isinstance(deal, dict):
        return OUTCOME_OPEN
    if not deal.get("active", 1):
        if _is_disqualification(deal):
            return OUTCOME_DISQUALIFIED
        return OUTCOME_LOST
    if won_stage_keys is None:
        from app.services import pipeline_stages
        won_stage_keys = pipeline_stages.won_stage_keys()
    if deal.get("stage") in won_stage_keys:
        return OUTCOME_WON
    return OUTCOME_OPEN


def attach_outcomes(deals, *, won_stage_keys=None):
    """Set `outcome` and `is_disqualified` on each deal. Returns the same list."""
    if not deals:
        return deals
    if won_stage_keys is None:
        from app.services import pipeline_stages
        won_stage_keys = pipeline_stages.won_stage_keys()
    missing = []
    for deal in deals:
        if deal.get("active", 1):
            continue
        reason_id = deal.get("lost_reason_id")
        if reason_id and not _flag_known(deal):
            missing.append(int(reason_id))
    flags = _flags_for(missing)
    for deal in deals:
        source = deal
        reason_id = deal.get("lost_reason_id")
        if (
            not deal.get("active", 1)
            and reason_id
            and not _flag_known(deal)
            and int(reason_id) in flags
        ):
            source = dict(deal)
            source["is_disqualification"] = flags[int(reason_id)]
        outcome = deal_outcome(source, won_stage_keys=won_stage_keys)
        deal["outcome"] = outcome
        deal["is_disqualified"] = outcome == OUTCOME_DISQUALIFIED
    return deals


def _flag_known(deal) -> bool:
    reason = deal.get("lost_reason")
    if isinstance(reason, dict) and "is_disqualification" in reason:
        return True
    return bool(deal.get("lost_reason_id")) and deal.get("is_disqualification") is not None


def _is_disqualification(deal) -> bool:
    reason = deal.get("lost_reason")
    if isinstance(reason, dict) and "is_disqualification" in reason:
        return bool(reason.get("is_disqualification"))
    if deal.get("lost_reason_id") and deal.get("is_disqualification") is not None:
        return bool(deal.get("is_disqualification"))
    reason_id = deal.get("lost_reason_id")
    if not reason_id:
        return False
    from app.services.lost_reasons import get_lost_reason
    row = get_lost_reason(reason_id)
    return bool(row and row.get("is_disqualification"))


def _flags_for(reason_ids):
    ids = sorted(set(reason_ids))
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    with get_db() as db:
        rows = db.execute(
            f"SELECT id, is_disqualification FROM lost_reasons WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
    return {int(row["id"]): row["is_disqualification"] for row in rows}
