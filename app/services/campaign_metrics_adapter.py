"""Build campaign-metrics inputs from the database and call the calculator.

App pages and MCP tools both call `metrics_for_campaign`. They do not
count deals themselves.

RECONNECT(B) of #154
-------------------
When `app.services.campaign_metrics.compute_campaign_metrics` exists, it
is used. Until then `campaign_metrics_stub.compute_campaign_metrics` is
used. No edit is required if B's function has this signature and returns
the dict below. If B names the function differently, change `_load_compute`.

    compute_campaign_metrics(campaign, deals, events, today, tz, period=None) -> dict

`today` is a `datetime.date` in the operator timezone (`TZ`, UTC if unset
or invalid — the same clock as the follow-up gate). `tz` is a
`zoneinfo.ZoneInfo`. `period` is None or `week`, `month`, or `year`.
Weeks are Monday-start; the stub does not build period rows.

`campaign` dict:

    id, name, slug, status, start_date, deadline,   # dates are YYYY-MM-DD or None
    targets: {leads, contacts, conversations, advanced, won, won_value}
        each None or a number
    qualified_pool_max_position: int or None
        highest pipeline position with is_qualified_pool, so "advanced"
        does not depend on a stage key

`deals` list. `outcome` is already classified (see RECONNECT(A)):

    id, type, stage, stage_label, stage_position,
    is_won_stage, is_lost_stage, is_qualified_pool, triggers_nurture,
    active, outcome,          # open | won | lost | disqualified
    value_estimate, closed_at, created_at,
    lost_reason_id, lost_reason_name,
    lost_reason_is_disqualification   # False when that column does not exist

`events` list, one per activity on those deals, oldest first:

    id, deal_id, occurred_at, activity_type, body,
    kind,                     # activity | stage_change | call_outcome
    from_stage, to_stage,     # set when kind is stage_change
    call_outcome,             # key such as meeting_scheduled, or None
    call_outcome_label

Return dict (what the page and `get_campaign` render):

    results: total, by_stage[{key, label, count}], contacted, not_contacted,
        conversations, advanced, open, won, lost, disqualified,
        qualified_leads, won_value, lost_value
    win_loss: won, lost, disqualified, open, closed, ratio, win_rate,
        label ("3 : 1" or "no closed deals"), disqualification_rate,
        lost_by_reason[{name, count, value}], won_value, lost_value
        Disqualified deals are excluded from ratio and win_rate.
    pacing: today, timezone, start_date, deadline, has_deadline,
        deadline_passed, days_elapsed, days_remaining, days_total,
        time_elapsed_pct,
        summary: {primary_target, pct_complete, pace},
        targets: {<key>: {target, actual, remaining, pct_complete,
            required_per_week, required_per_month, required_per_year,
            actual_per_week, actual_per_month, actual_per_year,
            pace, projected}}
        pace is ahead | on_track | behind | None.
        No deadline: required rates and projection are null.
        Deadline already passed: required rates are null.
    periods: {grain, available, rows, note?}
        row: key, label, actuals, required, cumulative, won, lost,
             disqualified, win_rate, win_loss_label
        Rows are empty when period is None. When period is set, one row
        per week, month, or year.
    inputs: {"results.won": "why this number", ...}
        HTML title text. Paths use a dot. Period cells use
        periods.<row key>.actuals.<target> (and required / cumulative).

RECONNECT(A) of #154
-------------------
`classify_deal` calls `app.services.deal_outcome.deal_outcome(deal)` when
that function exists. The deal dict includes `active`, `stage`, and
`lost_reason` (with `is_disqualification` when the column exists). The
return must be open, won, lost, or disqualified.

Until that module exists, the fallback is: active 0 -> lost; otherwise a
stage with is_won -> won; otherwise open. It does not read
`is_disqualification`. If A uses another module or function name, change
`_load_shared_outcome`.
"""
import logging

from app.database import get_db
from app.services import pipeline_stages
from app.services.deals import CALL_OUTCOMES, STAGE_CHANGED_PREFIX, STAGE_CHANGED_SEP
from app.services.staleness import operator_today, operator_zone

