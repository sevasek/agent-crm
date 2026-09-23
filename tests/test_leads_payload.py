from app.services.catalog import create_service
from app.services.deals import list_deals, get_deal
from app.services.leads import ingest_lead, sanitize_lead_payload
from app.services.partners import get_partner_by_email


def _headers(key="test-crm-api-key"):
    return {"X-API-Key": key}


def test_sanitize_drops_unknown_keys():
    cleaned = sanitize_lead_payload({
        "name": "Jane Doe",
        "email": "jane@acme.example",
        "is_admin": True,
        "password": "secret",
        "__proto__": {"polluted": True},
        "extra": {"nested": 1},
    })
    assert "is_admin" not in cleaned
    assert "password" not in cleaned
    assert "__proto__" not in cleaned
    assert "extra" not in cleaned
    assert cleaned["name"] == "Jane Doe"
    assert cleaned["email"] == "jane@acme.example"


def test_extra_keys_ignored_on_ingest(db):
    create_service("Consulting", "consulting")
    result = ingest_lead({
        "name": "Jane Doe",
        "email": "jane@acme.example",
        "service_slug": "consulting",
        "is_admin": True,
        "role": "superuser",
        "password": "nope",
    })
    assert result["status"] == "created"
    partner = get_partner_by_email("jane@acme.example")
    assert partner is not None
    assert "is_admin" not in partner
    assert "password" not in partner
    assert "role" not in partner


def test_value_estimate_numeric_string_stores_number(db):
    create_service("Consulting", "consulting")
    result = ingest_lead({
        "name": "Jane Doe",
        "email": "jane@acme.example",
        "service_slug": "consulting",
        "value_estimate": "5000",
    })
    assert result["status"] == "created"
    partner = get_partner_by_email("jane@acme.example")
    deals = list_deals(partner_id=partner["id"])
    assert len(deals) == 1
    assert deals[0]["value_estimate"] == 5000
    assert not isinstance(deals[0]["value_estimate"], str)


def test_value_estimate_object_does_not_raise_or_store_dict(db):
    create_service("Consulting", "consulting")
    result = ingest_lead({
        "name": "Jane Doe",
        "email": "jane@acme.example",
        "service_slug": "consulting",
        "value_estimate": {"x": 1},
    })
    assert result["status"] == "created"
    partner = get_partner_by_email("jane@acme.example")
    deal = list_deals(partner_id=partner["id"])[0]
    assert deal["value_estimate"] is None
    stored = get_deal(deal["id"])
    assert not isinstance(stored["value_estimate"], dict)


def test_nested_object_in_name_is_invalid(db):
    create_service("Consulting", "consulting")
    result = ingest_lead({
        "name": {"first": "Jane", "last": "Doe"},
        "email": "jane@acme.example",
        "service_slug": "consulting",
    })
    assert result == {"email": "jane@acme.example", "status": "invalid"}
    assert get_partner_by_email("jane@acme.example") is None


def test_nested_object_in_text_field_does_not_raise(db):
    create_service("Consulting", "consulting")
    result = ingest_lead({
        "name": "Jane Doe",
        "email": "jane@acme.example",
        "service_slug": "consulting",
        "phone": {"mobile": "0400"},
        "preferred_channel": {"kind": "email"},
        "pain_points": ["scattergun systems"],
        "next_action_date": {"year": 2026},
    })
    assert result["status"] == "created"
    partner = get_partner_by_email("jane@acme.example")
    assert partner["phone"] is None
    assert partner["preferred_channel"] is None
    deal = list_deals(partner_id=partner["id"])[0]
    assert deal["pain_points"] is None
    assert deal["next_action_date"] is None


def test_team_size_numeric_string_and_garbage(db):
    create_service("Consulting", "consulting")
    ingest_lead({
        "name": "Jane Doe",
        "email": "jane@acme.example",
        "service_slug": "consulting",
        "team_size": "12",
    })
    partner = get_partner_by_email("jane@acme.example")
    assert partner["team_size"] == 12

    ingest_lead({
        "name": "Someone Else",
        "email": "someone@other.example",
        "service_slug": "consulting",
        "team_size": "not-a-number",
    })
    other = get_partner_by_email("someone@other.example")
    assert other["team_size"] is None

    ingest_lead({
        "name": "Third Person",
        "email": "third@other.example",
        "service_slug": "consulting",
        "team_size": {"count": 5},
    })
    third = get_partner_by_email("third@other.example")
    assert third["team_size"] is None


