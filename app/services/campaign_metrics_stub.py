"""Temporary campaign metrics until `app.services.campaign_metrics` exists.

RECONNECT(B) of #154: delete this module once
`compute_campaign_metrics` lives in `app.services.campaign_metrics` and
returns the same dict. The adapter imports that function automatically.

This stub counts deals and win/loss, and it fills actual / remaining /
percent for each target that is set. It does not calculate required
rates, actual rates, pace, projection, or period rows. Those keys are
present and null, with an `inputs` note, so the page and MCP can render
them. Do not extend this file into the pacing formulas.
"""
from datetime import date, datetime

# Shown on hover for every figure this stub refuses to invent.
RATE_NOTE = (
    "Required rate, actual rate, on track / behind, projection, and the "
    "period breakdown are calculated by the campaign metrics module. "
    "This install is using the counts-only fallback."
)

_RATE_FIELDS = (
    "required_per_week",
    "required_per_month",
    "required_per_year",
    "actual_per_week",
    "actual_per_month",
    "actual_per_year",
    "pace",
    "projected",
)

_TARGET_ORDER = ("leads", "contacts", "conversations", "advanced", "won", "won_value")

# Call-outcome keys that count as a conversation in this fallback.
# meeting_scheduled is also the booking signal for "advanced".
_CONVERSATION_OUTCOMES = frozenset({"interested", "meeting_scheduled", "won"})


