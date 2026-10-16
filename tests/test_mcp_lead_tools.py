"""MCP lead tools and the changed deal tools."""
from app.services.catalog import create_service
from app.services.deals import create_deal, get_deal
from app.services.lead_records import create_lead
from app.services.partners import create_partner, list_partners
from tests.test_mcp_tools import _enable, call


def _partners_count():
    return len(list_partners())


def test_create_lead_and_error_codes(client, db, monkeypatch):
    _enable(monkeypatch)
    created, err = call(client, "create_lead", {
        "company_name": "Riverstone Dental",
        "website": "https://riverstonedental.example",
        "source": "research:directory",
    })
    assert err is False
    assert created["lead"]["type"] == "lead"
    assert created["lead"]["partner_id"] is None
    assert created["possible_duplicates"] == []
    assert _partners_count() == 0

    missing, _ = call(client, "create_lead", {"name": "No Identity"})
    assert missing["error"] == "invalid"

    create_service("Consulting", "consulting")
    bad_svc, _ = call(client, "create_lead", {
        "company_name": "X", "website": "https://x.example", "service_slug": "nope",
    })
    assert bad_svc["error"] == "invalid_service"

    missing_partner, _ = call(client, "create_lead", {
        "company_name": "Y", "website": "https://y.example", "partner_id": 999,
    })
    assert missing_partner["error"] == "partner_not_found"


def test_update_get_list_find_duplicates(client, db, monkeypatch):
    _enable(monkeypatch)
    lead, _ = call(client, "create_lead", {
        "company_name": "Riverstone Dental", "website": "https://riverstonedental.example",
    })
    lead_id = lead["lead"]["id"]
    updated, _ = call(client, "update_lead", {
        "lead_id": lead_id, "phone": "0400 111 222", "industry": "Dental",
    })
    assert updated["lead"]["phone"] == "0400 111 222"

    lost_update, _ = call(client, "update_lead", {"lead_id": 999, "phone": "1"})
    assert lost_update["error"] == "not_found"
    opp_id = create_deal(
        create_partner("Acme", email="a@acme.example"),
        create_service("Consulting", "consulting"),
    )
    not_lead, _ = call(client, "update_lead", {"lead_id": opp_id, "phone": "1"})
    assert not_lead["error"] == "not_a_lead"

    got, _ = call(client, "get_lead", {"lead_id": lead_id})
    assert got["lead"]["id"] == lead_id
    assert "conversion_readiness" in got["lead"]
    assert "last_researched_at" in got["lead"]
    assert got["lead"]["research_note_count"] == 0

    listed, _ = call(client, "list_leads", {"query": "Riverstone"})
    assert listed["count"] == 1

    dups, _ = call(client, "find_duplicates", {
        "company_name": "Riverstone Dental", "website": "https://riverstonedental.example",
    })
    assert any(m["id"] == lead_id and m["strength"] == "strong" for m in dups["matches"])


def test_mark_lost_restore_merge_lost_reasons(client, db, monkeypatch):
    _enable(monkeypatch)
    reasons, _ = call(client, "list_lost_reasons")
    assert reasons["count"] >= 1
    created_reason, _ = call(client, "create_lost_reason", {"name": "Seasonal pause"})
    assert created_reason["lost_reason"]["name"] == "Seasonal pause"
    dup, _ = call(client, "create_lost_reason", {"name": "Seasonal pause"})
    assert dup["error"] == "duplicate_name"
    updated_reason, _ = call(client, "update_lost_reason", {
        "lost_reason_id": created_reason["lost_reason"]["id"], "active": False,
    })
    assert updated_reason["lost_reason"]["active"] in (0, False)

    lead, _ = call(client, "create_lead", {
        "company_name": "Cafe North", "phone": "0400 222 333",
    })
    lost, _ = call(client, "mark_lost", {
        "deal_id": lead["lead"]["id"], "lost_reason": "Not a fit",
    })
    assert lost["deal"]["active"] is False
    already, _ = call(client, "mark_lost", {
        "deal_id": lead["lead"]["id"], "lost_reason": "Not a fit",
    })
    assert already["error"] == "already_lost"
    restored, _ = call(client, "restore_deal", {"deal_id": lead["lead"]["id"]})
    assert restored["deal"]["active"] is True

    a, _ = call(client, "create_lead", {"company_name": "Merge A", "email": "a@merge.example"})
    b, _ = call(client, "create_lead", {"company_name": "Merge B", "email": "b@merge.example"})
    merged, _ = call(client, "merge_leads", {
        "target_id": a["lead"]["id"], "source_ids": [b["lead"]["id"]],
    })
    assert b["lead"]["id"] in merged["merged_ids"]
    cannot, _ = call(client, "restore_deal", {"deal_id": b["lead"]["id"]})
    assert cannot["error"] == "merged"


