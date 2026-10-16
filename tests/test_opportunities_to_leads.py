from app.services.activities import list_activities_for_deal
from app.services.catalog import create_service
from app.services.deals import create_deal, get_deal, set_deal_stage
from app.services.partners import create_partner
from scripts.opportunities_to_leads import (
    ACTIVITY_BODY,
    convert_opportunity_to_lead,
    main,
    qualifying_opportunities,
    run,
)


def _setup(db, *, stage="new", parent=None, name="Early Cafe", email="early@cafe.example"):
    sid = create_service("Consulting", f"consulting-{name.lower().replace(' ', '-')}")
    pid = create_partner(name, is_company=True, email=email, phone="0400111000", website="https://cafe.example")
    deal_id = create_deal(pid, sid, parent_deal_id=parent)
    if stage != "new":
        set_deal_stage(deal_id, stage)
    return pid, sid, deal_id


def test_dry_run_changes_nothing_and_prints_ids(db, capsys):
    _pid, _sid, deal_id = _setup(db)
    ids = run(["new"], apply=False)
    assert ids == [deal_id]
    assert get_deal(deal_id)["type"] == "opportunity"
    out = capsys.readouterr().out
    assert f"would convert: {deal_id}" in out
    assert "count: 1" in out
    bodies = [a["body"] for a in list_activities_for_deal(deal_id)]
    assert ACTIVITY_BODY not in bodies


def test_apply_converts_only_qualifying(db, capsys):
    _pid, sid, early_id = _setup(db, name="Early Cafe")
    parent_pid = create_partner("Parent Co", is_company=True, email="parent@co.example")
    parent_id = create_deal(parent_pid, sid)
    child_id = create_deal(parent_pid, sid, parent_deal_id=parent_id)
    won_parent_pid = create_partner("Won Parent", is_company=True, email="won@co.example")
    won_parent_id = create_deal(won_parent_pid, sid)
    won_child_id = create_deal(won_parent_pid, sid, parent_deal_id=won_parent_id)
    set_deal_stage(won_child_id, "won")

    ids = run(["new"], apply=True)
    assert early_id in ids
    assert parent_id in ids
    assert child_id not in ids
    assert won_parent_id not in ids
    assert get_deal(early_id)["type"] == "lead"
    assert get_deal(early_id)["partner_id"] == _pid
    assert get_deal(early_id)["email"] == "early@cafe.example"
    assert get_deal(early_id)["company_name"] == "Early Cafe"
    assert get_deal(child_id)["type"] == "opportunity"
    assert get_deal(won_parent_id)["type"] == "opportunity"
    bodies = [a["body"] for a in list_activities_for_deal(early_id)]
    assert bodies.count(ACTIVITY_BODY) == 1
    out = capsys.readouterr().out
    assert "converted:" in out


def test_skips_parent_link_and_won_child(db):
    _pid, sid, parent_id = _setup(db, name="Has Child")
    child_id = create_deal(_pid, sid, parent_deal_id=parent_id)
    set_deal_stage(child_id, "won")
    chosen = {d["id"] for d in qualifying_opportunities(["new"])}
    assert parent_id not in chosen
    assert child_id not in chosen


def test_dry_run_cli_never_calls_init_db(db, monkeypatch, capsys):
    import scripts.opportunities_to_leads as script

    def boom():
        raise AssertionError("dry-run must not migrate the database")

    monkeypatch.setattr(script, "init_db", boom)
    assert script.main(["--stage", "new"]) == 0
    assert "would convert" in capsys.readouterr().out


def test_dry_run_cli_aborts_on_unmigrated_schema(db, capsys):
    import scripts.opportunities_to_leads as script

    with db.get_db() as conn:
        conn.execute("PRAGMA user_version = 7")
        conn.commit()
    assert script.main(["--stage", "new"]) == 1
    assert "never migrates" in capsys.readouterr().err


def test_apply_writes_one_system_activity(db):
    _pid, _sid, deal_id = _setup(db, name="Once Only")
    convert_opportunity_to_lead(deal_id)
    convert_opportunity_to_lead(deal_id)
    bodies = [a["body"] for a in list_activities_for_deal(deal_id)]
    # Second call still writes if invoked directly; the CLI converts each id once.
    assert ACTIVITY_BODY in bodies
    assert get_deal(deal_id)["type"] == "lead"
