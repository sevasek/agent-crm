from datetime import date

from app.services.catalog import create_service
from app.services.deals import create_deal, get_deal
from app.services.partners import create_partner, get_partner
from tests.test_mcp_tools import _enable, call


def test_offer_create_list_attach_get_deal(client, db, monkeypatch):
    _enable(monkeypatch)
    service_id = create_service("Consulting", "consulting")
    created, _ = call(client, "create_offer", {
        "name": "Consulting deposit",
        "service_slug": "consulting",
        "price": 350,
        "currency": "USD",
        "description": "Discovery / deposit",
    })
    assert created["ok"] is True
    assert created["status"] == "created"
    offer_id = created["offer"]["id"]
    assert created["offer"]["price"] == 350
    assert created["offer"]["currency"] == "USD"

    catalog, _ = call(client, "list_catalog")
    assert any(o["id"] == offer_id for o in catalog["offers"])

    partner_id = create_partner("Acme Childcare", is_company=True, phone="0400111222")
    deal, _ = call(client, "create_deal", {
        "partner_id": partner_id,
        "service_id": service_id,
        "offer_id": offer_id,
    })
    assert deal["deal"]["offer_id"] == offer_id

    got, _ = call(client, "get_deal", {"deal_id": deal["deal"]["id"]})
    assert got["offer"]["id"] == offer_id
    assert got["offer"]["name"] == "Consulting deposit"
    assert got["offer"]["price"] == 350


def test_ingest_phone_company_no_email(client, db, monkeypatch):
    """MCP ingest_leads accepts company+phone scraped rows with no email."""
    _enable(monkeypatch)
    create_service("Dead Lead Reactivation", "dead-lead-reactivation")
    lead = {
        "company_name": "Northside Cafe",
        "phone": "0400 333 444",
        "service_slug": "dead-lead-reactivation",
    }
    assert "email" not in lead
    payload, _ = call(client, "ingest_leads", {"leads": [lead]})
    result = payload["results"][0]
    assert result["status"] == "created"
    assert result["partner_id"]
    assert result["deal_id"]
    partner = get_partner(result["partner_id"])
    assert partner["email"] is None
    assert partner["is_company"] == 1
    assert partner["phone"] == "0400 333 444"


def test_call_queue_returns_new_dead_leads_with_phone(client, db, monkeypatch):
    _enable(monkeypatch)
    create_service("Dead Lead Reactivation", "dead-lead-reactivation")
    ingest, _ = call(client, "ingest_leads", {"leads": [{
        "company_name": "Southside Cafe",
        "phone": "0400 333 555",
        "service_slug": "dead-lead-reactivation",
        "source": "scrape:cafes",
    }]})
    deal_id = ingest["results"][0]["deal_id"]
    default_queue, _ = call(client, "get_call_queue")
    assert default_queue["calls"] == []

    campaign, _ = call(client, "get_call_queue", {
        "stage": "new",
        "service_slug": "dead-lead-reactivation",
    })
    assert campaign["ok"] is True
    assert [c["deal"]["id"] for c in campaign["calls"]] == [deal_id]
    assert campaign["calls"][0]["partner_phone"] == "0400 333 555"

    via_flag, _ = call(client, "get_call_queue", {
        "include_new": True,
        "service_slug": "dead-lead-reactivation",
    })
    assert [c["deal"]["id"] for c in via_flag["calls"]] == [deal_id]


def test_list_deals_filters_by_owner(client, db, monkeypatch):
    _enable(monkeypatch)
    service_id = create_service("Consulting", "consulting")
    hers = create_partner("Bob Book", phone="0400000001")
    his = create_partner("Alice Book", phone="0400000002")
    her_deal = create_deal(hers, service_id)
    his_deal = create_deal(his, service_id)
    assigned, _ = call(client, "set_deal_owner", {"deal_id": her_deal, "owner": "bob"})
    assert assigned["deal"]["owner_key"] == "bob"
    call(client, "set_deal_owner", {"deal_id": his_deal, "owner_key": "alice"})

    listed, _ = call(client, "list_deals", {"owner": "bob"})
    assert [d["id"] for d in listed["deals"]] == [her_deal]
    assert listed["deals"][0]["owner_key"] == "bob"


