"""ADR section 3, through MCP tools. Partners stay unchanged until conversion."""
from app.database import get_db
from app.services.catalog import create_service
from tests.test_mcp_tools import _enable, call


def _partner_count():
    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM partners").fetchone()["n"]


def test_section_3_walkthrough(client, db, monkeypatch):
    _enable(monkeypatch)
    create_service("Online booking", "online-booking")
    start = _partner_count()

    found, err = call(client, "find_duplicates", {
        "company_name": "Harbour Physio",
        "website": "https://harbourphysio.example",
    })
    assert err is False and found["ok"] is True
    assert found["matches"] == []
    assert _partner_count() == start

    created, err = call(client, "create_lead", {
        "company_name": "Harbour Physio",
        "website": "https://harbourphysio.example",
        "source": "research:directory",
        "tags": ["campaign:physio-illawarra-2026-10"],
    })
    assert err is False and created["ok"] is True
    lead_id = created["lead"]["id"]
    assert created["lead"]["type"] == "lead"
    assert created["lead"]["partner_id"] is None
    assert _partner_count() == start

    updated, err = call(client, "update_lead", {
        "lead_id": lead_id, "phone": "0400111222", "industry": "Health", "team_size": 8,
    })
    assert err is False and updated["ok"] is True
    note, err = call(client, "log_activity", {
        "deal_id": lead_id,
        "type": "research",
        "source_url": "https://harbourphysio.example/about",
        "body": "Website says they plan a second clinic in 2027.",
    })
    assert err is False and note["ok"] is True
    assert _partner_count() == start

    updated, err = call(client, "update_lead", {
        "lead_id": lead_id,
        "contact_name": "Pat Manager",
        "title": "Practice manager",
        "email": "pm@harbourphysio.example",
    })
    assert err is False and updated["lead"]["email"] == "pm@harbourphysio.example"
    assert _partner_count() == start

    detail, err = call(client, "get_lead", {"lead_id": lead_id})
    assert err is False and detail["ok"] is True
    assert detail["research_note_count"] >= 1
    assert detail["last_researched_at"]
    assert "service" not in detail["conversion_readiness"]["missing"] or detail["conversion_readiness"]["ready"] is False
    assert _partner_count() == start

    converted, err = call(client, "convert_lead", {
        "lead_id": lead_id,
        "service_slug": "online-booking",
        "partner_action": "create",
        "value_estimate": 4800,
        "expected_close": "2026-12-15",
        "note": "Phone-only booking is the pain.",
    })
    assert err is False and converted["ok"] is True
    assert converted["opportunity"]["id"] == lead_id
    assert converted["opportunity"]["type"] == "opportunity"
    assert converted["partner_created"] is True
    assert _partner_count() == start + 2

    rejected, err = call(client, "create_lead", {
        "company_name": "No Fit Clinic", "email": "nope@example.com",
    })
    lost, err = call(client, "mark_lost", {
        "deal_id": rejected["lead"]["id"], "lost_reason": "Not a fit", "note": "Wrong size",
    })
    assert lost["ok"] is True
    assert lost["record"]["active"] == 0

    waiting, err = call(client, "create_lead", {
        "company_name": "Later Clinic",
        "email": "later@example.com",
        "next_action": "Call in spring",
        "next_action_date": "2020-01-01",
    })
    due, err = call(client, "list_due_followups", {"type": "lead"})
    assert err is False
    assert any(row["id"] == waiting["lead"]["id"] and row["type"] == "lead" for row in due["deals"])

    duplicate, err = call(client, "ingest_leads", {"leads": [{
        "company_name": "Later Clinic",
        "email": "later@example.com",
        "website": "https://later.example",
    }]})
    assert duplicate["results"][0]["status"] == "duplicate_open_lead"
    assert duplicate["results"][0]["lead_id"] == waiting["lead"]["id"]