logger = logging.getLogger(__name__)

PERIODS = ("week", "month", "year")
_OUTCOMES = {"open", "won", "lost", "disqualified"}
_CALL_LABEL_TO_KEY = {label: key for key, label, _target in CALL_OUTCOMES}

_TARGET_KEYS = ("leads", "contacts", "conversations", "advanced", "won", "won_value")
_TARGET_COLUMNS = {
    "leads": "target_leads",
    "contacts": "target_contacts",
    "conversations": "target_conversations",
    "advanced": "target_advanced",
    "won": "target_won",
    "won_value": "target_won_value",
}


def _load_shared_outcome():
    """The disqualification-aware classifier, once that module is installed."""
    try:
        from app.services import deal_outcome as module
    except ImportError:
        return None
    function = getattr(module, "deal_outcome", None)
    if not callable(function):
        logger.warning(
            "app.services.deal_outcome has no deal_outcome(); "
            "campaign outcomes stay open/won/lost. RECONNECT(A) of #154."
        )
        return None
    return function


def _load_compute():
    """Prefer B's module. The stub keeps the pages working until it lands."""
    try:
        from app.services import campaign_metrics as module
    except ImportError:
        module = None
    function = getattr(module, "compute_campaign_metrics", None) if module else None
    if callable(function):
        return function, "campaign_metrics"
    from app.services import campaign_metrics_stub as stub

    return stub.compute_campaign_metrics, "stub"


def classify_deal(deal, *, won_stage_keys=None) -> str:
    """open, won, lost, or disqualified. See RECONNECT(A) in the module doc."""
    shared = _load_shared_outcome()
    if shared is not None:
        outcome = shared(deal)
        if isinstance(outcome, str) and outcome.strip().lower() in _OUTCOMES:
            return outcome.strip().lower()
        logger.warning("deal_outcome returned %r; using the open/won/lost fallback", outcome)
    if won_stage_keys is None:
        won_stage_keys = pipeline_stages.won_stage_keys()
    return _fallback_outcome(deal, won_stage_keys)


def _fallback_outcome(deal, won_stage_keys) -> str:
    # RECONNECT(A): active=0 is lost even when lost_reason.is_disqualification
    # is set. That flag is not interpreted here.
    active = deal.get("active")
    if active in (0, False):
        return "lost"
    stage = deal.get("stage") or ""
    if stage in won_stage_keys:
        return "won"
    return "open"


def annotate_outcomes(deals) -> None:
    """Set `outcome` on each deal with the shared classifier."""
    if not deals:
        return
    _attach_lost_reasons(deals)
    won_stage_keys = pipeline_stages.won_stage_keys()
    for deal in deals:
        deal["outcome"] = classify_deal(deal, won_stage_keys=won_stage_keys)


def metrics_for_campaign(campaign, *, period=None, today=None) -> dict:
    """Results, win/loss, pacing, and an optional period breakdown.

    `period` None omits period rows (the list page). `week`, `month`, or
    `year` asks for that breakdown. Raises ValueError for anything else.
    """
    if period == "":
        period = None
    if period is not None and period not in PERIODS:
        raise ValueError("invalid_period")
    today = today or operator_today()
    tz = operator_zone()
    deals = _load_deals(campaign["slug"])
    events = _load_events([deal["id"] for deal in deals])
    payload = _campaign_payload(campaign)
    function, engine = _load_compute()
    raw = function(payload, deals, events, today, tz, period=period)
    metrics = _with_engine(raw, engine)
    metrics["pacing"].setdefault("timezone", getattr(tz, "key", None) or "UTC")
    metrics["pacing"].setdefault("today", today.isoformat())
    if period:
        metrics["periods"].setdefault("grain", period)
    return metrics