def test_title_passed_to_create_partner(db):
    create_service("Consulting", "consulting")
    ingest_lead({
        "name": "Jane Doe",
        "email": "jane@acme.example",
        "service_slug": "consulting",
        "title": "Director",
    })
    partner = get_partner_by_email("jane@acme.example")
    assert partner["title"] == "Director"


def test_non_dict_lead_is_invalid_not_raised(db):
    create_service("Consulting", "consulting")
    assert ingest_lead("not-an-object") == {"email": "", "status": "invalid"}
    assert ingest_lead(["name", "Jane"]) == {"email": "", "status": "invalid"}
    assert ingest_lead(None) == {"email": "", "status": "invalid"}


def test_bad_item_does_not_sink_batch(client, monkeypatch):
    monkeypatch.setenv("CRM_API_KEY", "test-crm-api-key")
    create_service("Consulting", "consulting")

    resp = client.post("/api/v1/leads", json={
        "leads": [
            {"name": {"first": "Nope"}, "email": "bad@example.com", "service_slug": "consulting"},
            {"name": "Jane Doe", "email": "jane@acme.example", "service_slug": "consulting",
             "value_estimate": {"x": 1}},
        ]
    }, headers=_headers())

    assert resp.status_code == 200
    results = resp.json()["results"]
    assert results[0] == {"email": "bad@example.com", "status": "invalid"}
    assert results[1]["email"] == "jane@acme.example"
    assert results[1]["status"] == "created"
    assert results[1]["partner_id"]
    assert get_partner_by_email("bad@example.com") is None
    partner = get_partner_by_email("jane@acme.example")
    assert partner is not None
    assert list_deals(partner_id=partner["id"])[0]["value_estimate"] is None


def test_as_number_rejects_non_finite_and_out_of_range():
    from app.services.leads import _as_number

    assert _as_number(float("inf"), integer=True) is None
    assert _as_number(float("-inf"), integer=True) is None
    assert _as_number(float("nan"), integer=True) is None
    assert _as_number(float("inf")) is None
    assert _as_number(float("nan")) is None
    assert _as_number("Infinity") is None
    assert _as_number("Infinity", integer=True) is None
    assert _as_number("1e400") is None
    assert _as_number("1e400", integer=True) is None
    assert _as_number(10**30, integer=True) is None
    assert _as_number(10**30) is None
    assert _as_number(2**63, integer=True) is None
    assert _as_number(2**63 - 1, integer=True) == 2**63 - 1
    assert _as_number(-(2**63), integer=True) == -(2**63)
    assert _as_number("0") == 0
    assert _as_number("0", integer=True) == 0


def test_non_finite_team_size_does_not_sink_batch(client, monkeypatch):
    monkeypatch.setenv("CRM_API_KEY", "test-crm-api-key")
    create_service("Consulting", "consulting")
    payload = (
        b'{"leads":['
        b'{"name":"Inf Size","email":"inf@example.com","service_slug":"consulting","team_size":Infinity},'
        b'{"name":"NaN Size","email":"nan@example.com","service_slug":"consulting","team_size":NaN},'
        b'{"name":"Huge Size","email":"huge@example.com","service_slug":"consulting",'
        b'"team_size":99999999999999999999999999999999},'
        b'{"name":"Jane Doe","email":"jane@acme.example","service_slug":"consulting"}'
        b"]}"
    )
    resp = client.post(
        "/api/v1/leads",
        content=payload,
        headers={**_headers(), "Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert [r["status"] for r in results] == ["created", "created", "created", "created"]
    assert get_partner_by_email("inf@example.com")["team_size"] is None
    assert get_partner_by_email("nan@example.com")["team_size"] is None
    assert get_partner_by_email("huge@example.com")["team_size"] is None
    assert get_partner_by_email("jane@acme.example") is not None


def test_non_finite_value_estimate_not_stored(db):
    create_service("Consulting", "consulting")
    for i, estimate in enumerate(
        (float("nan"), float("inf"), "Infinity", "1e400", 10**30)
    ):
        result = ingest_lead({
            "name": f"Lead {i}",
            "email": f"est{i}@example.com",
            "service_slug": "consulting",
            "value_estimate": estimate,
        })
        assert result["status"] == "created", estimate
        partner = get_partner_by_email(f"est{i}@example.com")
        deal = list_deals(partner_id=partner["id"])[0]
        assert deal["value_estimate"] is None, estimate


def test_zero_value_estimate_is_kept(db):
    create_service("Consulting", "consulting")
    ingest_lead({
        "name": "Jane Doe",
        "email": "jane@acme.example",
        "service_slug": "consulting",
        "value_estimate": "0",
    })
    partner = get_partner_by_email("jane@acme.example")
    assert list_deals(partner_id=partner["id"])[0]["value_estimate"] == 0
