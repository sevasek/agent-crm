"""Health Check ladder side effects, ported from sevasek/crm.

Off unless HC_LADDER_ENABLED is true. agent-crm is a generic product and
is also deployed for other clients, so this sevasek workflow must not run
unless an operator turns it on.

When enabled, behaviour matches the live CRM:

* Moving a health-check deal to ``hc_paid`` creates Willow's open task
  "Send HC kickoff" (owner ``willow``, status ``delegated``). A second
  move while that task is still open does not create another.
* Moving a health-check deal to ``hc_presented`` spawns two child deals
  on the same partner: automation-delivery and automations-support.
  ``source`` is ``hc-spawn:<parent id>``, ``parent_deal_id`` is the
  Health Check deal, and no offer or price is attached. An open child
  for that service is left alone. IT Support is not spawned.
* Other services are ignored, even if someone moves them onto an
  ``hc_*`` stage key.
"""
import logging
import os

from app.services.catalog import get_service, get_service_by_slug
from app.services.deals import create_deal, list_deals
from app.services.delegated_tasks import create_task
from app.services import pipeline_stages

logger = logging.getLogger(__name__)

ENABLED_ENV = "HC_LADDER_ENABLED"
HC_SERVICE_SLUG = "health-check"
PAID_STAGE = "hc_paid"
PRESENTED_STAGE = "hc_presented"
KICKOFF_TITLE = "Send HC kickoff"
KICKOFF_OWNER = "willow"
KICKOFF_BRIEF = "Paid Health Check. Send the kickoff."
# Order is part of the contract: delivery, then support. IT Support is omitted.
CHILD_SERVICE_SLUGS = ("automation-delivery", "automations-support")
EXCLUDED_SERVICE_SLUG = "it-support"
SPAWN_SOURCE_PREFIX = "hc-spawn:"

_TRUE = {"1", "true", "yes", "on"}


def ladder_enabled() -> bool:
    return (os.getenv(ENABLED_ENV) or "").strip().lower() in _TRUE


def spawn_source(parent_deal_id: int) -> str:
    return f"{SPAWN_SOURCE_PREFIX}{int(parent_deal_id)}"


def ladder_instruction() -> str:
    """Sentence appended to MCP instructions when the ladder is on."""
    return (
        "Health Check (service_slug=health-check): hc_paid creates a Willow "
        "'Send HC kickoff' task (idempotent while open); hc_presented spawns "
        "Automation Delivery + Automations Support child deals on the same "
        "partner (source hc-spawn:<id>, no price, idempotent while those "
        "children are open). IT Support is not auto-spawned."
    )


def _open_child(parent_id: int, service_id: int):
    closed = pipeline_stages.closed_stage_keys()
    for child in list_deals(parent_deal_id=parent_id, service_id=service_id):
        if child.get("stage") not in closed:
            return child
    return None


def _ensure_kickoff(deal: dict) -> None:
    task, error, created = create_task(
        deal["id"],
        KICKOFF_TITLE,
        brief=KICKOFF_BRIEF,
        owner=KICKOFF_OWNER,
        status="delegated",
        created_by="hc-ladder",
    )
    if error:
        logger.warning(
            "HC ladder kickoff failed for deal %s: %s", deal.get("id"), error,
        )
        return
    if created:
        logger.info(
            "HC ladder created kickoff task %s on deal %s",
            task.get("id") if task else None,
            deal.get("id"),
        )


def _spawn_children(deal: dict) -> None:
    if EXCLUDED_SERVICE_SLUG in CHILD_SERVICE_SLUGS:
        raise RuntimeError("IT Support must not be auto-spawned")
    parent_id = deal["id"]
    partner_id = deal["partner_id"]
    source = spawn_source(parent_id)
    for slug in CHILD_SERVICE_SLUGS:
        service = get_service_by_slug(slug)
        if not service:
            logger.warning(
                "HC ladder skipped %s for deal %s: service is not in the catalog",
                slug,
                parent_id,
            )
            continue
        existing = _open_child(parent_id, service["id"])
        if existing:
            logger.info(
                "HC ladder left open %s child %s on deal %s",
                slug,
                existing.get("id"),
                parent_id,
            )
            continue
        child_id = create_deal(
            partner_id,
            service["id"],
            source=source,
            parent_deal_id=parent_id,
            attach_default_offer=False,
            value_estimate=None,
            created_note=f"Health Check ladder spawned {slug} from deal {parent_id}",
        )
        logger.info(
            "HC ladder spawned %s deal %s from deal %s", slug, child_id, parent_id,
        )


def apply_hc_ladder(deal: dict, old_stage: str, new_stage: str) -> None:
    """Run ladder side effects after a stage change has been committed.

    Never raises: a catalog or task failure must not undo the stage move.
    """
    if not ladder_enabled():
        return
    if not deal or old_stage == new_stage:
        return
    if new_stage not in (PAID_STAGE, PRESENTED_STAGE):
        return
    service = get_service(deal.get("service_id"))
    if not service or service.get("slug") != HC_SERVICE_SLUG:
        return
    try:
        if new_stage == PAID_STAGE:
            _ensure_kickoff(deal)
        else:
            _spawn_children(deal)
    except Exception:
        logger.exception("HC ladder failed for deal %s", deal.get("id"))
