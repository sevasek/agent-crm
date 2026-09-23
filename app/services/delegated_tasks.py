"""Deal-scoped delegated tasks — a sales agent hands work to another agent or a human via MCP.

Not a project manager. One row is one piece of deal-scoped work (a reminder,
a send, a follow-up). Completing it writes the deal timeline so the operator
sees the result. The default-owner agent (DELEGATE_DEFAULT_OWNER, "agent" by
default) is notified by webhook (when configured) and can also poll
list_delegated_tasks(owner=<default owner>, status=delegated).
"""
import logging
import os
from datetime import datetime

import httpx

from app.database import get_db
from app.services.activities import log_activity
from app.services.auth import clean_owner_key, sanitize_text
from app.services.deals import get_deal
from app.services.partners import get_partner
from app.services.staleness import operator_today, parse_action_date

logger = logging.getLogger(__name__)

TASK_STATUSES = ("proposed", "delegated", "done", "cancelled")
DEFAULT_STATUS = "delegated"
OPEN_STATUSES = ("proposed", "delegated")
NOTIFY_STATUS = "delegated"


def default_owner():
    """Owner slug that gets the webhook and the 'delegated' default status."""
    return clean_owner_key(os.getenv("DELEGATE_DEFAULT_OWNER")) or "agent"


def clean_task_owner(value, default=None):
    if value is None or value == "":
        return default
    return clean_owner_key(value) or default


def clean_task_status(value, default=None):
    if value is None or value == "":
        return default
    text = str(value).strip().lower()
    if text in TASK_STATUSES:
        return text
    return default


def _clean_due_date(value):
    if value in (None, ""):
        return None
    parsed = parse_action_date(value)
    return parsed.isoformat() if parsed else None


def _now():
    return datetime.utcnow().isoformat()


