import os

from app.services.catalog import create_service
from app.services.partners import get_partner_by_email
from app.services.deals import list_deals


def _headers(key="test-crm-api-key"):
    return {"X-API-Key": key}


def test_new_lead_creates_partner_and_deal(client, monkeypatch):
    monkeypatch.setenv("CRM_API_KEY", "test-crm-api-key")
    create_service("Consulting", "consulting")

    resp = client.post("/api/v1/leads", json={
        "leads": [{"name": "Jane Doe", "email": "jane@acme.example", "service_slug": "consulting"}]
    }, headers=_headers())

    assert resp.status_code == 200
    created = resp.json()["results"][0]
    assert created["email"] == "jane@acme.example"
    assert created["status"] == "created"
    assert created["partner_id"]
    assert created["deal_id"]

    partner = get_partner_by_email("jane@acme.example")
    assert partner is not None
    deals = list_deals(partner_id=partner["id"])
    assert len(deals) == 1


def test_company_name_creates_parent_company(client, monkeypatch):
    monkeypatch.setenv("CRM_API_KEY", "test-crm-api-key")
    create_service("Consulting", "consulting")

    client.post("/api/v1/leads", json={
        "leads": [{
            "name": "Jane Doe", "email": "jane@acme.example",
            "company_name": "Acme Childcare", "service_slug": "consulting",
        }]
    }, headers=_headers())

    partner = get_partner_by_email("jane@acme.example")
    assert partner["parent_id"] is not None


def test_resubmitting_same_lead_is_skipped_as_duplicate(client, monkeypatch):
    monkeypatch.setenv("CRM_API_KEY", "test-crm-api-key")
    create_service("Consulting", "consulting")
    lead = {"name": "Jane Doe", "email": "jane@acme.example", "service_slug": "consulting"}

    r1 = client.post("/api/v1/leads", json={"leads": [lead]}, headers=_headers())
    r2 = client.post("/api/v1/leads", json={"leads": [lead]}, headers=_headers())

    assert r1.json()["results"][0]["status"] == "created"
    assert r2.json()["results"][0]["status"] == "duplicate_open_deal"

    partner = get_partner_by_email("jane@acme.example")
    assert len(list_deals(partner_id=partner["id"])) == 1


def test_same_email_different_service_creates_second_deal(client, monkeypatch):
    monkeypatch.setenv("CRM_API_KEY", "test-crm-api-key")
    create_service("Consulting", "consulting")
    create_service("Website Rebuild", "website-rebuild")

    client.post("/api/v1/leads", json={"leads": [
        {"name": "Jane Doe", "email": "jane@acme.example", "service_slug": "consulting"}
    ]}, headers=_headers())
    r2 = client.post("/api/v1/leads", json={"leads": [
        {"name": "Jane Doe", "email": "jane@acme.example", "service_slug": "website-rebuild"}
    ]}, headers=_headers())

    assert r2.json()["results"][0]["status"] == "existing_partner_new_deal"
    partner = get_partner_by_email("jane@acme.example")
    assert len(list_deals(partner_id=partner["id"])) == 2


def test_missing_name_is_invalid(client, monkeypatch):
    monkeypatch.setenv("CRM_API_KEY", "test-crm-api-key")
    create_service("Consulting", "consulting")

    resp = client.post("/api/v1/leads", json={
        "leads": [{"email": "nobody@example.com", "service_slug": "consulting"}]
    }, headers=_headers())

    assert resp.json()["results"][0]["status"] == "invalid"


def test_unknown_service_slug_is_invalid_service(client, monkeypatch):
    monkeypatch.setenv("CRM_API_KEY", "test-crm-api-key")

    resp = client.post("/api/v1/leads", json={
        "leads": [{"name": "Jane Doe", "service_slug": "does-not-exist"}]
    }, headers=_headers())

    assert resp.json()["results"][0]["status"] == "invalid_service"


def test_wrong_api_key_rejected(client, monkeypatch):
    monkeypatch.setenv("CRM_API_KEY", "test-crm-api-key")
    resp = client.post("/api/v1/leads", json={"leads": [{"name": "X", "service_slug": "consulting"}]},
                        headers=_headers("wrong-key"))
    assert resp.status_code == 401


def test_missing_api_key_env_fails_closed(client, monkeypatch):
    monkeypatch.delenv("CRM_API_KEY", raising=False)
    resp = client.post("/api/v1/leads", json={"leads": [{"name": "X", "service_slug": "consulting"}]},
                        headers=_headers("anything"))
    assert resp.status_code == 401


def test_empty_leads_list_rejected(client, monkeypatch):
    monkeypatch.setenv("CRM_API_KEY", "test-crm-api-key")
    resp = client.post("/api/v1/leads", json={"leads": []}, headers=_headers())
    assert resp.status_code == 422