def test_bulk_update_deals_by_service_slug(client, db, monkeypatch):
    _enable(monkeypatch)
    create_service("Dead Lead Reactivation", "dead-lead-reactivation")
    create_service("Consulting", "consulting")
    a = create_partner("Cafe A", is_company=True, phone="0400100001")
    b = create_partner("Cafe B", is_company=True, phone="0400100002")
    keep = create_partner("Acme Childcare", is_company=True, phone="0400100003")
    from app.services.catalog import get_service_by_slug
    dlr = get_service_by_slug("dead-lead-reactivation")
    hc = get_service_by_slug("consulting")
    d1 = create_deal(a, dlr["id"])
    d2 = create_deal(b, dlr["id"])
    d3 = create_deal(keep, hc["id"])
    today = date.today().isoformat()
    payload, _ = call(client, "bulk_update_deals", {
        "service_slug": "dead-lead-reactivation",
        "stage": "new",
        "set_stage": "contacted",
        "next_action": "Call",
        "next_action_date": today,
    })
    assert payload["ok"] is True
    assert set(payload["updated_ids"]) == {d1, d2}
    assert get_deal(d1)["stage"] == "contacted"
    assert get_deal(d2)["next_action_date"] == today
    assert get_deal(d3)["stage"] == "new"


def test_search_partners_finds_nested_people(client, db, monkeypatch):
    _enable(monkeypatch)
    company_id = create_partner("Acme Childcare", is_company=True, phone="0400999000")
    person_id = create_partner(
        "Jane Nested", parent_id=company_id, email="jane@nested.example",
    )
    found, _ = call(client, "search_partners", {"query": "Jane Nested"})
    assert found["partners"][0]["id"] == person_id
    assert found["partners"][0]["company"]["id"] == company_id

    got, _ = call(client, "get_partner", {"partner_id": company_id})
    assert [p["id"] for p in got["people"]] == [person_id]


def test_create_service_is_idempotent(client, db, monkeypatch):
    _enable(monkeypatch)

    created, _ = call(client, "create_service", {
        "name": "AI Concierge", "slug": "ai-concierge",
    })
    assert created["ok"] is True
    assert created["status"] == "created"
    assert created["service"]["slug"] == "ai-concierge"

    again, _ = call(client, "create_service", {"name": "AI Concierge"})
    assert again["status"] == "existing"
    assert again["service"]["id"] == created["service"]["id"]


def test_update_service_changes_fields_and_can_deactivate(client, db, monkeypatch):
    _enable(monkeypatch)
    created, _ = call(client, "create_service", {
        "name": "AI Concierge", "slug": "ai-concierge",
    })
    service_id = created["service"]["id"]
    updated, _ = call(client, "update_service", {
        "service_id": service_id,
        "name": "AI Concierge retainer",
        "description": "Done-with-you AI consulting.",
        "nurture_list_slug": "ai-concierge-interest",
    })
    assert updated["ok"] is True
    assert updated["status"] == "updated"
    assert updated["service"]["name"] == "AI Concierge retainer"
    assert updated["service"]["slug"] == "ai-concierge"
    assert updated["service"]["description"] == "Done-with-you AI consulting."
    assert updated["service"]["nurture_list_slug"] == "ai-concierge-interest"
    assert updated["service"]["active"] is True

    hidden, _ = call(client, "update_service", {
        "service_id": service_id, "active": False,
    })
    assert hidden["service"]["active"] is False
    catalog, _ = call(client, "list_catalog")
    assert all(s["id"] != service_id for s in catalog["services"])

    restored, _ = call(client, "update_service", {
        "service_id": service_id, "active": True,
    })
    assert restored["service"]["active"] is True
    catalog, _ = call(client, "list_catalog")
    assert any(s["id"] == service_id for s in catalog["services"])


def test_update_service_rejects_slug_conflict_and_missing(client, db, monkeypatch):
    _enable(monkeypatch)
    first, _ = call(client, "create_service", {"name": "Consulting", "slug": "consulting"})
    second, _ = call(client, "create_service", {"name": "GEO", "slug": "geo"})
    conflict, is_error = call(client, "update_service", {
        "service_id": second["service"]["id"], "slug": "consulting",
    })
    assert is_error is True
    assert conflict["error"] == "slug_conflict"
    missing, is_error = call(client, "update_service", {
        "service_id": 999, "name": "Nope",
    })
    assert is_error is True
    assert missing["error"] == "not_found"
    invalid, is_error = call(client, "update_service", {
        "service_id": first["service"]["id"], "slug": "../admin",
    })
    assert is_error is True
    assert invalid["error"] == "invalid_slug"
    empty, is_error = call(client, "update_service", {"service_id": first["service"]["id"]})
    assert is_error is True
    assert empty["error"] == "no_fields"
