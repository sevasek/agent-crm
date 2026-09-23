from app.services.activities import list_activities_for_deal
from app.services.catalog import create_service
from app.services.deals import create_deal, get_deal, list_deals
from app.services.partners import create_partner, get_partner, get_partner_by_email


def _enable(monkeypatch, key="test-mcp-key"):
    monkeypatch.setenv("CRM_MCP_API_KEY", key)


def _headers(key="test-mcp-key"):
    return {
        "X-API-Key": key,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def call(client, name, arguments=None, key="test-mcp-key"):
    resp = client.post("/mcp", json={
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    }, headers=_headers(key))
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    return result["structuredContent"], result["isError"]


def test_search_and_get_partner(client, db, monkeypatch):
    _enable(monkeypatch)
    partner_id = create_partner("Jane Doe", email="jane@acme.example", phone="+61400000000")
    payload, is_error = call(client, "search_partners", {"query": "jane"})
    assert is_error is False
    assert payload["ok"] is True
    assert payload["partners"][0]["id"] == partner_id

    payload, is_error = call(client, "get_partner", {"partner_id": partner_id})
    assert payload["partner"]["email"] == "jane@acme.example"
    assert payload["deals"] == []


def test_ingest_leads_is_idempotent_and_returns_ids(client, db, monkeypatch):
    _enable(monkeypatch)
    create_service("Consulting", "consulting")
    lead = {
        "name": "Jane Doe",
        "email": "jane@acme.example",
        "service_slug": "consulting",
        "phone": "+61400000000",
    }
    first, _ = call(client, "ingest_leads", {"leads": [lead]})
    assert first["results"][0]["status"] == "created"
    assert first["results"][0]["partner_id"]
    assert first["results"][0]["deal_id"]
    second, _ = call(client, "ingest_leads", {"leads": [lead]})
    assert second["results"][0]["status"] == "duplicate_open_deal"
    partner = get_partner_by_email("jane@acme.example")
    assert len(list_deals(partner_id=partner["id"])) == 1


def test_create_deal_skips_duplicate_open(client, db, monkeypatch):
    _enable(monkeypatch)
    service_id = create_service("Consulting", "consulting")
    partner_id = create_partner("Jane", email="jane@example.com", phone="0400000000")
    first, _ = call(client, "create_deal", {
        "partner_id": partner_id, "service_id": service_id, "source": "referral",
    })
    assert first["status"] == "created"
    second, _ = call(client, "create_deal", {
        "partner_id": partner_id, "service_slug": "consulting",
    })
    assert second["status"] == "duplicate_open_deal"
    assert second["deal"]["id"] == first["deal"]["id"]


def test_update_partner_fill_empty_only_by_default(client, db, monkeypatch):
    _enable(monkeypatch)
    partner_id = create_partner("Jane", email="jane@example.com", title="Director")
    unchanged, _ = call(client, "update_partner", {
        "partner_id": partner_id, "title": "CEO",
    })
    assert unchanged["status"] == "unchanged"
    assert get_partner(partner_id)["title"] == "Director"

    filled, _ = call(client, "update_partner", {
        "partner_id": partner_id, "phone": "0400111222",
    })
    assert filled["status"] == "updated"
    assert get_partner(partner_id)["phone"] == "0400111222"
    assert get_partner(partner_id)["title"] == "Director"

    overwritten, _ = call(client, "update_partner", {
        "partner_id": partner_id, "title": "CEO", "fill_empty_only": False,
    })
    assert overwritten["partner"]["title"] == "CEO"


def test_set_stage_and_log_activity(client, db, monkeypatch):
    _enable(monkeypatch)
    service_id = create_service("Consulting", "consulting")
    partner_id = create_partner("Jane", email="jane@example.com")
    deal_id = create_deal(partner_id, service_id)
    moved, _ = call(client, "set_deal_stage", {"deal_id": deal_id, "stage": "qualified"})
    assert moved["ok"] is True
    assert get_deal(deal_id)["stage"] == "qualified"

    logged, _ = call(client, "log_activity", {
        "partner_id": partner_id, "deal_id": deal_id, "type": "note", "body": "Left a voicemail",
    })
    assert logged["ok"] is True
    assert logged["activity"]["type"] == "note"

    system, is_error = call(client, "log_activity", {
        "partner_id": partner_id, "type": "system", "body": "should refuse",
    })
    assert is_error is True
    assert system["error"] == "invalid_type"


def test_record_call_outcome(client, db, monkeypatch):
    _enable(monkeypatch)
    service_id = create_service("Consulting", "consulting")
    partner_id = create_partner("Jane", email="jane@example.com", phone="0400000000")
    deal_id = create_deal(partner_id, service_id, stage="qualified")
    payload, _ = call(client, "record_call_outcome", {
        "deal_id": deal_id, "outcome": "no_answer", "note": "tried twice",
    })
    assert payload["ok"] is True
    assert get_deal(deal_id)["stage"] == "qualified"
    bodies = [a["body"] for a in list_activities_for_deal(deal_id)]
    assert any("No answer" in (b or "") for b in bodies)


def test_unknown_tool_is_error_result(client, db, monkeypatch):
    _enable(monkeypatch)
    payload, is_error = call(client, "drop_table")
    assert is_error is True
    assert payload["error"] == "unknown_tool"


def test_get_missing_partner(client, db, monkeypatch):
    _enable(monkeypatch)
    payload, is_error = call(client, "get_partner", {"partner_id": 999})
    assert is_error is True
    assert payload["error"] == "not_found"


def test_list_catalog_and_call_queue(client, db, monkeypatch):
    _enable(monkeypatch)
    catalog, _ = call(client, "list_catalog")
    keys = [s["key"] for s in catalog["stages"]]
    assert "qualified" in keys
    assert "no_answer" in [o["key"] for o in catalog["call_outcomes"]]
    queue, _ = call(client, "get_call_queue")
    assert queue["ok"] is True
    assert queue["calls"] == []


def test_list_deals_rejects_unknown_stage(client, db, monkeypatch):
    _enable(monkeypatch)
    payload, is_error = call(client, "list_deals", {"stage": "bogus"})
    assert is_error is True
    assert payload["error"] == "invalid_stage"


def test_create_partner_existing_email(client, db, monkeypatch):
    _enable(monkeypatch)
    create_partner("Jane", email="jane@example.com")
    payload, _ = call(client, "create_partner", {"name": "Other", "email": "jane@example.com"})
    assert payload["status"] == "existing"
    assert payload["partner"]["name"] == "Jane"