def compute_campaign_metrics(campaign, deals, events, today, tz, period=None) -> dict:
    """Same signature as `app.services.campaign_metrics.compute_campaign_metrics`.

    `deals` must already carry `outcome` (`open`, `won`, `lost`, or
    `disqualified`). Disqualified deals are left out of win/loss and out
    of the lead-count actual. This stub never classifies a deal itself.
    """
    deals = list(deals or [])
    events = list(events or [])
    today = _as_date(today)
    targets = (campaign or {}).get("targets") or {}

    deal_ids = {deal.get("id") for deal in deals}
    contacted_ids = set()
    conversation_ids = set()
    booked_ids = set()
    for event in events:
        deal_id = event.get("deal_id")
        if deal_id not in deal_ids:
            continue
        activity_type = event.get("activity_type")
        if activity_type in ("call", "email", "meeting") or event.get("kind") == "call_outcome":
            contacted_ids.add(deal_id)
        outcome_key = event.get("call_outcome")
        if activity_type == "meeting" or outcome_key in _CONVERSATION_OUTCOMES:
            conversation_ids.add(deal_id)
        if outcome_key == "meeting_scheduled":
            booked_ids.add(deal_id)

    pool_max = (campaign or {}).get("qualified_pool_max_position")
    by_stage = {}
    won = lost = disqualified = open_count = 0
    won_value = 0.0
    lost_value = 0.0
    lost_reasons = {}
    advanced_ids = set()
    for deal in deals:
        outcome = deal.get("outcome") or "open"
        if outcome == "won":
            won += 1
            won_value += _amount(deal.get("value_estimate"))
        elif outcome == "lost":
            lost += 1
            amount = _amount(deal.get("value_estimate"))
            lost_value += amount
            reason = deal.get("lost_reason_name") or "Unknown"
            bucket = lost_reasons.setdefault(reason, {"name": reason, "count": 0, "value": 0.0})
            bucket["count"] += 1
            bucket["value"] += amount
        elif outcome == "disqualified":
            disqualified += 1
        else:
            open_count += 1
            outcome = "open"
        # A disqualified lead is bad sourcing, not progress against "advanced".
        if outcome != "disqualified" and (
            _is_advanced(deal, outcome, pool_max) or deal.get("id") in booked_ids
        ):
            advanced_ids.add(deal.get("id"))
        key = deal.get("stage") or ""
        stage = by_stage.setdefault(key, {
            "key": key,
            "label": deal.get("stage_label") or key or "—",
            "position": deal.get("stage_position") if deal.get("stage_position") is not None else 0,
            "count": 0,
        })
        stage["count"] += 1

    total = len(deals)
    qualified_leads = total - disqualified
    stage_rows = sorted(by_stage.values(), key=lambda row: (row["position"], row["key"]))
    for row in stage_rows:
        row.pop("position", None)

    closed = won + lost
    if closed:
        win_rate = won / closed
        ratio = f"{won} : {lost}"
        label = ratio
    else:
        win_rate = None
        ratio = None
        label = "no closed deals"
    disqualification_rate = (disqualified / total) if total else None

    actuals = {
        "leads": qualified_leads,
        "contacts": len(contacted_ids),
        "conversations": len(conversation_ids),
        "advanced": len(advanced_ids),
        "won": won,
        "won_value": won_value,
    }
    pacing_targets = {}
    inputs = {
        "results.total": "Deals carrying this campaign's tag, including lost.",
        "results.contacted": (
            "Deals with a call, email, or meeting, or a logged call outcome."
        ),
        "results.not_contacted": "Total minus contacted.",
        "results.conversations": (
            "Deals with a meeting, or a call outcome of warm, meeting scheduled, or won."
        ),
        "results.advanced": (
            "Deals that are won, have a meeting-scheduled call outcome, or sit in a "
            "stage after the qualified pool that is not nurture and not a lost stage."
        ),
        "results.open": "Deals whose outcome is open.",
        "results.won": "Deals whose outcome is won.",
        "results.lost": "Deals whose outcome is lost. Disqualified is separate.",
        "results.disqualified": "Deals whose outcome is disqualified.",
        "results.qualified_leads": "Total minus disqualified. This is the lead-count actual.",
        "results.won_value": "Sum of value_estimate on won deals.",
        "results.lost_value": "Sum of value_estimate on lost deals.",
        "win_loss.label": "Won to lost. Disqualified and open deals are excluded.",
        "win_loss.win_rate": "Won divided by won plus lost. Blank when nothing is closed.",
        "win_loss.ratio": "Won to lost.",
        "win_loss.disqualification_rate": "Disqualified divided by every deal in the campaign.",
        "win_loss.won_value": "Sum of value_estimate on won deals.",
        "win_loss.lost_value": "Sum of value_estimate on lost deals.",
        "pacing.summary.pace": RATE_NOTE,
    }
    for row in stage_rows:
        inputs[f"results.by_stage.{row['key']}"] = f"Deals currently in stage {row['key']}."

    primary_key = None
    primary_pct = None
    for key in _TARGET_ORDER:
        target = targets.get(key)
        if target is None:
            continue
        actual = actuals[key]
        try:
            target_number = float(target)
        except (TypeError, ValueError):
            continue
        remaining = max(target_number - actual, 0)
        pct = (actual / target_number) if target_number else None
        entry = {
            "target": target_number if key == "won_value" else int(target_number),
            "actual": actual,
            "remaining": remaining,
            "pct_complete": pct,
            "pace": None,
            "projected": None,
        }
        for field in _RATE_FIELDS:
            if field in ("pace", "projected"):
                continue
            entry[field] = None
            inputs[f"pacing.targets.{key}.{field}"] = RATE_NOTE
        inputs[f"pacing.targets.{key}.pace"] = RATE_NOTE
        inputs[f"pacing.targets.{key}.projected"] = RATE_NOTE
        inputs[f"pacing.targets.{key}.actual"] = _actual_input(key, actual)
        inputs[f"pacing.targets.{key}.remaining"] = f"target {target_number:g} minus actual {actual:g}"
        inputs[f"pacing.targets.{key}.pct_complete"] = (
            f"actual {actual:g} / target {target_number:g}"
            if target_number
            else "target is 0"
        )
        pacing_targets[key] = entry
        if primary_key is None:
            primary_key = key
            primary_pct = pct

    if primary_key:
        inputs["pacing.summary.pct_complete"] = (
            f"{primary_key}: " + inputs[f"pacing.targets.{primary_key}.pct_complete"]
        )
    else:
        inputs["pacing.summary.pct_complete"] = "No targets set."

    window = _window(campaign or {}, today)
    for field in ("days_elapsed", "days_remaining", "days_total", "time_elapsed_pct"):
        inputs[f"pacing.{field}"] = window["inputs"][field]
    window.pop("inputs", None)

    lost_by_reason = sorted(lost_reasons.values(), key=lambda item: (-item["count"], item["name"]))
    for item in lost_by_reason:
        inputs[f"win_loss.lost_by_reason.{item['name']}"] = (
            f"{item['count']} lost with this reason; value is the sum of their estimates."
        )

    grain = period if period in ("week", "month", "year") else None
    return {
        "results": {
            "total": total,
            "by_stage": stage_rows,
            "contacted": len(contacted_ids),
            "not_contacted": total - len(contacted_ids),
            "conversations": len(conversation_ids),
            "advanced": len(advanced_ids),
            "open": open_count,
            "won": won,
            "lost": lost,
            "disqualified": disqualified,
            "qualified_leads": qualified_leads,
            "won_value": won_value,
            "lost_value": lost_value,
        },
        "win_loss": {
            "won": won,
            "lost": lost,
            "disqualified": disqualified,
            "open": open_count,
            "closed": closed,
            "ratio": ratio,
            "win_rate": win_rate,
            "label": label,
            "disqualification_rate": disqualification_rate,
            "lost_by_reason": lost_by_reason,
            "won_value": won_value,
            "lost_value": lost_value,
        },
        "pacing": {
            **window,
            "timezone": getattr(tz, "key", None) or (str(tz) if tz else "UTC"),
            "summary": {
                "primary_target": primary_key,
                "pct_complete": primary_pct,
                "pace": None,
            },
            "targets": pacing_targets,
        },
        "periods": {
            "grain": grain,
            "available": False,
            "rows": [],
            "note": (
                "Period rows (week, month, and year) are calculated by the "
                "campaign metrics module. The grouping above is ready for "
                "those rows."
            ),
        },
        "inputs": inputs,
    }


