"""Optional reclassification of early opportunities. Dry-run does not write."""
from app.database import get_db
from app.services.activities import list_activities_for_deal
from app.services.catalog import create_service
from app.services.deals import create_deal, get_deal, set_deal_stage
from app.services.partners import create_partner
from scripts.opportunities_to_leads import main


def _opportunity(stage="new"):
    partner_id = create_partner("Ada Script", email="ada-script@x.example", phone="0400")
    service_id = create_service("Consulting", f"consulting-{partner_id}")
    deal_id = create_deal(partner_id, service_id)
    if stage != "new":
        set_deal_stage(deal_id, stage)
    return deal_id


def test_dry_run_prints_and_does_not_write(db, capsys):
    deal_id = _opportunity()
    assert main(["--stage", "new"]) == 0
    assert str(deal_id) in capsys.readouterr().out
    assert get_deal(deal_id)["type"] == "opportunity"


def test_apply_converts_only_qualifying_records(db, capsys):
    open_id = _opportunity("new")
    contacted = _opportunity("contacted")
    parent = _opportunity("new")
    child_service = create_service("Other", "other-script")
    child = create_deal(get_deal(parent)["partner_id"], child_service, parent_deal_id=parent)
    won = _opportunity("new")
    set_deal_stage(won, "won")
    blocked_parent = _opportunity("new")
    won_child = create_deal(
        get_deal(blocked_parent)["partner_id"],
        create_service("Won Svc", "won-svc"),
        parent_deal_id=blocked_parent,
    )
    set_deal_stage(won_child, "won")

    assert main(["--stage", "new", "--apply"]) == 0
    assert get_deal(open_id)["type"] == "lead"
    assert get_deal(open_id)["partner_id"]
    assert get_deal(contacted)["type"] == "opportunity"
    assert get_deal(child)["type"] == "opportunity"
    assert get_deal(blocked_parent)["type"] == "opportunity"
    assert get_deal(won)["type"] == "opportunity"
    notes = [row for row in list_activities_for_deal(open_id) if "Reclassified" in (row["body"] or "")]
    assert len(notes) == 1
    with get_db() as conn:
        assert conn.execute(
            "SELECT type FROM deals WHERE id = ?", (open_id,)
        ).fetchone()["type"] == "lead"
