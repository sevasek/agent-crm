"""Operator-defined automations that run when a deal enters a stage.

Nothing is seeded. A new database has an empty table, so a stage change
does only what it always did until someone creates an automation in the
admin UI or over MCP.

An automation has a name the operator chooses, a stage, an optional
service and/or offer scope, and one or two actions:

* ``delegated_task`` — one task (owner, title, brief, optional due-in-days).
  Title and brief may contain ``{partner_name}``, ``{deal_id}``, ``{stage}``,
  ``{service_name}``, ``{service_slug}``, ``{automation_name}``.
* ``spawn_child_deals`` — child deals for chosen services, same partner,
  ``parent_deal_id`` set, ``source`` ``automation:<automation id>``, no offer
  and no price.

Re-entering the stage does not open a second task while one with the same
title and owner is still open, and does not open a second child while one
for that service is still open. A finished task or a closed child does not
block a new one. Creating a deal that is already in the stage does not run
automations; only a stage change does.
"""
import json
import logging
from datetime import timedelta

from app.database import IntegrityConflict, get_db
from app.services.auth import clean_owner_key, sanitize_text
from app.services.catalog import get_service, get_service_by_slug
from app.services.offers import get_offer
from app.services import pipeline_stages
from app.services.staleness import operator_today

logger = logging.getLogger(__name__)

ACTION_TASK = "delegated_task"
ACTION_CHILDREN = "spawn_child_deals"
ACTION_TYPES = (ACTION_TASK, ACTION_CHILDREN)
_UNSET = object()
_MAX_DUE_DAYS = 3650
_PLACEHOLDERS = (
    "partner_name",
    "deal_id",
    "stage",
    "service_name",
    "service_slug",
    "automation_name",
)


def automation_source(automation_id: int) -> str:
    return f"automation:{int(automation_id)}"


def _now():
    from datetime import datetime
    return datetime.utcnow().isoformat()


def render_template(text, values: dict) -> str:
    """Replace ``{name}`` placeholders. Unknown braces are left as written."""
    rendered = text or ""
    for key in _PLACEHOLDERS:
        rendered = rendered.replace("{" + key + "}", str(values.get(key, "") or ""))
    return rendered


def _due_in_days(value):
    if value is None or value == "":
        return None, None
    if isinstance(value, bool):
        return None, "invalid_due"
    if isinstance(value, int):
        days = value
    elif isinstance(value, str) and value.strip().isdigit():
        days = int(value.strip())
    else:
        return None, "invalid_due"
    if days < 0 or days > _MAX_DUE_DAYS:
        return None, "invalid_due"
    return days, None


def _resolve_service_scope(service_id=_UNSET, service_slug=_UNSET):
    """Return (service_id or None, error). ``_UNSET`` means the caller omitted it."""
    sent_id = service_id is not _UNSET
    sent_slug = service_slug is not _UNSET
    if not sent_id and not sent_slug:
        return _UNSET, None
    resolved = None
    if sent_slug:
        slug = (service_slug or "").strip().lower()
        if not slug:
            resolved = None
        else:
            service = get_service_by_slug(slug)
            if not service:
                return None, f"unknown_service:{slug}"
            resolved = service["id"]
    if sent_id:
        if service_id in (None, "", 0, "0"):
            by_id = None
        else:
            try:
                by_id = int(service_id)
            except (TypeError, ValueError):
                return None, "service_not_found"
            if by_id < 1 or not get_service(by_id):
                return None, "service_not_found"
        if sent_slug and resolved is not None and by_id is not None and resolved != by_id:
            return None, "scope_conflict"
        if not sent_slug or resolved is None:
            resolved = by_id
    return resolved, None


def _resolve_offer(offer_id):
    if offer_id in (None, "", 0, "0"):
        return None, None
    try:
        parsed = int(offer_id)
    except (TypeError, ValueError):
        return None, "offer_not_found"
    if parsed < 1 or not get_offer(parsed):
        return None, "offer_not_found"
    return parsed, None


