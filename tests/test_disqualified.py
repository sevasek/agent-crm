"""Disqualification is a flag on a lost reason, and outcome is derived."""
import sqlite3

from app.database import (
    DISQUALIFIED_LOST_REASON,
    MIGRATIONS,
    SCHEMA_VERSION,
    get_db,
    init_db,
    migrate_012,
)
from app.services.activities import list_activities_for_deal
from app.services.auth import generate_csrf_token
from app.services.catalog import create_service
from app.services.deal_outcome import OUTCOME_DISQUALIFIED, OUTCOME_LOST, OUTCOME_OPEN, OUTCOME_WON, deal_outcome
from app.services.deals import create_deal, get_deal, list_deals, mark_lost, set_deal_stage
from app.services.lead_records import create_lead, list_leads
from app.services.lost_reasons import (
    create_lost_reason,
    get_lost_reason_by_name,
    list_lost_reasons,
    update_lost_reason,
)
from app.services.partners import create_partner
from tests.test_mcp_tools import _enable, call


def _csrf(client):
    return generate_csrf_token(client.cookies.get("session"))


def test_deal_outcome_from_active_stage_and_reason_flag():
    won = {"won"}
    assert deal_outcome({"active": 1, "stage": "new"}, won_stage_keys=won) == OUTCOME_OPEN
    assert deal_outcome({"stage": "won"}, won_stage_keys=won) == OUTCOME_WON
    lost = {
        "active": 0,
        "stage": "won",
        "lost_reason_id": 3,
        "is_disqualification": 0,
    }
    assert deal_outcome(lost, won_stage_keys=won) == OUTCOME_LOST
    disqualified = {
        "active": 0,
        "stage": "proposal",
        "lost_reason": {"name": "Disqualified", "is_disqualification": 1},
    }
    assert deal_outcome(disqualified, won_stage_keys=won) == OUTCOME_DISQUALIFIED
    assert deal_outcome({"active": 0}, won_stage_keys=won) == OUTCOME_LOST


def test_disqualified_reason_is_seeded(db):
    row = get_lost_reason_by_name(DISQUALIFIED_LOST_REASON)
    assert row["name"] == "Disqualified"
    assert row["is_disqualification"] == 1
    assert row["active"] == 1
    assert row["triggers_nurture"] == 0
    others = [r for r in list_lost_reasons() if r["name"] != "Disqualified"]
    assert others
    assert all(r["is_disqualification"] == 0 for r in others)


def test_migrate_012_keeps_handmade_disqualified_row(tmp_path, monkeypatch):
    """Production already has a hand-made reason named Disqualified. Keep its id."""
    path = tmp_path / "data" / "crm.db"
    path.parent.mkdir()
    monkeypatch.setattr("app.database.DB_PATH", str(path))
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    with get_db() as db:
        for ver in range(1, 12):
            MIGRATIONS[ver](db)
        db.execute(
            """INSERT INTO lost_reasons
                   (name, triggers_nurture, active, created_at, updated_at)
               VALUES ('Disqualified', 0, 1, '2026-01-01T00:00:00', '2026-01-01T00:00:00')"""
        )
        handmade_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute("PRAGMA user_version = 11")
        db.commit()
    assert handmade_id == 8
    init_db()
    with get_db() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT * FROM lost_reasons WHERE name = 'Disqualified'"
        ).fetchall()]
        assert len(rows) == 1
        assert rows[0]["id"] == handmade_id
        assert rows[0]["is_disqualification"] == 1
        assert rows[0]["created_at"] == "2026-01-01T00:00:00"
        assert rows[0]["updated_at"] != "2026-01-01T00:00:00"
        assert db.execute("SELECT COUNT(*) FROM lost_reasons").fetchone()[0] == 8
        cols = {row["name"]: row for row in db.execute("PRAGMA table_info(lost_reasons)")}
        assert cols["is_disqualification"]["notnull"] == 1
        assert cols["is_disqualification"]["dflt_value"] == "0"
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_migrate_012_inserts_disqualified_when_missing(tmp_path, monkeypatch):
    path = tmp_path / "data" / "crm.db"
    path.parent.mkdir()
    monkeypatch.setattr("app.database.DB_PATH", str(path))
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    with get_db() as db:
        for ver in range(1, 12):
            MIGRATIONS[ver](db)
        db.execute("PRAGMA user_version = 11")
        db.commit()
    init_db()
    row = get_lost_reason_by_name("Disqualified")
    assert row["is_disqualification"] == 1
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM lost_reasons").fetchone()[0] == 8


