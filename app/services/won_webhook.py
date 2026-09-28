"""Deal-won hand-off via an optional outbound webhook.

The CRM does not raise invoices. When a deal enters a stage flagged
``is_won``, this POSTs a small JSON payload to whatever invoicing tool
you point DEAL_WON_WEBHOOK_URL at (Zapier, an invoicing API, an agent)
and logs the outcome. Unset URL = no-op (no timeline activity). Invoice
status is not written back into the CRM.
"""
import logging
import os

import httpx

logger = logging.getLogger(__name__)

# Returned as the first tuple element from notify_deal_won.
SENT = "sent"
SKIPPED = "skipped"
FAILED = "failed"


def _offer_payload(offer: dict | None) -> dict | None:
    if not offer:
        return None
    return {
        "id": offer.get("id"),
        "name": offer.get("name"),
        "price": offer.get("price"),
        "currency": offer.get("currency"),
    }


def notify_deal_won(partner: dict, service: dict, deal: dict, stage: str,
                    offer: dict = None) -> tuple[str, str]:
    """POST deal-won details to DEAL_WON_WEBHOOK_URL.

    Returns ``(SENT, message)`` on 2xx, ``(SKIPPED, message)`` when the URL
    is unset (callers must not write a timeline activity), or
    ``(FAILED, message)`` on network / non-2xx errors.

    Never raises: this is a network boundary, so an outage must not block
    a stage change.
    """
    url = (os.getenv("DEAL_WON_WEBHOOK_URL") or "").strip()
    deal_id = (deal or {}).get("id")
    if not url:
        msg = "DEAL_WON_WEBHOOK_URL not configured — deal-won hand-off skipped"
        logger.debug(msg)
        return SKIPPED, msg

    headers = {"Content-Type": "application/json"}
    token = (os.getenv("DEAL_WON_WEBHOOK_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = {
        "deal_id": deal_id,
        "stage": stage,
        "partner_id": (partner or {}).get("id"),
        "partner_name": (partner or {}).get("name") or "",
        "partner_email": (partner or {}).get("email") or None,
        "service_id": (service or {}).get("id"),
        "service_name": (service or {}).get("name"),
        "service_slug": (service or {}).get("slug"),
        "value_estimate": (deal or {}).get("value_estimate"),
        "offer": _offer_payload(offer),
    }
    try:
        response = httpx.post(url, json=body, headers=headers, timeout=10.0, follow_redirects=False)
        response.raise_for_status()
        return SENT, f"sent to deal-won webhook for deal {deal_id}"
    except Exception as exc:
        msg = f"deal-won webhook failed for deal {deal_id}: {exc}"
        logger.warning(msg)
        return FAILED, msg
