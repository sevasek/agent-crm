"""Move open opportunities in chosen stages back to leads.

Dry-run by default. Does not run on boot. The schema migration never calls this.

    python scripts/opportunities_to_leads.py --stage new
    python scripts/opportunities_to_leads.py --stage new --stage contacted --apply
"""
import argparse
import sys

sys.path.insert(0, ".")

from app.database import get_db, init_db
from app.services.activities import log_activity
from app.services.partners import get_partner
from app.services import pipeline_stages


def qualifying_ids(stages):
    won = pipeline_stages.won_stage_keys()
    placeholders = ",".join("?" * len(stages))
    with get_db() as db:
        rows = db.execute(
            f"""SELECT id, partner_id FROM deals
                WHERE type = 'opportunity' AND active = 1
                  AND parent_deal_id IS NULL
                  AND stage IN ({placeholders})
                ORDER BY id""",
            stages,
        ).fetchall()
        chosen = []
        for row in rows:
            child = db.execute(
                """SELECT stage FROM deals
                   WHERE parent_deal_id = ? AND active = 1""",
                (row["id"],),
            ).fetchall()
            if any(item["stage"] in won for item in child):
                continue
            chosen.append(dict(row))
    return chosen


def apply_one(deal_id, partner_id):
    partner = get_partner(partner_id) or {}
    with get_db() as db:
        db.execute(
            """UPDATE deals SET
                 type = 'lead',
                 contact_name = COALESCE(contact_name, ?),
                 company_name = COALESCE(company_name, ?),
                 email = COALESCE(email, ?),
                 phone = COALESCE(phone, ?),
                 website = COALESCE(website, ?),
                 title = COALESCE(title, ?),
                 updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND type = 'opportunity'""",
            (
                None if partner.get("is_company") else partner.get("name"),
                partner.get("name") if partner.get("is_company") else None,
                partner.get("email"),
                partner.get("phone"),
                partner.get("website"),
                partner.get("title"),
                deal_id,
            ),
        )
        db.commit()
    log_activity(partner_id, "system", "Reclassified from opportunity to lead", deal_id=deal_id)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", action="append", required=True, help="Stage key. Repeat for more than one.")
    parser.add_argument("--apply", action="store_true", help="Write the change. Without this flag the script only prints.")
    args = parser.parse_args(argv)
    init_db()
    rows = qualifying_ids(args.stage)
    ids = [row["id"] for row in rows]
    print(f"{'apply' if args.apply else 'dry-run'}: {len(ids)} opportunity(ies): {ids}")
    if args.apply:
        for row in rows:
            apply_one(row["id"], row["partner_id"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
