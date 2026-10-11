"""ADR 0001 P3: ingest defaults to lead; type=opportunity keeps today's path."""
from app.services.catalog import create_service
from app.services.deals import get_deal, list_deals
from app.services.lead_records import convert_lead, create_lead
from app.services.leads import DEPRECATION_TYPE_OMITTED, ingest_lead
from app.services.partners import create_partner, get_partner, list_partners


def _partners_count():
    return len(list_partners())


def _consulting(db):
    return create_service("Consulting", "consulting")


def test_default_ingest_creates_lead_and_not_a_partner(db):
    _consulting(db)
    before = _partners_count()
    result = ingest_lead({
        "name": "Pat Manager",
        "email": "pm@harbourphysio.example",
        "company_name": "Harbour Physio",
        "website": "https://harbourphysio.example",
        "service_slug": "consulting",
        "source": "research:directory",
    })
    assert result["status"] == "created"
    assert result["type"] == "lead"
    assert result["lead_id"] == result["deal_id"]
    assert result["partner_id"] is None
    assert result["also_matched"] == []
    assert result["type_defaulted"] is True
    assert _partners_count() == before
    lead = get_deal(result["deal_id"])
    assert lead["type"] == "lead"
    assert lead["contact_name"] == "Pat Manager"
    assert lead["company_name"] == "Harbour Physio"
    assert lead["email"] == "pm@harbourphysio.example"
    assert list_deals() == []
    assert list_deals(type="lead")[0]["id"] == lead["id"]


def test_lead_ingest_never_changes_existing_partner(db):
    _consulting(db)
    partner_id = create_partner("Harbour Physio", is_company=True, email="pm@harbourphysio.example")
    before = get_partner(partner_id)
    result = ingest_lead({
        "type": "lead",
        "name": "Pat Manager",
        "email": "pm@harbourphysio.example",
        "phone": "0400 111 222",
        "title": "Practice manager",
    })
    assert result["status"] == "created"
    assert result["partner_id"] == partner_id
    after = get_partner(partner_id)
    assert after["phone"] == before["phone"]
    assert after["name"] == before["name"]
    lead = get_deal(result["deal_id"])
    assert lead["partner_id"] == partner_id
    assert lead["phone"] == "0400 111 222"


def test_weak_key_only_creates_a_new_lead(db):
    _consulting(db)
    first = ingest_lead({
        "type": "lead",
        "company_name": "Cafe North",
        "phone": "0400 222 333",
    })
    # Same phone, different name — phone-only is a weak key (Q9).
    second = ingest_lead({
        "type": "lead",
        "company_name": "Cafe South",
        "phone": "0400 222 333",
    })
    assert first["status"] == "created"
    assert second["status"] == "created"
    assert second["deal_id"] != first["deal_id"]
    # Website-only is also weak.
    site_a = ingest_lead({
        "type": "lead",
        "name": "Hui Shin",
        "website": "https://shared-host.example",
    })
    site_b = ingest_lead({
        "type": "lead",
        "name": "Other Person",
        "website": "https://shared-host.example",
    })
    assert site_a["status"] == "created"
    assert site_b["status"] == "created"
    assert site_b["deal_id"] != site_a["deal_id"]


def test_strong_email_match_merges_oldest_and_lists_also_matched(db):
    first, _ = create_lead(company_name="Harbour One", email="same@harbour.example")
    second, _ = create_lead(company_name="Harbour Two", email="same@harbour.example")
    result = ingest_lead({
        "type": "lead",
        "name": "Pat",
        "email": "same@harbour.example",
        "phone": "0400 000 111",
        "title": "Director",
    })
    assert result["status"] == "duplicate_open_lead"
    assert result["deal_id"] == first["id"]
    assert result["lead_id"] == first["id"]
    assert result["also_matched"] == [second["id"]]
    filled = get_deal(first["id"])
    assert filled["phone"] == "0400 000 111"
    assert filled["title"] == "Director"
    assert get_deal(second["id"])["phone"] is None