def _clean_actions(actions):
    if not isinstance(actions, list) or not actions:
        return None, "actions_required"
    cleaned = []
    seen = set()
    for raw in actions:
        if not isinstance(raw, dict):
            return None, "invalid_action"
        action_type = (raw.get("type") or raw.get("action_type") or "").strip()
        if action_type not in ACTION_TYPES:
            return None, "invalid_action"
        if action_type in seen:
            return None, "duplicate_action"
        seen.add(action_type)
        if action_type == ACTION_TASK:
            title = sanitize_text(raw.get("title") or "", max_len=200)
            if not title:
                return None, "title_required"
            owner = clean_owner_key(raw.get("owner"))
            if not owner:
                return None, "invalid_owner"
            brief = sanitize_text(raw.get("brief") or "", max_len=4000, allow_newlines=True)
            days, due_error = _due_in_days(raw.get("due_in_days"))
            if due_error:
                return None, due_error
            cleaned.append({
                "type": ACTION_TASK,
                "config": {
                    "owner": owner,
                    "title": title,
                    "brief": brief,
                    "due_in_days": days,
                },
            })
            continue
        service_ids, error = _child_service_ids(raw)
        if error:
            return None, error
        cleaned.append({
            "type": ACTION_CHILDREN,
            "config": {"service_ids": service_ids},
        })
    if not cleaned:
        return None, "actions_required"
    return cleaned, None


def _child_service_ids(raw):
    ordered = []
    slugs = raw.get("service_slugs")
    if slugs is not None:
        if isinstance(slugs, str):
            slugs = [part.strip() for part in slugs.split(",") if part.strip()]
        if not isinstance(slugs, list):
            return None, "unknown_service:"
        for slug in slugs:
            text = str(slug or "").strip().lower()
            if not text:
                continue
            service = get_service_by_slug(text)
            if not service:
                return None, f"unknown_service:{text}"
            if service["id"] not in ordered:
                ordered.append(service["id"])
    ids = raw.get("service_ids")
    if ids is not None:
        if not isinstance(ids, list):
            return None, "service_not_found"
        for value in ids:
            try:
                service_id = int(value)
            except (TypeError, ValueError):
                return None, "service_not_found"
            if service_id < 1 or not get_service(service_id):
                return None, "service_not_found"
            if service_id not in ordered:
                ordered.append(service_id)
    if not ordered:
        return None, "actions_required"
    return ordered, None


def _insert_actions(db, automation_id, actions):
    for position, action in enumerate(actions):
        db.execute(
            """INSERT INTO stage_automation_actions
                   (automation_id, position, action_type, config)
               VALUES (?, ?, ?, ?)""",
            (
                automation_id,
                position,
                action["type"],
                json.dumps(action["config"], sort_keys=True),
            ),
        )


def _load_actions(db, automation_ids):
    if not automation_ids:
        return {}
    placeholders = ",".join("?" * len(automation_ids))
    rows = db.execute(
        f"""SELECT * FROM stage_automation_actions
            WHERE automation_id IN ({placeholders})
            ORDER BY position, id""",
        tuple(automation_ids),
    ).fetchall()
    grouped = {automation_id: [] for automation_id in automation_ids}
    for row in rows:
        grouped.setdefault(row["automation_id"], []).append(row)
    return grouped


def _public_action(row):
    try:
        config = json.loads(row["config"] or "{}")
    except json.JSONDecodeError:
        config = {}
    if not isinstance(config, dict):
        config = {}
    if row["action_type"] == ACTION_TASK:
        return {
            "type": ACTION_TASK,
            "owner": config.get("owner") or "",
            "title": config.get("title") or "",
            "brief": config.get("brief") or "",
            "due_in_days": config.get("due_in_days"),
        }
    service_ids = []
    service_slugs = []
    for value in config.get("service_ids") or []:
        try:
            service_id = int(value)
        except (TypeError, ValueError):
            continue
        service_ids.append(service_id)
        service = get_service(service_id)
        service_slugs.append(service["slug"] if service else None)
    return {
        "type": ACTION_CHILDREN,
        "service_ids": service_ids,
        "service_slugs": service_slugs,
    }


