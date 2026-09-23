"""Nurture hand-off via an optional outbound webhook.

The CRM does not run drip sequences. When a deal enters a stage flagged
``triggers_nurture``, this POSTs the partner to whatever tool you point
NURTURE_WEBHOOK_URL at (a mailing-list tool, an automation platform, an agent)
and logs the outcome. Unset URL = no-op.
"""
import logging
import os

import httpx

from app.services.auth import is_valid_slug

logger = logging.getLogger(__name__)


def enroll_partner_in_nurture(partner: dict, service: dict, deal_id: int = None) -> tuple[bool, str]:
    """POST the partner to NURTURE_WEBHOOK_URL. Returns (success, message).

    Never raises: this is a network boundary, so callers can always log the
    result as an activity rather than let an outage block a stage change.
    """
    url = (os.getenv("NURTURE_WEBHOOK_URL") or "").strip()
    list_slug = (service or {}).get("nurture_list_slug")
    email = (partner or {}).get("email")

    if not list_slug:
        return False, f"service '{(service or {}).get('name', '?')}' has no nurture_list_slug configured"
    if not is_valid_slug(list_slug):
        return False, f"service '{(service or {}).get('name', '?')}' has an invalid nurture_list_slug"
    if not email:
        return False, f"partner '{(partner or {}).get('name', '?')}' has no email on file"
    if not url:
        msg = "NURTURE_WEBHOOK_URL not configured — nurture hand-off skipped"
        logger.warning(msg)
        return False, msg

    headers = {"Content-Type": "application/json"}
    token = (os.getenv("NURTURE_WEBHOOK_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = {
        "email": email,
        "name": partner.get("name", ""),
        "list_slug": list_slug,
        "partner_id": partner.get("id"),
        "deal_id": deal_id,
    }
    try:
        response = httpx.post(url, json=body, headers=headers, timeout=10.0, follow_redirects=False)
        response.raise_for_status()
        return True, f"sent to nurture webhook for '{list_slug}'"
    except Exception as exc:
        msg = f"nurture webhook failed for '{list_slug}': {exc}"
        logger.warning(msg)
        return False, msg