def test_strong_match_on_opportunity_fills_deal_fields_only(db):
    _consulting(db)
    lead, _ = create_lead(
        company_name="Harbour Physio",
        contact_name="Pat Manager",
        email="pm@harbourphysio.example",
        website="https://harbourphysio.example",
    )
    converted, error = convert_lead(
        lead["id"], service_slug="consulting", partner_action="create",
    )
    assert error is None
    partner_phone = get_partner(converted["partner"]["id"])["phone"]
    result = ingest_lead({
        "type": "lead",
        "name": "Pat Manager",
        "email": "pm@harbourphysio.example",
        "phone": "0400 999 000",
        "title": "Should stay off the opportunity",
        "pain_points": "Phone-only booking",
        "value_estimate": 4800,
    })
    assert result["status"] == "duplicate_open_opportunity"
    assert result["type"] == "opportunity"
    assert result["lead_id"] is None
    assert result["deal_id"] == lead["id"]
    deal = get_deal(lead["id"])
    assert deal["pain_points"] == "Phone-only booking"
    assert deal["value_estimate"] == 4800
    assert deal.get("title") in (None, "")
    assert get_partner(converted["partner"]["id"])["phone"] == partner_phone


def test_omitted_type_sets_deprecation_header(client, db, monkeypatch):
    monkeypatch.setenv("CRM_API_KEY", "test-crm-api-key")
    _consulting(db)
    before = _partners_count()
    resp = client.post("/api/v1/leads", json={
        "leads": [{
            "name": "Pat Manager",
            "email": "pm@harbourphysio.example",
            "company_name": "Harbour Physio",
        }]
    }, headers={"X-API-Key": "test-crm-api-key"})
    assert resp.status_code == 200
    assert resp.headers.get("deprecation") == DEPRECATION_TYPE_OMITTED
    item = resp.json()["results"][0]
    assert item["type_defaulted"] is True
    assert item["type"] == "lead"
    assert item["status"] == "created"
    assert _partners_count() == before


def test_explicit_type_skips_deprecation_header(client, db, monkeypatch):
    monkeypatch.setenv("CRM_API_KEY", "test-crm-api-key")
    resp = client.post("/api/v1/leads", json={
        "leads": [{
            "type": "lead",
            "company_name": "Harbour Physio",
            "website": "https://harbourphysio.example",
        }]
    }, headers={"X-API-Key": "test-crm-api-key"})
    assert resp.status_code == 200
    assert "deprecation" not in {k.lower() for k in resp.headers}
    assert resp.json()["results"][0].get("type_defaulted") is None


def test_opportunity_type_still_creates_partner(db):
    _consulting(db)
    before = _partners_count()
    result = ingest_lead({
        "type": "opportunity",
        "name": "Jane Doe",
        "email": "jane@acme.example",
        "service_slug": "consulting",
    })
    assert result["status"] == "created"
    assert result["type"] == "opportunity"
    assert result["lead_id"] is None
    assert result["partner_id"]
    assert _partners_count() == before + 1


def test_is_company_maps_name_to_company_name(db):
    result = ingest_lead({
        "type": "lead",
        "name": "Acme Removal Co",
        "phone": "1300 681 034",
        "is_company": True,
    })
    assert result["status"] == "created"
    lead = get_deal(result["deal_id"])
    assert lead["company_name"] == "Acme Removal Co"
    assert not lead.get("contact_name")
    assert _partners_count() == 0


def test_lead_ingest_optional_service_and_invalid_service(db):
    result = ingest_lead({
        "type": "lead",
        "company_name": "No Service Yet",
        "website": "https://noservice.example",
    })
    assert result["status"] == "created"
    assert get_deal(result["deal_id"])["service_id"] is None
    bad = ingest_lead({
        "type": "lead",
        "company_name": "Bad Service",
        "website": "https://bad.example",
        "service_slug": "does-not-exist",
    })
    assert bad["status"] == "invalid_service"
    assert _partners_count() == 0