def test_migrate_012_again_does_not_rewrite_the_seed(db):
    with get_db() as conn:
        before = [dict(r) for r in conn.execute("SELECT * FROM lost_reasons ORDER BY id")]
        migrate_012(conn)
        migrate_012(conn)
        conn.commit()
        after = [dict(r) for r in conn.execute("SELECT * FROM lost_reasons ORDER BY id")]
    assert before == after


def test_create_and_update_lost_reason_flag(db):
    created, error = create_lost_reason("Out of area", is_disqualification=True)
    assert error is None
    assert created["is_disqualification"] == 1
    cleared, error = update_lost_reason(created["id"], is_disqualification=False)
    assert error is None
    assert cleared["is_disqualification"] == 0
    flagged, error = update_lost_reason(created["id"], is_disqualification=True, active=True)
    assert error is None
    assert flagged["is_disqualification"] == 1


def test_mark_lost_can_change_reason_without_moving_closed_at(db):
    lead, _ = create_lead(company_name="Too Small Co")
    lost, error = mark_lost(lead["id"], lost_reason="Not a fit", note="First pass")
    assert error is None
    closed_at = lost["closed_at"]
    assert any(
        (a["body"] or "").startswith("Marked lost: Not a fit")
        for a in list_activities_for_deal(lead["id"])
    )

    _, error = mark_lost(lead["id"], lost_reason="not-a-real-reason")
    assert error == "invalid_lost_reason"
    assert get_deal(lead["id"])["lost_reason_id"] == lost["lost_reason_id"]

    _, error = mark_lost(lead["id"])
    assert error == "already_lost"

    changed, error = mark_lost(lead["id"], lost_reason="Disqualified")
    assert error is None
    assert changed["closed_at"] == closed_at
    assert changed["lost_note"] == "First pass"
    assert changed["lost_reason_id"] == get_lost_reason_by_name("Disqualified")["id"]
    assert changed["active"] == 0
    bodies = [a["body"] for a in list_activities_for_deal(lead["id"])]
    assert "Lost reason changed from Not a fit to Disqualified" in bodies
    assert deal_outcome(changed) == OUTCOME_DISQUALIFIED

    noted, error = mark_lost(lead["id"], lost_reason="No budget", note="They said no")
    assert error is None
    assert noted["closed_at"] == closed_at
    assert noted["lost_note"] == "They said no"
    assert deal_outcome(get_deal(lead["id"])) == OUTCOME_LOST


def test_reason_change_does_not_enroll_nurture(db, monkeypatch):
    calls = []

    def fake(*_args, **_kwargs):
        calls.append(1)
        return ("sent", "enrolled")

    monkeypatch.setattr("app.services.nurture.enroll_partner_in_nurture", fake)
    partner_id = create_partner("Nurture Later", email="nurture@x.example")
    service_id = create_service("Consulting", "consulting-nurture-dq")
    deal_id = create_deal(partner_id, service_id)
    marked, error = mark_lost(deal_id, lost_reason="Not now")
    assert error is None
    assert len(calls) == 1
    changed, error = mark_lost(deal_id, lost_reason="Disqualified")
    assert error is None
    assert changed["closed_at"] == marked["closed_at"]
    assert len(calls) == 1


