from app.services.activities import list_activities_for_partner
from app.services.catalog import create_service
from app.services.deals import list_deals
from app.services.leads import ingest_lead
from app.services.partners import (
    get_partner,
    get_partner_by_email,
    list_partners,
    update_partner,
)


def _consulting(db):
    return create_service("Consulting", "consulting")


def test_title_saved_on_create(db):
    _consulting(db)
    result = ingest_lead({
        "name": "Jane Doe",
        "email": "jane@acme.example",
        "title": "Director",
        "service_slug": "consulting",
    })

    assert result["status"] == "created"
    partner = get_partner_by_email("jane@acme.example")
    assert partner["title"] == "Director"


def test_second_ingest_fills_blank_phone_only(db):
    _consulting(db)
    ingest_lead({
        "name": "Jane Doe",
        "email": "jane@acme.example",
        "title": "Director",
        "service_slug": "consulting",
    })

    result = ingest_lead({
        "name": "Jane Doe",
        "email": "jane@acme.example",
        "title": "CEO",
        "phone": "0400 000 000",
        "service_slug": "consulting",
    })

    assert result["status"] == "duplicate_open_deal"
    partner = get_partner_by_email("jane@acme.example")
    assert partner["phone"] == "0400 000 000"
    assert partner["title"] == "Director"


def test_second_ingest_does_not_overwrite_edited_title(db):
    _consulting(db)
    ingest_lead({
        "name": "Jane Doe",
        "email": "jane@acme.example",
        "title": "Director",
        "service_slug": "consulting",
    })
    partner = get_partner_by_email("jane@acme.example")
    update_partner(partner["id"], title="Managing Director")

    result = ingest_lead({
        "name": "Jane Doe",
        "email": "jane@acme.example",
        "title": "CEO",
        "service_slug": "consulting",
    })

    assert result["status"] == "duplicate_open_deal"
    assert get_partner(partner["id"])["title"] == "Managing Director"


def test_missing_email_and_phone_is_invalid(db):
    _consulting(db)
    result = ingest_lead({
        "name": "Jane Doe",
        "service_slug": "consulting",
    })

    assert result["status"] == "invalid"
    assert list_partners() == []


def test_activity_logged_only_on_create_paths(db):
    _consulting(db)
    create_service("Website Rebuild", "website-rebuild")

    created = ingest_lead({
        "name": "Jane Doe",
        "email": "jane@acme.example",
        "service_slug": "consulting",
    })
    assert created["status"] == "created"
    partner = get_partner_by_email("jane@acme.example")
    ingest_notes = [a for a in list_activities_for_partner(partner["id"])
                    if a["body"] == "Deal created (lead ingest)"]
    assert len(ingest_notes) == 1
    assert ingest_notes[0]["type"] == "system"
    bodies = [a["body"] for a in list_activities_for_partner(partner["id"])]
    assert "Lead ingested via API" not in bodies
    assert bodies.count("Deal created") == 0

    duplicate = ingest_lead({
        "name": "Jane Doe",
        "email": "jane@acme.example",
        "service_slug": "consulting",
    })
    assert duplicate["status"] == "duplicate_open_deal"
    ingest_notes = [a for a in list_activities_for_partner(partner["id"])
                    if a["body"] == "Deal created (lead ingest)"]
    assert len(ingest_notes) == 1

    new_deal = ingest_lead({
        "name": "Jane Doe",
        "email": "jane@acme.example",
        "service_slug": "website-rebuild",
    })
    assert new_deal["status"] == "existing_partner_new_deal"
    ingest_notes = [a for a in list_activities_for_partner(partner["id"])
                    if a["body"] == "Deal created (lead ingest)"]
    assert len(ingest_notes) == 2

    invalid = ingest_lead({"name": "Nobody", "service_slug": "consulting"})
    assert invalid["status"] == "invalid"

    invalid_service = ingest_lead({
        "name": "Somebody",
        "email": "somebody@example.com",
        "service_slug": "does-not-exist",
    })
    assert invalid_service["status"] == "invalid_service"
    assert get_partner_by_email("somebody@example.com") is None


def test_phone_only_dedups_by_name_and_company(db):
    _consulting(db)
    first = ingest_lead({
        "name": "Jane Doe",
        "phone": "0400 000 000",
        "company_name": "Acme Childcare",
        "service_slug": "consulting",
    })
    second = ingest_lead({
        "name": "Jane Doe",
        "phone": "0400 111 111",
        "company_name": "Acme Childcare",
        "service_slug": "consulting",
    })

    assert first["status"] == "created"
    assert second["status"] == "duplicate_open_deal"
    people = [p for p in list_partners() if not p["is_company"]]
    assert len(people) == 1
    assert people[0]["phone"] == "0400 000 000"
    assert len(list_deals(partner_id=people[0]["id"])) == 1


def test_numeric_phone_does_not_raise(db):
    _consulting(db)
    result = ingest_lead({
        "name": "Jane Doe",
        "phone": 61412345678,
        "service_slug": "consulting",
    })
    assert result["status"] == "created"
    people = [p for p in list_partners() if not p["is_company"]]
    assert len(people) == 1
    assert people[0]["phone"] == "61412345678"