def _public(row, action_rows):
    service = get_service(row["service_id"]) if row["service_id"] else None
    offer = get_offer(row["offer_id"]) if row["offer_id"] else None
    warnings = []
    if row["service_id"] and not service:
        warnings.append("The scoped service no longer exists, so this automation will not run.")
    if row["offer_id"] and not offer:
        warnings.append("The scoped offer no longer exists, so this automation will not run.")
    actions = [_public_action(action) for action in action_rows]
    for action in actions:
        if action["type"] != ACTION_CHILDREN:
            continue
        missing = [
            service_id
            for service_id, slug in zip(action["service_ids"], action["service_slugs"])
            if slug is None
        ]
        if missing:
            warnings.append(
                "A child service no longer exists and will be skipped: "
                + ", ".join(f"#{service_id}" for service_id in missing)
                + "."
            )
    return {
        "id": row["id"],
        "name": row["name"],
        "enabled": bool(row["enabled"]),
        "stage_key": row["stage_key"],
        "service_id": row["service_id"],
        "service_slug": service["slug"] if service else None,
        "offer_id": row["offer_id"],
        "actions": actions,
        "warnings": warnings,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_automations():
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM stage_automations ORDER BY id"
        ).fetchall()
        grouped = _load_actions(db, [row["id"] for row in rows])
    return [_public(row, grouped.get(row["id"], [])) for row in rows]


def get_automation(automation_id: int):
    if not automation_id:
        return None
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM stage_automations WHERE id = ?",
            (int(automation_id),),
        ).fetchone()
        if not row:
            return None
        actions = _load_actions(db, [row["id"]]).get(row["id"], [])
    return _public(row, actions)


def get_automation_by_name(name: str):
    text = sanitize_text(name or "", max_len=80)
    if not text:
        return None
    with get_db() as db:
        row = db.execute(
            "SELECT id FROM stage_automations WHERE name = ?",
            (text,),
        ).fetchone()
    if not row:
        return None
    return get_automation(row["id"])


def _validate_header(name, stage_key, service_id, offer_id, enabled):
    cleaned_name = sanitize_text(name or "", max_len=80)
    if not cleaned_name:
        return None, "name_required"
    stage = (stage_key or "").strip()
    if not stage or not pipeline_stages.get_stage(stage):
        return None, "invalid_stage"
    if offer_id is _UNSET:
        resolved_offer = _UNSET
    else:
        resolved_offer, offer_error = _resolve_offer(offer_id)
        if offer_error:
            return None, offer_error
    resolved_service, service_error = _resolve_service_scope(service_id, _UNSET)
    # service_id here is already resolved by the caller, or _UNSET.
    if service_error:
        return None, service_error
    return {
        "name": cleaned_name,
        "stage_key": stage,
        "service_id": None if resolved_service is _UNSET else resolved_service,
        "offer_id": None if resolved_offer is _UNSET else resolved_offer,
        "enabled": 1 if enabled else 0,
    }, None