def test_set_deal_stage_rejects_lead_and_lost(client, db, monkeypatch):
    _enable(monkeypatch)
    create_service("Consulting", "consulting")
    lead, _ = call(client, "create_lead", {
        "company_name": "Stage Lead", "website": "https://stage.example",
    })
    refused, _ = call(client, "set_deal_stage", {"deal_id": lead["lead"]["id"], "stage": "contacted"})
    assert refused["error"] == "not_an_opportunity"
    assert get_deal(lead["lead"]["id"])["stage"] == "new"

    partner_id = create_partner("Opp", email="opp@x.example")
    from app.services.catalog import get_service_by_slug
    deal_id = create_deal(partner_id, get_service_by_slug("consulting")["id"])
    call(client, "mark_lost", {"deal_id": deal_id, "lost_reason": "Not a fit"})
    lost_stage, _ = call(client, "set_deal_stage", {"deal_id": deal_id, "stage": "contacted"})
    assert lost_stage["error"] == "deal_lost"


def test_section_3_walkthrough_partners_unchanged_until_convert(client, db, monkeypatch):
    """ADR §3 steps 1–5 via call_tool. partners count asserted after every step."""
    _enable(monkeypatch)
    create_service("Online booking", "online-booking")
    before = _partners_count()

    dups, _ = call(client, "find_duplicates", {
        "company_name": "Riverstone Dental",
        "website": "https://riverstonedental.example",
    })
    assert dups["matches"] == []
    assert _partners_count() == before

    created, _ = call(client, "create_lead", {
        "company_name": "Riverstone Dental",
        "website": "https://riverstonedental.example",
        "source": "research:directory",
        "tags": ["campaign:example-2026-10"],
    })
    lead_id = created["lead"]["id"]
    assert created["lead"]["partner_id"] is None
    assert _partners_count() == before

    call(client, "update_lead", {
        "lead_id": lead_id, "phone": "0400 111 000", "industry": "Dentistry",
    })
    call(client, "log_activity", {
        "deal_id": lead_id,
        "type": "research",
        "source_url": "https://riverstonedental.example/about",
        "body": "Website says they plan a second clinic in 2027.",
    })
    call(client, "update_lead", {
        "lead_id": lead_id,
        "contact_name": "Pat Manager",
        "title": "Practice manager",
        "email": "pm@riverstonedental.example",
    })
    assert _partners_count() == before

    got, _ = call(client, "get_lead", {"lead_id": lead_id})
    assert got["lead"]["research_note_count"] == 1
    assert got["lead"]["last_researched_at"]
    assert _partners_count() == before

    converted, _ = call(client, "convert_lead", {
        "lead_id": lead_id,
        "service_slug": "online-booking",
        "partner_action": "create",
        "value_estimate": 4800,
        "note": "Fit enough. Phone-only booking is the pain.",
    })
    assert converted["ok"] is True
    assert converted["partner_created"] is True
    assert converted["opportunity"]["id"] == lead_id
    assert converted["opportunity"]["type"] == "opportunity"
    # partner_action=create with company_name + contact_name makes a company
    # partner and a person partner (the opportunity hangs off the person).
    assert _partners_count() == before + 2

    call(client, "update_lead", {
        "lead_id": lead_id,
    })
    # converted record is no longer a lead
    not_lead, _ = call(client, "get_lead", {"lead_id": lead_id})
    assert not_lead["error"] == "not_a_lead"

    # 4c option 1: a still-open lead appears in list_due_followups
    parked, _ = call(client, "create_lead", {
        "company_name": "Later Clinic",
        "website": "https://later.example",
        "next_action": "Revisit in a week",
        "next_action_date": "2000-01-01",
    })
    due, _ = call(client, "list_due_followups", {"type": "lead"})
    assert parked["lead"]["id"] in [d["id"] for d in due["deals"]]
    assert _partners_count() == before + 2