def test_numeric_phone_does_not_sink_batch(client, db, monkeypatch):
    monkeypatch.setenv("CRM_API_KEY", "test-crm-api-key")
    _consulting(db)
    resp = client.post("/api/v1/leads", json={
        "leads": [
            {"name": "Numeric Phone", "phone": 61412345678, "service_slug": "consulting"},
            {
                "name": "Jane Doe",
                "email": "jane@acme.example",
                "service_slug": "consulting",
            },
        ]
    }, headers={"X-API-Key": "test-crm-api-key"})
    assert resp.status_code == 200
    assert [r["status"] for r in resp.json()["results"]] == ["created", "created"]
    assert get_partner_by_email("jane@acme.example") is not None


def test_numeric_phone_fills_blank_on_email_match(db):
    _consulting(db)
    ingest_lead({
        "name": "Jane Doe",
        "email": "jane@acme.example",
        "service_slug": "consulting",
    })
    ingest_lead({
        "name": "Jane Doe",
        "email": "jane@acme.example",
        "phone": 61412345678,
        "service_slug": "consulting",
    })
    assert get_partner_by_email("jane@acme.example")["phone"] == "61412345678"


def test_phone_only_company_name_drift_matches_on_phone_and_name(db):
    """Phone+name is identity. Company spelling drift or omit must not duplicate."""
    _consulting(db)
    first = ingest_lead({
        "name": "Jane Doe",
        "phone": "0400 000 000",
        "company_name": "Acme Childcare",
        "service_slug": "consulting",
    })
    drifted = ingest_lead({
        "name": "Jane Doe",
        "phone": "0400 000 000",
        "company_name": "Acme Pty Ltd",
        "service_slug": "consulting",
    })
    omitted = ingest_lead({
        "name": "Jane Doe",
        "phone": "0400 000 000",
        "service_slug": "consulting",
    })
    assert first["status"] == "created"
    assert drifted["status"] == "duplicate_open_deal"
    assert omitted["status"] == "duplicate_open_deal"
    people = [p for p in list_partners() if not p["is_company"]]
    assert len(people) == 1
    assert len(list_deals()) == 1


def test_is_company_flag_sets_the_lead_itself_as_a_company(db):
    """A business-only lead (no named contact) should be stored as a company,
    not a person — is_company is the lead's own flag, distinct from
    company_name, which creates a separate parent record."""
    _consulting(db)
    result = ingest_lead({
        "name": "Acme Removal Co",
        "phone": "1300 681 034",
        "is_company": True,
        "service_slug": "consulting",
    })

    assert result["status"] == "created"
    partners = list_partners()
    assert len(partners) == 1
    assert partners[0]["is_company"] == 1
    assert partners[0]["name"] == "Acme Removal Co"


def test_is_company_lead_dedups_by_name_on_repost(db):
    """A re-run of the same company-only ETL lead (no email) must match the
    existing company record, not create a duplicate — same dedup guarantee
    the API already gives person leads."""
    _consulting(db)
    first = ingest_lead({
        "name": "Acme Removal Co",
        "phone": "1300 681 034",
        "is_company": True,
        "service_slug": "consulting",
    })
    second = ingest_lead({
        "name": "Acme Removal Co",
        "phone": "1300 681 034",
        "is_company": True,
        "service_slug": "consulting",
    })

    assert first["status"] == "created"
    assert second["status"] == "duplicate_open_deal"
    assert len(list_partners()) == 1


def test_is_company_true_string_is_truthy(db):
    """ETL payloads may send is_company as a string ('true'/'1') rather than
    a JSON boolean — both must be honored the same way."""
    _consulting(db)
    ingest_lead({
        "name": "Nanotise",
        "phone": "1800 626 684",
        "is_company": "true",
        "service_slug": "consulting",
    })
    assert list_partners()[0]["is_company"] == 1


def test_company_and_phone_without_email_creates_company_deal(db):
    create_service("Dead Lead Reactivation", "dead-lead-reactivation")
    result = ingest_lead({
        "company_name": "Cafe North",
        "phone": "0400 222 333",
        "service_slug": "dead-lead-reactivation",
        "source": "scrape:cafes",
    })
    assert result["status"] == "created"
    assert result["partner_id"]
    assert result["deal_id"]
    partners = list_partners()
    assert len(partners) == 1
    assert partners[0]["is_company"] == 1
    assert partners[0]["name"] == "Cafe North"
    assert partners[0]["phone"] == "0400 222 333"
    assert partners[0]["email"] is None
    deals = list_deals(partner_id=partners[0]["id"])
    assert len(deals) == 1
    assert deals[0]["source"] == "scrape:cafes"


def test_website_only_ingest_is_valid(db):
    _consulting(db)
    result = ingest_lead({
        "name": "Hui Shin",
        "website": "https://huishin.example",
        "service_slug": "consulting",
    })
    assert result["status"] == "created"
    people = [p for p in list_partners() if not p["is_company"]]
    assert people[0]["website"] == "https://huishin.example"