def test_flagging_a_reason_changes_outcome_without_rewriting_the_deal(db):
    created, _ = create_lost_reason("Wrong suburb")
    lead, _ = create_lead(company_name="Suburb Co")
    lost, _ = mark_lost(lead["id"], lost_reason="Wrong suburb")
    assert deal_outcome(get_deal(lead["id"])) == OUTCOME_LOST
    update_lost_reason(created["id"], is_disqualification=True)
    fresh = get_deal(lead["id"])
    assert fresh["closed_at"] == lost["closed_at"]
    assert fresh["updated_at"] == lost["updated_at"]
    assert deal_outcome(fresh) == OUTCOME_DISQUALIFIED


def test_lost_stage_shim_does_not_replace_an_existing_reason(db):
    partner_id = create_partner("Already Lost")
    service_id = create_service("Consulting", "consulting-shim-dq")
    deal_id = create_deal(partner_id, service_id)
    lost, _ = mark_lost(deal_id, lost_reason="No budget")
    assert set_deal_stage(deal_id, "lost") is True
    fresh = get_deal(deal_id)
    assert fresh["lost_reason_id"] == lost["lost_reason_id"]
    assert fresh["stage"] == "lost"


def test_lists_and_pages_say_disqualified(logged_in_client, db):
    partner_id = create_partner("Wrong Fit Cafe")
    service_id = create_service("Consulting", "consulting-dq")
    disqualified_id = create_deal(partner_id, service_id)
    mark_lost(disqualified_id, lost_reason="Disqualified")
    lost_id = create_deal(create_partner("Budget Cafe"), service_id)
    mark_lost(lost_id, lost_reason="No budget")
    lead, _ = create_lead(company_name="Tiny Clinic")
    mark_lost(lead["id"], lost_reason="Disqualified")

    rows = {row["id"]: row for row in list_deals(include_lost=True)}
    assert rows[disqualified_id]["outcome"] == "disqualified"
    assert rows[disqualified_id]["is_disqualified"] is True
    assert rows[lost_id]["outcome"] == "lost"
    assert rows[lost_id]["is_disqualified"] is False
    listed = {row["id"]: row for row in list_leads(include_lost=True)}
    assert listed[lead["id"]]["outcome"] == "disqualified"

    deals = logged_in_client.get("/crm/deals?lost=1")
    assert "pill-disqualified" in deals.text
    assert "pill-lost" in deals.text
    assert "Wrong Fit Cafe" in deals.text
    pipeline = logged_in_client.get("/crm/pipeline?lost=1")
    assert "pill-disqualified" in pipeline.text
    leads = logged_in_client.get("/crm/leads?lost=1")
    assert "pill-disqualified" in leads.text
    assert "Tiny Clinic" in leads.text

    page = logged_in_client.get(f"/crm/deals/{disqualified_id}/edit")
    assert "This opportunity is disqualified" in page.text
    assert "pill-disqualified" in page.text
    assert "pill-lost" not in page.text
    assert "Update lost reason" in page.text
    assert "(disqualification)" in page.text

    lost_page = logged_in_client.get(f"/crm/deals/{lost_id}/edit")
    assert "This opportunity is lost" in lost_page.text
    assert "pill-lost" in lost_page.text

    lead_page = logged_in_client.get(f"/crm/leads/{lead['id']}")
    assert "This lead is disqualified" in lead_page.text
    assert "pill-disqualified" in lead_page.text