def summary_from_metrics(metrics) -> dict:
    """The columns on the campaigns list. Same numbers as the detail page."""
    metrics = metrics or {}
    win_loss = metrics.get("win_loss") or {}
    pacing = metrics.get("pacing") or {}
    summary = pacing.get("summary") or {}
    inputs = metrics.get("inputs") or {}
    return {
        "pct_complete": summary.get("pct_complete"),
        "pace": summary.get("pace"),
        "primary_target": summary.get("primary_target"),
        "win_loss_label": win_loss.get("label") or "no closed deals",
        "win_rate": win_loss.get("win_rate"),
        "won": win_loss.get("won"),
        "lost": win_loss.get("lost"),
        "disqualified": win_loss.get("disqualified"),
        "open": win_loss.get("open"),
        "pct_inputs": inputs.get("pacing.summary.pct_complete") or "",
        "pace_inputs": inputs.get("pacing.summary.pace") or "",
        "win_loss_inputs": inputs.get("win_loss.label") or "",
    }


def _campaign_payload(campaign) -> dict:
    stages = pipeline_stages.list_stages()
    pool_positions = [
        stage["position"] for stage in stages if stage.get("is_qualified_pool")
    ]
    targets = {}
    for key, column in _TARGET_COLUMNS.items():
        targets[key] = campaign.get(column)
    return {
        "id": campaign.get("id"),
        "name": campaign.get("name"),
        "slug": campaign.get("slug"),
        "status": campaign.get("status"),
        "start_date": campaign.get("start_date"),
        "deadline": campaign.get("deadline"),
        "targets": targets,
        "qualified_pool_max_position": max(pool_positions) if pool_positions else None,
        "pipeline": [
            {
                "key": stage["key"],
                "label": stage["label"],
                "position": stage["position"],
                "is_won": bool(stage.get("is_won")),
                "is_lost": bool(stage.get("is_lost")),
                "is_qualified_pool": bool(stage.get("is_qualified_pool")),
                "triggers_nurture": bool(stage.get("triggers_nurture")),
            }
            for stage in stages
        ],
    }


def _load_deals(slug: str) -> list:
    from app.services.campaigns import campaign_tag
    from app.services.deals import list_deals

    tag = campaign_tag(slug)
    if not tag:
        return []
    rows = list_deals(tags=[tag], type="all", include_lost=True)
    return _prepare_deals(rows)


def _prepare_deals(rows) -> list:
    stages = {stage["key"]: stage for stage in pipeline_stages.list_stages()}
    _attach_lost_reasons(rows)
    won_stage_keys = pipeline_stages.won_stage_keys()
    prepared = []
    for deal in rows:
        stage = stages.get(deal.get("stage") or "")
        reason = deal.get("lost_reason")
        prepared.append({
            "id": deal.get("id"),
            "type": deal.get("type"),
            "stage": deal.get("stage"),
            "stage_label": (stage or {}).get("label") or deal.get("stage") or "",
            "stage_position": (stage or {}).get("position"),
            "is_won_stage": bool(stage and stage.get("is_won")),
            "is_lost_stage": bool(stage and stage.get("is_lost")),
            "is_qualified_pool": bool(stage and stage.get("is_qualified_pool")),
            "triggers_nurture": bool(stage and stage.get("triggers_nurture")),
            "active": deal.get("active"),
            "outcome": classify_deal(deal, won_stage_keys=won_stage_keys),
            "value_estimate": deal.get("value_estimate"),
            "closed_at": deal.get("closed_at"),
            "created_at": deal.get("created_at"),
            "lost_reason_id": deal.get("lost_reason_id"),
            "lost_reason_name": deal.get("lost_reason_name"),
            "lost_reason_is_disqualification": bool(
                deal.get("lost_reason_is_disqualification")
            ),
            "lost_reason": reason,
        })
    return prepared


def _attach_lost_reasons(deals) -> None:
    ids = sorted({
        int(deal["lost_reason_id"])
        for deal in deals
        if deal.get("lost_reason_id")
    })
    reasons = {}
    if ids:
        for chunk in _chunks(ids, 500):
            placeholders = ", ".join("?" * len(chunk))
            with get_db() as db:
                rows = db.execute(
                    f"SELECT * FROM lost_reasons WHERE id IN ({placeholders})",
                    chunk,
                ).fetchall()
            for row in rows:
                reasons[row["id"]] = dict(row)
    for deal in deals:
        reason = reasons.get(deal.get("lost_reason_id"))
        deal["lost_reason"] = reason
        deal["lost_reason_name"] = reason.get("name") if reason else None
        deal["lost_reason_is_disqualification"] = bool(
            reason and reason.get("is_disqualification")
        )


