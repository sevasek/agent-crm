"""Build campaign-metrics inputs from the database and call the calculator.

App pages and MCP tools both call `metrics_for_campaign`. They do not
count deals themselves.

`compute_campaign_metrics` in `app.services.campaign_metrics` does the
math. `deal_outcome` in `app.services.deal_outcome` labels each deal
`open`, `won`, `lost`, or `disqualified` before that call. Every figure
comes back as ``{"value", "formula", "inputs", "note"}``. Percents are
on a 0–100 scale. See that module's docstring and
docs/DATA_MODEL.md → Campaign metrics.

Deals passed in use `value` (the deal's `value_estimate`) and
`lost_reason` (the reason name). Events are one row per countable
activity:

- `contact` — a call or email, including a call outcome that is not a
  conversation
- `conversation` — a meeting, or a call outcome of warm, meeting
  scheduled, or won
- `advanced` — a stage change into a won stage, or into a stage after
  the qualified pool that is not nurture and not lost

A conversation does not also emit a contact. The stage change is the
advanced event, so a meeting-scheduled call and its stage move count
once each.
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
_CONVERSATION_OUTCOMES = frozenset({"interested", "meeting_scheduled", "won"})

_TARGET_COLUMNS = {
    "leads": "target_leads",
    "contacts": "target_contacts",
    "conversations": "target_conversations",
    "advanced": "target_advanced",
    "won": "target_won",
    "won_value": "target_won_value",
}


def _load_shared_outcome():
    """The disqualification-aware classifier."""
    try:
        from app.services.deal_outcome import deal_outcome
    except ImportError:
        return None
    if not callable(deal_outcome):
        logger.warning("app.services.deal_outcome.deal_outcome is not callable")
        return None
    return deal_outcome


def _load_compute():
    """The pure campaign metrics function."""
    from app.services.campaign_metrics import compute_campaign_metrics

    return compute_campaign_metrics, "campaign_metrics"


def classify_deal(deal, *, won_stage_keys=None) -> str:
    """open, won, lost, or disqualified, via `deal_outcome`."""
    shared = _load_shared_outcome()
    if shared is None:
        return _fallback_outcome(deal, won_stage_keys)
    outcome = shared(deal, won_stage_keys=won_stage_keys) if won_stage_keys is not None else shared(deal)
    if isinstance(outcome, str) and outcome.strip().lower() in _OUTCOMES:
        return outcome.strip().lower()
    logger.warning("deal_outcome returned %r; using the open/won/lost fallback", outcome)
    return _fallback_outcome(deal, won_stage_keys)


def _fallback_outcome(deal, won_stage_keys) -> str:
    """Used only when `deal_outcome` cannot be imported."""
    if won_stage_keys is None:
        won_stage_keys = pipeline_stages.won_stage_keys()
    active = deal.get("active")
    if active in (0, False):
        return "lost"
    stage = deal.get("stage") or ""
    if stage in won_stage_keys:
        return "won"
    return "open"


def annotate_outcomes(deals):
    """Set `outcome` on each deal with `attach_outcomes`."""
    if not deals:
        return deals
    from app.services.deal_outcome import attach_outcomes

    return attach_outcomes(deals)


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
    if not isinstance(raw, dict):
        raise TypeError("compute_campaign_metrics must return a dict")
    metrics = dict(raw)
    metrics["engine"] = engine
    return metrics


def summary_from_metrics(metrics) -> dict:
    """The columns on the campaigns list. Same numbers as the detail page.

    `pct_complete` and `win_rate` are on a 0–100 scale, or None.
    """
    metrics = metrics or {}
    win_loss = metrics.get("win_loss") if isinstance(metrics.get("win_loss"), dict) else {}
    ratio = win_loss.get("ratio") if isinstance(win_loss.get("ratio"), dict) else {}
    targets = metrics.get("targets") if isinstance(metrics.get("targets"), dict) else {}
    primary = next(iter(targets.values()), None) if targets else None
    if not isinstance(primary, dict):
        primary = None
    percent = (primary or {}).get("percent_complete")
    label = ratio.get("value") or ratio.get("note") or "no closed deals"
    return {
        "pct_complete": metric_number(percent),
        "pace": metrics.get("pacing_status"),
        "primary_target": (primary or {}).get("name"),
        "win_loss_label": label,
        "win_rate": metric_number(win_loss.get("win_rate")),
        "won": metric_number(win_loss.get("won")),
        "lost": metric_number(win_loss.get("lost")),
        "disqualified": metric_number(win_loss.get("disqualified")),
        "open": metric_number(win_loss.get("open")),
        "pct_title": metric_title(percent),
        "pace_title": _pace_title(primary, metrics.get("pacing_status")),
        "win_loss_title": metric_title(ratio) or metric_title(win_loss.get("win_rate")),
    }


def metric_number(node):
    """The number inside a metric dict, or the value itself."""
    if isinstance(node, dict) and "formula" in node:
        return node.get("value")
    return node


def metric_title(node) -> str:
    """Hover text: formula, the inputs, and the note when the value is blank."""
    if not isinstance(node, dict):
        return ""
    parts = []
    formula = node.get("formula")
    if formula:
        parts.append(str(formula))
    inputs = node.get("inputs")
    if isinstance(inputs, dict) and inputs:
        bits = [f"{key} = {_input_text(value)}" for key, value in inputs.items()]
        parts.append(", ".join(bits))
    note = node.get("note")
    if note:
        parts.append(str(note))
    return " — ".join(parts)


def _pace_title(primary, pacing_status) -> str:
    if not pacing_status:
        return ""
    inputs = (primary or {}).get("status_inputs") if isinstance(primary, dict) else None
    name = (primary or {}).get("name") if isinstance(primary, dict) else None
    text = f"Pace is {pacing_status}."
    if name:
        text += f" Primary target {name}."
    if isinstance(inputs, dict) and inputs:
        text += " " + ", ".join(f"{key} = {_input_text(value)}" for key, value in inputs.items())
    return text


def _input_text(value) -> str:
    if isinstance(value, float):
        text = f"{value:.4f}".rstrip("0").rstrip(".")
        return text or "0"
    return str(value)


def _campaign_payload(campaign) -> dict:
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
    annotate_outcomes(rows)
    prepared = []
    for deal in rows:
        reason = deal.get("lost_reason")
        if isinstance(reason, dict):
            reason_name = reason.get("name") or ""
        elif isinstance(reason, str):
            reason_name = reason
        else:
            reason_name = deal.get("lost_reason_name") or ""
        prepared.append({
            "id": deal.get("id"),
            "outcome": deal.get("outcome") or "open",
            "stage": deal.get("stage") or "",
            "value": deal.get("value_estimate"),
            "created_at": deal.get("created_at"),
            "closed_at": deal.get("closed_at"),
            "lost_reason": reason_name,
        })
    return prepared


def _load_events(deal_ids) -> list:
    if not deal_ids:
        return []
    advanced_keys = _advanced_stage_keys()
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
    events = []
    for row in activities:
        events.extend(_events_from_activity(row, advanced_keys))
    return events


def _advanced_stage_keys() -> set:
    stages = pipeline_stages.list_stages()
    pool = [stage["position"] for stage in stages if stage.get("is_qualified_pool")]
    pool_max = max(pool) if pool else None
    keys = set()
    for stage in stages:
        if stage.get("is_lost") or stage.get("triggers_nurture"):
            continue
        if stage.get("is_won"):
            keys.add(stage["key"])
            continue
        position = stage.get("position")
        if pool_max is not None and position is not None and position > pool_max:
            keys.add(stage["key"])
    return keys


def _events_from_activity(row, advanced_keys) -> list:
    body = row.get("body") or ""
    occurred = row.get("occurred_at")
    deal_id = row.get("deal_id")
    activity_type = row.get("type")
    call_key = _call_outcome_key(body)
    if call_key:
        kind = "conversation" if call_key in _CONVERSATION_OUTCOMES else "contact"
        return [_event(kind, deal_id, occurred)]
    if body.startswith(STAGE_CHANGED_PREFIX) and STAGE_CHANGED_SEP in body:
        new_stage = body.split(STAGE_CHANGED_SEP, 1)[1].strip()
        if new_stage in advanced_keys:
            return [_event("advanced", deal_id, occurred)]
        return []
    if activity_type == "meeting":
        return [_event("conversation", deal_id, occurred)]
    if activity_type in ("call", "email"):
        return [_event("contact", deal_id, occurred)]
    return []


def _call_outcome_key(body: str):
    if not body.startswith("Call outcome: "):
        return None
    label = body[len("Call outcome: "):]
    label = label.split(" — ", 1)[0]
    label = label.split(" (", 1)[0].strip()
    return _CALL_LABEL_TO_KEY.get(label)


def _event(kind: str, deal_id, occurred) -> dict:
    return {
        "type": kind,
        "deal_id": deal_id,
        "timestamp": occurred,
        "occurred_at": occurred,
    }


def _chunks(items, size):
    for start in range(0, len(items), size):
        yield items[start:start + size]
