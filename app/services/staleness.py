"""Follow-up reminder / staleness gate on `deals.next_action_date`.

Same pattern as the daily engagement-gate used elsewhere in our stack, ported
onto this app's deals: a daily check against an explicit clock (here,
`next_action_date`) rather than a learned reminder.
Open deals whose date is today or earlier fail the gate; a one-shot system
activity is logged per due-date so re-running the cron is idempotent.

"Today" is the operator's calendar (UTC by default, overridable
via `TZ`), not the container's implicit UTC.

See docs/DATA_MODEL.md → Follow-ups.
"""

import logging
import os
from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.database import get_db
from app.services.auth import sanitize_text
from app.services.deals import list_deals
from app.services import pipeline_stages

DUE = "due"
OVERDUE = "overdue"
DUE_GATE_PREFIX = "due-gate:"

logger = logging.getLogger(__name__)


def operator_today():
    """Calendar date in the operator timezone — not UTC, not the host cron's TZ."""
    name = os.getenv("TZ", "UTC")
    try:
        tz = ZoneInfo(name)
    except (ZoneInfoNotFoundError, Exception):
        logger.warning("Invalid TZ %r; using UTC for the staleness gate", name)
        tz = ZoneInfo("UTC")
    return datetime.now(tz).date()


def parse_action_date(value):
    """`YYYY-MM-DD`, optionally with a time suffix. None if missing/garbage."""
    if not value:
        return None
    text = str(value).strip().replace("/", "-")[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def due_gate_key(action_date: date, status: str) -> str:
    """Stable idempotency token, independent of the human-readable sentence."""
    return f"{DUE_GATE_PREFIX}{action_date.isoformat()}:{status}"


def due_activity_prose(action_date: date, status: str) -> str:
    iso = action_date.isoformat()
    if status == DUE:
        return f"Follow-up due today ({iso})"
    return f"Follow-up overdue: next action was due {iso}"


def due_activity_body(action_date: date, status: str) -> str:
    return f"{due_gate_key(action_date, status)}\n{due_activity_prose(action_date, status)}"


def activity_display_body(body: str) -> str:
    """Hide the due-gate marker on the partner timeline."""
    if not body:
        return ""
    first, _, rest = body.partition("\n")
    if first.startswith(DUE_GATE_PREFIX):
        return rest
    return body


def deal_due_status(deal, today=None, action_date=None, closed_stage_keys=None):
    """`due` / `overdue` / None. Closed deals and undated deals are not gated."""
    if closed_stage_keys is None:
        closed_stage_keys = pipeline_stages.closed_stage_keys()
    if deal.get("stage") in closed_stage_keys:
        return None
    if action_date is None:
        action_date = parse_action_date(deal.get("next_action_date"))
    if action_date is None:
        return None
    today = today or operator_today()
    if action_date < today:
        return OVERDUE
    if action_date == today:
        return DUE
    return None


def annotate_deal(deal, today=None, closed_stage_keys=None):
    today = today or operator_today()
    if closed_stage_keys is None:
        closed_stage_keys = pipeline_stages.closed_stage_keys()
    action_date = parse_action_date(deal.get("next_action_date"))
    status = deal_due_status(deal, today=today, action_date=action_date, closed_stage_keys=closed_stage_keys)
    days_overdue = (today - action_date).days if action_date and status else None
    annotated = dict(deal)
    annotated["action_date"] = action_date
    annotated["due_status"] = status
    annotated["days_overdue"] = days_overdue
    return annotated


def annotate_deals(deals, today=None):
    today = today or operator_today()
    closed_stage_keys = pipeline_stages.closed_stage_keys()
    return [annotate_deal(d, today=today, closed_stage_keys=closed_stage_keys) for d in deals]


def list_due_deals(today=None, stage=None, owner_key=None, service_slug=None):
    """Open deals whose `next_action_date` is today or earlier, oldest date first.

    `stage` is applied in SQL via list_deals, so `?due=1&stage=qualified`
    stays qualified-only. `owner_key` / `service_slug` likewise.
    """
    today = today or operator_today()
    closed_stage_keys = pipeline_stages.closed_stage_keys()
    due = []
    for deal in list_deals(stage=stage or None, owner_key=owner_key, service_slug=service_slug):
        annotated = annotate_deal(deal, today=today, closed_stage_keys=closed_stage_keys)
        if annotated["due_status"]:
            due.append(annotated)
    due.sort(key=lambda d: (
        d.get("action_date") or date.max,
        (d.get("partner_name") or "").lower(),
        d["id"],
    ))
    return due


def _already_logged(db, deal_id: int, action_date: date, status: str) -> bool:
    key = due_gate_key(action_date, status)
    legacy = due_activity_prose(action_date, status)
    row = db.execute(
        """SELECT id FROM activities
           WHERE deal_id = ? AND type = 'system'
             AND (body LIKE ? OR body = ?)
           LIMIT 1""",
        (deal_id, key + "%", legacy),
    ).fetchone()
    return row is not None


def run_staleness_gate(today=None):
    """Log a one-shot activity per due/overdue deal and return the current set.

    Re-running against the same `next_action_date` does not duplicate the
    activity. Changing the date (or the deal becoming due then overdue on a
    later day) produces a new key, so a new activity is allowed. Dedup is the
    `due-gate:` marker, not the display sentence. Check+insert share one
    IMMEDIATE transaction so overlapping cron/manual runs cannot double-log.
    """
    today = today or operator_today()
    due = list_due_deals(today=today)
    logged_ids = []
    for deal in due:
        action_date = deal.get("action_date") or parse_action_date(deal.get("next_action_date"))
        if action_date is None:
            continue
        status = deal["due_status"]
        body = due_activity_body(action_date, status)
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            if _already_logged(db, deal["id"], action_date, status):
                continue
            db.execute(
                """INSERT INTO activities (partner_id, deal_id, type, body)
                   VALUES (?, ?, 'system', ?)""",
                (
                    deal["partner_id"],
                    deal["id"],
                    sanitize_text(body, max_len=4000, allow_newlines=True),
                ),
            )
            db.commit()
        logged_ids.append(deal["id"])
    return {"due": due, "logged_ids": logged_ids}