def get_task(task_id: int):
    if not task_id:
        return None
    with get_db() as db:
        row = db.execute("SELECT * FROM delegated_tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else None


def list_tasks(owner=None, status=None, deal_id=None, due_only=False, today=None):
    query = "SELECT * FROM delegated_tasks WHERE 1=1"
    params = []
    owner_key = clean_task_owner(owner)
    if owner_key:
        query += " AND owner = ?"
        params.append(owner_key)
    status_key = clean_task_status(status)
    if status_key:
        query += " AND status = ?"
        params.append(status_key)
    if deal_id:
        query += " AND deal_id = ?"
        params.append(deal_id)
    query += " ORDER BY CASE WHEN due_date IS NULL OR TRIM(due_date) = '' THEN 1 ELSE 0 END, due_date, id"
    with get_db() as db:
        rows = [dict(r) for r in db.execute(query, params).fetchall()]
    if due_only:
        today = today or operator_today()
        due = []
        for row in rows:
            action_date = parse_action_date(row.get("due_date"))
            if action_date is not None and action_date <= today:
                due.append(row)
        return due
    return rows


def find_open_task(deal_id: int, title: str, owner: str):
    title = (title or "").strip()
    owner = clean_task_owner(owner)
    if not deal_id or not title or not owner:
        return None
    placeholders = ",".join("?" * len(OPEN_STATUSES))
    with get_db() as db:
        row = db.execute(
            f"""SELECT * FROM delegated_tasks
                WHERE deal_id = ? AND lower(title) = lower(?) AND owner = ?
                  AND status IN ({placeholders})
                ORDER BY id DESC LIMIT 1""",
            (deal_id, title, owner, *OPEN_STATUSES),
        ).fetchone()
        return dict(row) if row else None


def _public_base():
    return (os.getenv("BASE_URL") or "http://localhost:8000").rstrip("/")


def crm_links(deal_id: int, partner_id: int):
    base = _public_base()
    return {
        "partner": f"{base}/partners/{partner_id}",
        "deal": f"{base}/deals/{deal_id}/edit",
        "call": f"{base}/deals/{deal_id}/call",
    }


_WEBHOOK_ERROR_MAX = 500


def _write_webhook_log(task_id, *, attempted=False, notified=False, last_error=None):
    """Persist webhook delivery fields. Never raises past the caller."""
    now = _now()
    error = None
    if last_error:
        error = sanitize_text(str(last_error), max_len=_WEBHOOK_ERROR_MAX) or "webhook_failed"
    with get_db() as db:
        if notified:
            db.execute(
                """UPDATE delegated_tasks
                   SET webhook_notified_at = ?,
                       webhook_last_attempt_at = ?,
                       webhook_last_error = NULL,
                       updated_at = ?
                   WHERE id = ?""",
                (now, now, now, task_id),
            )
        elif attempted:
            db.execute(
                """UPDATE delegated_tasks
                   SET webhook_last_attempt_at = ?,
                       webhook_last_error = ?,
                       updated_at = ?
                   WHERE id = ?""",
                (now, error, now, task_id),
            )
        else:
            db.execute(
                """UPDATE delegated_tasks
                   SET webhook_last_error = ?,
                       updated_at = ?
                   WHERE id = ?""",
                (error, now, task_id),
            )
        db.commit()
    return now


def webhook_payload(task: dict, partner_name: str = ""):
    partner_id = task["partner_id"]
    deal_id = task["deal_id"]
    if not partner_name:
        partner = get_partner(partner_id) or {}
        partner_name = partner.get("name") or ""
    return {
        "task_id": task["id"],
        "deal_id": deal_id,
        "partner_id": partner_id,
        "partner_name": partner_name,
        "title": task.get("title") or "",
        "brief": task.get("brief") or "",
        "due_date": task.get("due_date"),
        "owner": task.get("owner"),
        "status": task.get("status"),
        "crm_links": crm_links(deal_id, partner_id),
    }


def notify_agent(task: dict) -> dict:
    """POST the task webhook once per task. Never raises.

    Skips when the URL is unset, the task is not default-owner+delegated, or a
    previous attempt already got a 2xx (idempotent). A failed POST leaves
    webhook_notified_at empty so a later update/create can retry; the receiver
    dedupes on task_id.
    """
    if not task:
        return {"notified": False, "reason": "missing"}
    if task.get("owner") != default_owner() or task.get("status") != NOTIFY_STATUS:
        return {"notified": False, "reason": "not_agent_delegated"}
    if task.get("webhook_notified_at"):
        return {"notified": False, "reason": "already_notified"}
    url = (os.getenv("TASK_WEBHOOK_URL") or "").strip()
    if not url:
        _write_webhook_log(task["id"], last_error="webhook_unconfigured")
        return {"notified": False, "reason": "webhook_unconfigured"}

    partner = get_partner(task["partner_id"]) or {}
    payload = webhook_payload(task, partner_name=partner.get("name") or "")
    headers = {"Content-Type": "application/json"}
    token = (os.getenv("TASK_WEBHOOK_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = httpx.post(
            url, json=payload, headers=headers, timeout=10.0, follow_redirects=False,
        )
        response.raise_for_status()
    except Exception as exc:
        logger.warning("Task webhook failed for task %s: %s", task.get("id"), exc)
        now = _write_webhook_log(task["id"], attempted=True, last_error=exc)
        task["webhook_last_attempt_at"] = now
        task["webhook_last_error"] = sanitize_text(str(exc), max_len=_WEBHOOK_ERROR_MAX) or "webhook_failed"
        return {"notified": False, "reason": "webhook_failed"}

    now = _write_webhook_log(task["id"], attempted=True, notified=True)
    task["webhook_notified_at"] = now
    task["webhook_last_attempt_at"] = now
    task["webhook_last_error"] = None
    return {"notified": True, "reason": "ok"}


def create_task(deal_id: int, title: str, *, brief="", owner=None, status=None,
                due_date=None, created_by=None, result_notes=""):
    """Returns (task_dict, error_code, created_bool)."""
    deal = get_deal(deal_id)
    if not deal:
        return None, "not_found", False
    title = sanitize_text(title, max_len=200)
    if not title:
        return None, "title_required", False
    if owner in (None, ""):
        owner_key = default_owner()
    else:
        owner_key = clean_task_owner(owner)
        if owner_key is None:
            return None, "invalid_owner", False
    status_key = clean_task_status(status)
    if status is not None and status != "" and status_key is None:
        return None, "invalid_status", False
    if status_key is None:
        status_key = DEFAULT_STATUS if owner_key == default_owner() else "proposed"
    due = _clean_due_date(due_date)
    created_by_key = clean_task_owner(created_by) or sanitize_text(str(created_by or "mcp"), max_len=40).lower() or "mcp"
    brief = sanitize_text(brief, max_len=4000, allow_newlines=True) or None
    result_notes = sanitize_text(result_notes, max_len=4000, allow_newlines=True) or None

    existing = find_open_task(deal_id, title, owner_key)
    if existing:
        notify = notify_agent(existing)
        existing = get_task(existing["id"])
        existing["webhook"] = notify
        return existing, None, False

    now = _now()
    with get_db() as db:
        db.execute(
            """INSERT INTO delegated_tasks (
                   deal_id, partner_id, title, brief, owner, status, due_date,
                   created_by, result_notes, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                deal_id, deal["partner_id"], title, brief, owner_key, status_key, due,
                created_by_key, result_notes, now, now,
            ),
        )
        db.commit()
        task_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    log_activity(
        deal["partner_id"],
        "system",
        f"Delegated task created: {title} → {owner_key}",
        deal_id=deal_id,
    )
    task = get_task(task_id)
    notify = notify_agent(task)
    task = get_task(task_id)
    task["webhook"] = notify
    return task, None, True


def update_task(task_id: int, **fields):
    """Returns (task_dict, error_code)."""
    task = get_task(task_id)
    if not task:
        return None, "not_found"
    allowed = {"status", "due_date", "brief", "result_notes", "owner", "title"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if "title" in updates:
        title = sanitize_text(updates["title"], max_len=200)
        if not title:
            return None, "title_required"
        updates["title"] = title
    if "brief" in updates:
        updates["brief"] = sanitize_text(updates["brief"], max_len=4000, allow_newlines=True) or None
    if "result_notes" in updates:
        updates["result_notes"] = sanitize_text(updates["result_notes"], max_len=4000, allow_newlines=True) or None
    if "due_date" in updates:
        updates["due_date"] = _clean_due_date(updates["due_date"])
    if "owner" in updates:
        owner_key = clean_task_owner(updates["owner"])
        if owner_key is None:
            return None, "invalid_owner"
        updates["owner"] = owner_key
    if "status" in updates:
        status_key = clean_task_status(updates["status"])
        if status_key is None:
            return None, "invalid_status"
        updates["status"] = status_key
    if not updates:
        return task, None
    updates["updated_at"] = _now()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [task_id]
    with get_db() as db:
        db.execute(f"UPDATE delegated_tasks SET {set_clause} WHERE id = ?", values)
        db.commit()
    task = get_task(task_id)
    notify = notify_agent(task)
    task = get_task(task_id)
    task["webhook"] = notify
    return task, None


def complete_task(task_id: int, result_notes: str = ""):
    """Mark done, write result_notes, log a deal timeline note. Idempotent if already done."""
    task = get_task(task_id)
    if not task:
        return None, "not_found"
    notes = sanitize_text(result_notes, max_len=4000, allow_newlines=True) or task.get("result_notes")
    already_done = task.get("status") == "done"
    if not already_done:
        now = _now()
        with get_db() as db:
            db.execute(
                """UPDATE delegated_tasks
                   SET status = 'done', result_notes = ?, updated_at = ?
                   WHERE id = ?""",
                (notes, now, task_id),
            )
            db.commit()
        body = f"Delegated task done: {task['title']}"
        if notes:
            body += f" — {notes}"
        log_activity(task["partner_id"], "note", body, deal_id=task["deal_id"])
    elif notes and notes != (task.get("result_notes") or ""):
        now = _now()
        with get_db() as db:
            db.execute(
                "UPDATE delegated_tasks SET result_notes = ?, updated_at = ? WHERE id = ?",
                (notes, now, task_id),
            )
            db.commit()
    task = get_task(task_id)
    task["webhook"] = {"notified": False, "reason": "complete"}
    return task, None