def _load_events(deal_ids) -> list:
    if not deal_ids:
        return []
    activities = []
    for chunk in _chunks(list(deal_ids), 500):
        placeholders = ", ".join("?" * len(chunk))
        with get_db() as db:
            rows = db.execute(
                f"""SELECT * FROM activities
                    WHERE deal_id IN ({placeholders})
                    ORDER BY occurred_at, id""",
                chunk,
            ).fetchall()
        activities.extend(dict(row) for row in rows)
    return [_event_from_activity(row) for row in activities]


def _event_from_activity(row) -> dict:
    body = row.get("body") or ""
    event = {
        "id": row.get("id"),
        "deal_id": row.get("deal_id"),
        "occurred_at": row.get("occurred_at"),
        "activity_type": row.get("type"),
        "body": body,
        "kind": "activity",
        "from_stage": None,
        "to_stage": None,
        "call_outcome": None,
        "call_outcome_label": None,
    }
    if body.startswith("Call outcome: "):
        label = body[len("Call outcome: "):]
        label = label.split(" — ", 1)[0]
        label = label.split(" (", 1)[0].strip()
        event["kind"] = "call_outcome"
        event["call_outcome_label"] = label or None
        event["call_outcome"] = _CALL_LABEL_TO_KEY.get(label)
        return event
    if body.startswith(STAGE_CHANGED_PREFIX) and STAGE_CHANGED_SEP in body:
        rest = body[len(STAGE_CHANGED_PREFIX):]
        old, new = rest.split(STAGE_CHANGED_SEP, 1)
        event["kind"] = "stage_change"
        event["from_stage"] = old.strip() or None
        event["to_stage"] = new.strip() or None
    return event


def _chunks(items, size):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _with_engine(raw, engine: str) -> dict:
    if not isinstance(raw, dict):
        raw = {}
    metrics = dict(raw)
    results = metrics.get("results") if isinstance(metrics.get("results"), dict) else {}
    metrics["results"] = dict(results)
    metrics["results"].setdefault("by_stage", [])
    for key in (
        "total", "contacted", "not_contacted", "conversations", "advanced",
        "open", "won", "lost", "disqualified", "qualified_leads",
        "won_value", "lost_value",
    ):
        metrics["results"].setdefault(key, None)
    win_loss = metrics.get("win_loss") if isinstance(metrics.get("win_loss"), dict) else {}
    win_loss = dict(win_loss)
    win_loss.setdefault("lost_by_reason", [])
    win_loss.setdefault("label", "no closed deals")
    for key in (
        "won", "lost", "disqualified", "open", "closed", "ratio", "win_rate",
        "disqualification_rate", "won_value", "lost_value",
    ):
        win_loss.setdefault(key, None)
    metrics["win_loss"] = win_loss
    pacing = metrics.get("pacing") if isinstance(metrics.get("pacing"), dict) else {}
    pacing = dict(pacing)
    summary = pacing.get("summary") if isinstance(pacing.get("summary"), dict) else {}
    pacing["summary"] = dict(summary)
    for key in (
        "today", "timezone", "start_date", "deadline",
        "days_elapsed", "days_remaining", "days_total", "time_elapsed_pct",
    ):
        pacing.setdefault(key, None)
    targets = pacing.get("targets") if isinstance(pacing.get("targets"), dict) else {}
    pacing["targets"] = targets
    pacing.setdefault("has_deadline", False)
    pacing.setdefault("deadline_passed", False)
    metrics["pacing"] = pacing
    periods = metrics.get("periods") if isinstance(metrics.get("periods"), dict) else {}
    periods = dict(periods)
    rows = periods.get("rows") if isinstance(periods.get("rows"), list) else []
    periods["rows"] = rows
    periods.setdefault("available", bool(rows))
    metrics["periods"] = periods
    inputs = metrics.get("inputs") if isinstance(metrics.get("inputs"), dict) else {}
    metrics["inputs"] = dict(inputs)
    metrics["engine"] = engine
    return metrics
