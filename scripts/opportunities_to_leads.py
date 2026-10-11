"""Optional operator script: move early-stage opportunities back to leads.

The schema 8 migration does not guess which opportunities were really leads
(ADR 0001 D12). An operator who wants that cleanup runs this after the
release, against a copy first.

    python scripts/opportunities_to_leads.py --stage new --stage contacted
    python scripts/opportunities_to_leads.py --stage new --apply

Default is dry-run. Selects open opportunities in the given stages that
have no won child and no parent_deal_id. Copies empty lead contact columns
from the partner, sets type=lead, keeps partner_id, and writes one system
activity per changed record.
"""
import argparse
import sys

sys.path.insert(0, ".")

from app.database import get_db, init_db
from app.services.activities import log_activity
from app.services.deals import get_deal, list_deals
from app.services.partners import get_partner
from app.services import pipeline_stages

USAGE = (
    "Usage: python scripts/opportunities_to_leads.py --stage KEY [--stage KEY ...] "
    "[--dry-run | --apply]\n"
    "Default is dry-run. --apply writes. See docs/DATA_MODEL.md and ADR 0001 §6.5."
)

PARTNER_CONTACT_FIELDS = (
    "email", "phone", "website", "title", "address",
    "linkedin_url", "x_url", "instagram_url", "facebook_url", "youtube_url",
    "industry", "team_size", "preferred_channel",
)
ACTIVITY_BODY = "Converted opportunity to lead (opportunities_to_leads)"


def _empty(value):
    return value is None or value == ""


def qualifying_opportunities(stage_keys):
    """Open opportunities in those stages, no parent, no won child."""
    won = set(pipeline_stages.won_stage_keys())
    seen = set()
    chosen = []
    for key in stage_keys:
        for deal in list_deals(stage=key, type="opportunity", include_lost=False):
            if deal["id"] in seen:
                continue
            if deal.get("parent_deal_id"):
                continue
            if deal.get("stage") in won:
                continue
            children = list_deals(
                parent_deal_id=deal["id"], type="opportunity", include_lost=True,
            )
            if any(child.get("stage") in won for child in children):
                continue
            seen.add(deal["id"])
            chosen.append(deal)
    chosen.sort(key=lambda d: d["id"])
    return chosen


def _lead_updates_from_partner(deal, partner):
    updates = {}
    partner = partner or {}
    for field in PARTNER_CONTACT_FIELDS:
        if _empty(deal.get(field)) and not _empty(partner.get(field)):
            updates[field] = partner[field]
    if partner.get("is_company"):
        if _empty(deal.get("company_name")) and partner.get("name"):
            updates["company_name"] = partner["name"]
    else:
        if _empty(deal.get("contact_name")) and partner.get("name"):
            updates["contact_name"] = partner["name"]
        parent = get_partner(partner.get("parent_id")) if partner.get("parent_id") else None
        if parent and _empty(deal.get("company_name")) and parent.get("name"):
            updates["company_name"] = parent["name"]
    return updates


def convert_opportunity_to_lead(deal_id):
    deal = get_deal(deal_id)
    if not deal or deal.get("type") != "opportunity":
        return None
    partner = get_partner(deal["partner_id"]) if deal.get("partner_id") else None
    updates = _lead_updates_from_partner(deal, partner)
    updates["type"] = "lead"
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [deal_id]
    with get_db() as db:
        db.execute(f"UPDATE deals SET {set_clause} WHERE id = ?", values)
        db.commit()
    log_activity(deal.get("partner_id"), "system", ACTIVITY_BODY, deal_id=deal_id)
    return get_deal(deal_id)


def run(stage_keys, apply=False):
    deals = qualifying_opportunities(stage_keys)
    ids = [d["id"] for d in deals]
    verb = "converted" if apply else "would convert"
    print(f"{verb}: {', '.join(str(i) for i in ids) if ids else '(none)'}")
    print(f"count: {len(ids)}")
    if apply:
        for deal_id in ids:
            convert_opportunity_to_lead(deal_id)
    return ids


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(usage=USAGE)
    parser.add_argument("--stage", action="append", dest="stages", default=[])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not args.stages:
        print(USAGE)
        return 1
    apply = bool(args.apply) and not args.dry_run
    init_db()
    run(args.stages, apply=apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