def _is_advanced(deal, outcome, pool_max) -> bool:
    if outcome == "won" or deal.get("is_won_stage"):
        return True
    if deal.get("is_lost_stage") or deal.get("triggers_nurture"):
        return False
    if pool_max is None:
        return False
    position = deal.get("stage_position")
    if position is None:
        return False
    return position > pool_max


def _amount(value) -> float:
    if value in (None, ""):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number < 0 or number != number or number in (float("inf"), float("-inf")):
        return 0.0
    return number


def _actual_input(key, actual) -> str:
    if key == "leads":
        return f"{actual:g} deals, not counting disqualified."
    if key == "contacts":
        return f"{actual:g} deals contacted."
    if key == "conversations":
        return f"{actual:g} deals with a conversation."
    if key == "advanced":
        return f"{actual:g} deals advanced."
    if key == "won":
        return f"{actual:g} deals won."
    return f"Won value so far is {actual:g}."


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _parse_date(value):
    if not value:
        return None
    text = str(value).strip()[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def _window(campaign, today):
    start = _parse_date(campaign.get("start_date"))
    deadline = _parse_date(campaign.get("deadline"))
    info = {
        "today": today.isoformat() if today else None,
        "start_date": start.isoformat() if start else None,
        "deadline": deadline.isoformat() if deadline else None,
        "has_deadline": deadline is not None,
        "deadline_passed": bool(deadline and today and today > deadline),
        "days_elapsed": None,
        "days_remaining": None,
        "days_total": None,
        "time_elapsed_pct": None,
        "inputs": {},
    }
    if start and deadline and deadline >= start and today:
        # Inclusive calendar days. Monday through Friday is 5, not 4.
        # A deadline that is today is the last day, not a missed deadline.
        total = (deadline - start).days + 1
        if today < start:
            elapsed = 0
        elif today > deadline:
            elapsed = total
        else:
            elapsed = (today - start).days + 1
        info["days_total"] = total
        info["days_elapsed"] = elapsed
        info["days_remaining"] = total - elapsed
        info["time_elapsed_pct"] = (elapsed / total) if total else None
        info["inputs"]["days_total"] = (
            f"Inclusive days from {start.isoformat()} through {deadline.isoformat()}."
        )
        info["inputs"]["days_elapsed"] = (
            f"Inclusive days from {start.isoformat()} through {today.isoformat()}, "
            f"capped to the window."
        )
        info["inputs"]["days_remaining"] = "Window length minus days elapsed."
        info["inputs"]["time_elapsed_pct"] = "Days elapsed divided by the window length."
    else:
        if deadline and today and today <= deadline:
            info["days_remaining"] = (deadline - today).days + 1
        elif deadline and today and today > deadline:
            info["days_remaining"] = 0
        missing = "No start date and deadline, so there is no campaign window."
        if deadline and not start:
            missing = "No start date, so elapsed time and the window length are blank."
        elif start and not deadline:
            missing = "No deadline, so there is no required pace and no window length."
        for field in ("days_elapsed", "days_remaining", "days_total", "time_elapsed_pct"):
            info["inputs"][field] = missing
        if info["days_remaining"] is not None and deadline:
            info["inputs"]["days_remaining"] = (
                f"Inclusive days from {today.isoformat()} through {deadline.isoformat()}."
                if today and today <= deadline
                else "The deadline is in the past."
            )
    return info