def create_automation(name, stage_key, *, service_id=None, service_slug=None,
                      offer_id=None, enabled=True, actions):
    """Returns (automation, None) or (None, error_code)."""
    resolved_service, service_error = _resolve_service_scope(service_id, service_slug)
    if service_error:
        return None, service_error
    if resolved_service is _UNSET:
        resolved_service = None
    header, error = _validate_header(name, stage_key, resolved_service, offer_id, enabled)
    if error:
        return None, error
    cleaned_actions, error = _clean_actions(actions)
    if error:
        return None, error
    if get_automation_by_name(header["name"]):
        return None, "name_taken"
    now = _now()
    try:
        with get_db() as db:
            cursor = db.execute(
                """INSERT INTO stage_automations
                       (name, enabled, stage_key, service_id, offer_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    header["name"],
                    header["enabled"],
                    header["stage_key"],
                    header["service_id"],
                    header["offer_id"],
                    now,
                    now,
                ),
            )
            automation_id = cursor.lastrowid
            _insert_actions(db, automation_id, cleaned_actions)
            db.commit()
    except IntegrityConflict:
        return None, "name_taken"
    return get_automation(automation_id), None


def update_automation(automation_id: int, *, name=_UNSET, stage_key=_UNSET,
                      service_id=_UNSET, service_slug=_UNSET, offer_id=_UNSET,
                      enabled=_UNSET, actions=_UNSET):
    """Returns (automation, None) or (None, error_code).

    Omitted fields stay as they are. Pass ``service_slug=""`` or
    ``service_id=None`` to clear a service scope, and ``offer_id=None``
    to clear an offer scope. ``actions``, when sent, replaces the list.
    """
    current = get_automation(automation_id)
    if not current:
        return None, "not_found"
    next_name = current["name"] if name is _UNSET else name
    next_stage = current["stage_key"] if stage_key is _UNSET else stage_key
    next_enabled = current["enabled"] if enabled is _UNSET else bool(enabled)
    if service_id is _UNSET and service_slug is _UNSET:
        resolved_service = current["service_id"]
    else:
        resolved_service, service_error = _resolve_service_scope(service_id, service_slug)
        if service_error:
            return None, service_error
        if resolved_service is _UNSET:
            resolved_service = current["service_id"]
    next_offer = current["offer_id"] if offer_id is _UNSET else offer_id
    header, error = _validate_header(
        next_name, next_stage, resolved_service, next_offer, next_enabled,
    )
    if error:
        return None, error
    other = get_automation_by_name(header["name"])
    if other and other["id"] != current["id"]:
        return None, "name_taken"
    cleaned_actions = None
    if actions is not _UNSET:
        cleaned_actions, error = _clean_actions(actions)
        if error:
            return None, error
    now = _now()
    try:
        with get_db() as db:
            db.execute(
                """UPDATE stage_automations
                   SET name = ?, enabled = ?, stage_key = ?, service_id = ?,
                       offer_id = ?, updated_at = ?
                   WHERE id = ?""",
                (
                    header["name"],
                    header["enabled"],
                    header["stage_key"],
                    header["service_id"],
                    header["offer_id"],
                    now,
                    current["id"],
                ),
            )
            if cleaned_actions is not None:
                db.execute(
                    "DELETE FROM stage_automation_actions WHERE automation_id = ?",
                    (current["id"],),
                )
                _insert_actions(db, current["id"], cleaned_actions)
            db.commit()
    except IntegrityConflict:
        return None, "name_taken"
    return get_automation(current["id"]), None


def delete_automation(automation_id: int):
    """Returns (True, None) or (False, error_code)."""
    current = get_automation(automation_id)
    if not current:
        return False, "not_found"
    with get_db() as db:
        db.execute(
            "DELETE FROM stage_automation_actions WHERE automation_id = ?",
            (current["id"],),
        )
        db.execute("DELETE FROM stage_automations WHERE id = ?", (current["id"],))
        db.commit()
    return True, None


def _matches(automation_row, deal) -> bool:
    service_id = automation_row["service_id"]
    if service_id:
        service = get_service(service_id)
        if not service or deal.get("service_id") != service_id:
            if not service:
                logger.warning(
                    "stage automation %s references missing service %s; not running",
                    automation_row["id"],
                    service_id,
                )
            return False
    offer_id = automation_row["offer_id"]
    if offer_id:
        offer = get_offer(offer_id)
        if not offer or deal.get("offer_id") != offer_id:
            if not offer:
                logger.warning(
                    "stage automation %s references missing offer %s; not running",
                    automation_row["id"],
                    offer_id,
                )
            return False
    return True


def _template_values(deal, stage_key, automation_name):
    from app.services.partners import get_partner

    partner = get_partner(deal["partner_id"]) or {}
    service = get_service(deal["service_id"]) or {}
    return {
        "partner_name": partner.get("name") or "",
        "deal_id": deal["id"],
        "stage": stage_key,
        "service_name": service.get("name") or "",
        "service_slug": service.get("slug") or "",
        "automation_name": automation_name,
    }


def _run_task(automation, deal, stage_key, config):
    from app.services.delegated_tasks import create_task

    values = _template_values(deal, stage_key, automation["name"])
    title = sanitize_text(render_template(config.get("title") or "", values), max_len=200)
    brief = sanitize_text(
        render_template(config.get("brief") or "", values),
        max_len=4000,
        allow_newlines=True,
    )
    owner = clean_owner_key(config.get("owner"))
    if not title or not owner:
        logger.warning(
            "stage automation %s skipped a task with no title or owner on deal %s",
            automation["id"],
            deal["id"],
        )
        return
    due = None
    days = config.get("due_in_days")
    if isinstance(days, int) and not isinstance(days, bool) and days >= 0:
        due = (operator_today() + timedelta(days=days)).isoformat()
    task, error, created = create_task(
        deal["id"],
        title,
        brief=brief,
        owner=owner,
        status="delegated",
        due_date=due,
        created_by="automation",
    )
    if error:
        logger.warning(
            "stage automation %s could not create a task on deal %s: %s",
            automation["id"],
            deal["id"],
            error,
        )
        return
    if created:
        logger.info(
            "stage automation %s created task %s on deal %s",
            automation["id"],
            task["id"] if task else None,
            deal["id"],
        )


def _open_child(parent_id, service_id):
    """An open child is active and not in an is_won stage.

    A lost child (active = 0) does not block a new child for that service.
    """
    from app.services.deals import list_deals

    won = pipeline_stages.won_stage_keys()
    for child in list_deals(
        parent_deal_id=parent_id, service_id=service_id, include_lost=True,
    ):
        if child.get("active") in (0, False):
            continue
        if child.get("stage") in won:
            continue
        return child
    return None


def _run_children(automation, deal, config):
    from app.services.deals import create_deal, validate_parent_link

    for service_id in config.get("service_ids") or []:
        try:
            service_id = int(service_id)
        except (TypeError, ValueError):
            continue
        service = get_service(service_id)
        if not service:
            logger.warning(
                "stage automation %s skipped missing child service %s on deal %s",
                automation["id"],
                service_id,
                deal["id"],
            )
            continue
        if _open_child(deal["id"], service_id):
            logger.info(
                "stage automation %s left the open %s child of deal %s",
                automation["id"],
                service["slug"],
                deal["id"],
            )
            continue
        _parent, parent_error = validate_parent_link(None, deal["id"], deal["partner_id"])
        if parent_error:
            logger.warning(
                "stage automation %s could not link a child of deal %s: %s",
                automation["id"],
                deal["id"],
                parent_error,
            )
            continue
        create_deal(
            deal["partner_id"],
            service_id,
            source=automation_source(automation["id"]),
            value_estimate=None,
            parent_deal_id=deal["id"],
            attach_default_offer=False,
            created_note=f"Opened by automation {automation['name']}",
        )


def _run(automation_row, deal, stage_key):
    automation = get_automation(automation_row["id"])
    if not automation or not automation["enabled"]:
        return
    with get_db() as db:
        action_rows = db.execute(
            """SELECT * FROM stage_automation_actions
               WHERE automation_id = ?
               ORDER BY position, id""",
            (automation["id"],),
        ).fetchall()
    for row in action_rows:
        try:
            config = json.loads(row["config"] or "{}")
        except json.JSONDecodeError:
            logger.warning("stage automation %s has unreadable action config", automation["id"])
            continue
        if not isinstance(config, dict):
            continue
        try:
            if row["action_type"] == ACTION_TASK:
                _run_task(automation, deal, stage_key, config)
            elif row["action_type"] == ACTION_CHILDREN:
                _run_children(automation, deal, config)
        except Exception:
            logger.exception(
                "stage automation %s action %s failed for deal %s",
                automation["id"],
                row["action_type"],
                deal["id"],
            )


def apply_stage_automations(deal: dict, old_stage: str, new_stage: str) -> None:
    """Run enabled automations for ``new_stage``. Never raises."""
    if not deal or not new_stage or old_stage == new_stage:
        return
    try:
        with get_db() as db:
            rows = db.execute(
                """SELECT * FROM stage_automations
                   WHERE enabled = 1 AND stage_key = ?
                   ORDER BY id""",
                (new_stage,),
            ).fetchall()
    except Exception:
        logger.exception("stage automations could not be loaded for deal %s", deal.get("id"))
        return
    for row in rows:
        if not _matches(row, deal):
            continue
        try:
            _run(row, deal, new_stage)
        except Exception:
            logger.exception(
                "stage automation %s failed for deal %s",
                row["id"],
                deal.get("id"),
            )