def test_admin_edits_disqualification_and_changes_reason(logged_in_client, db):
    page = logged_in_client.get("/definitions/lost-reasons")
    assert 'aria-label="Disqualification"' in page.text
    created = logged_in_client.post("/definitions/lost-reasons/new", data={
        "csrf_token": _csrf(logged_in_client),
        "name": "Out of area",
        "active": "on",
        "is_disqualification": "on",
    }, follow_redirects=False)
    assert created.status_code == 303
    row = get_lost_reason_by_name("Out of area")
    assert row["is_disqualification"] == 1
    cleared = logged_in_client.post(f"/definitions/lost-reasons/{row['id']}/edit", data={
        "csrf_token": _csrf(logged_in_client),
        "name": "Out of area",
        "active": "on",
    }, follow_redirects=False)
    assert cleared.status_code == 303
    assert get_lost_reason_by_name("Out of area")["is_disqualification"] == 0

    partner_id = create_partner("Change Me")
    service_id = create_service("Consulting", "consulting-change")
    deal_id = create_deal(partner_id, service_id)
    mark_lost(deal_id, lost_reason="Not a fit", note="Keep this note")
    closed_at = get_deal(deal_id)["closed_at"]
    resp = logged_in_client.post(f"/crm/deals/{deal_id}/lost", data={
        "csrf_token": _csrf(logged_in_client),
        "lost_reason": "Disqualified",
        "note": "",
    }, follow_redirects=False)
    assert resp.status_code == 303
    fresh = get_deal(deal_id)
    assert fresh["closed_at"] == closed_at
    assert fresh["lost_note"] == "Keep this note"
    assert fresh["lost_reason_id"] == get_lost_reason_by_name("Disqualified")["id"]
    page = logged_in_client.get(f"/crm/deals/{deal_id}/edit")
    assert "This opportunity is disqualified" in page.text


def test_mcp_outcome_and_lost_reason_flag(client, db, monkeypatch):
    _enable(monkeypatch)
    created, err = call(client, "create_lost_reason", {
        "name": "Too small", "is_disqualification": True,
    })
    assert err is False
    assert created["lost_reason"]["is_disqualification"] in (1, True)
    updated, err = call(client, "update_lost_reason", {
        "lost_reason_id": created["lost_reason"]["id"],
        "is_disqualification": False,
    })
    assert err is False
    assert updated["lost_reason"]["is_disqualification"] in (0, False)
    catalog, _ = call(client, "list_catalog")
    flagged = [r for r in catalog["lost_reasons"] if r["name"] == "Disqualified"]
    assert flagged and flagged[0]["is_disqualification"] is True

    partner_id = create_partner("MCP Cafe", email="mcp@x.example")
    service_id = create_service("Consulting", "consulting-mcp-dq")
    deal_id = create_deal(partner_id, service_id)
    opened, _ = call(client, "get_deal", {"deal_id": deal_id})
    assert opened["deal"]["outcome"] == "open"
    assert opened["deal"]["is_disqualified"] is False

    set_deal_stage(deal_id, "won")
    won, _ = call(client, "get_deal", {"deal_id": deal_id})
    assert won["deal"]["outcome"] == "won"
    assert won["deal"]["is_disqualified"] is False

    lost, _ = call(client, "mark_lost", {"deal_id": deal_id, "lost_reason": "No budget"})
    assert lost["deal"]["outcome"] == "lost"
    assert lost["deal"]["is_disqualified"] is False
    closed_at = lost["deal"]["closed_at"]
    same, _ = call(client, "mark_lost", {"deal_id": deal_id, "lost_reason": "No budget"})
    assert same["error"] == "already_lost"

    changed, err = call(client, "mark_lost", {
        "deal_id": deal_id, "lost_reason": "Disqualified",
    })
    assert err is False
    assert changed["deal"]["outcome"] == "disqualified"
    assert changed["deal"]["is_disqualified"] is True
    assert changed["deal"]["closed_at"] == closed_at
    assert changed["deal"]["lost_reason"] == "Disqualified"

    listed, _ = call(client, "list_deals", {"include_lost": True})
    row = next(d for d in listed["deals"] if d["id"] == deal_id)
    assert row["outcome"] == "disqualified"
    assert row["is_disqualified"] is True

    lead, _ = call(client, "create_lead", {"company_name": "MCP Lead"})
    call(client, "mark_lost", {"deal_id": lead["lead"]["id"], "lost_reason": "Disqualified"})
    got, _ = call(client, "get_lead", {"lead_id": lead["lead"]["id"]})
    assert got["lead"]["outcome"] == "disqualified"
    assert got["lead"]["is_disqualified"] is True
