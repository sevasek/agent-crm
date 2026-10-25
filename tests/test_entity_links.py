"""Relationships shown on record pages: partner, company, deal, lead, and definitions."""
from app.services.activities import log_activity
from app.services.campaigns import create_campaign
from app.services.catalog import create_service
from app.services.deals import create_deal, open_record_counts
from app.services.delegated_tasks import create_task, list_tasks
from app.services.lead_records import convert_lead, create_lead
from app.services.offers import create_offer
from app.services.partner_tags import apply_partner_tags
from app.services.partners import create_partner, list_partners


def _company_with_person():
    company_id = create_partner("Acme Retail", is_company=True)
    person_id = create_partner("Jane Doe", parent_id=company_id, title="Director")
    return company_id, person_id


def test_partner_page_shows_partner_tags(logged_in_client):
    pid = create_partner("Tagged Co", is_company=True)
    apply_partner_tags(pid, replace=["dental", "sydney"])
    resp = logged_in_client.get(f"/contacts/partners/{pid}")
    assert 'href="/contacts/partners?tag=dental"' in resp.text
    assert 'href="/contacts/partners?tag=sydney"' in resp.text


def test_partner_page_lists_linked_leads(logged_in_client):
    pid = create_partner("Jane Doe")
    lead, err = create_lead(contact_name="Jane Doe", email="jane@example.com", partner_id=pid)
    assert err is None
    resp = logged_in_client.get(f"/contacts/partners/{pid}")
    assert "<h2>Leads</h2>" in resp.text
    assert f'href="/crm/leads/{lead["id"]}"' in resp.text


def test_company_page_rolls_up_people_deals_and_activity(logged_in_client):
    company_id, person_id = _company_with_person()
    sid = create_service("Consulting", "consulting")
    deal_id = create_deal(person_id, sid)
    log_activity(person_id, "call", "Spoke about the audit", deal_id=deal_id)
    resp = logged_in_client.get(f"/contacts/partners/{company_id}")
    assert f'<a href="/crm/deals/{deal_id}/edit">Consulting</a>' in resp.text
    assert "Recent activity with these people" in resp.text
    assert "Spoke about the audit" in resp.text


def test_deal_card_shows_offer_owner_campaign_and_parent_link(logged_in_client):
    pid = create_partner("Jane Doe")
    sid = create_service("Consulting", "consulting")
    offer, _ = create_offer("Audit package", service_id=sid)
    campaign, err = create_campaign("Spring push", slug="spring-push")
    assert err is None
    parent_id = create_deal(pid, sid)
    child_id = create_deal(
        pid, sid, offer_id=offer["id"], owner_key="alice",
        parent_deal_id=parent_id, tags=["campaign:spring-push"],
    )
    assert child_id
    resp = logged_in_client.get(f"/contacts/partners/{pid}")
    assert "Audit package" in resp.text
    assert "owner alice" in resp.text
    assert f'href="/crm/campaigns/{campaign["id"]}">Spring push</a>' in resp.text
    assert f'<a href="/crm/deals/{parent_id}/edit">deal #{parent_id}</a>' in resp.text


def test_partner_and_deal_pages_show_delegated_tasks(logged_in_client):
    pid = create_partner("Jane Doe")
    sid = create_service("Consulting", "consulting")
    deal_id = create_deal(pid, sid)
    task, err, _created = create_task(deal_id, "Research the practice", owner="agent")
    assert err is None
    assert [t["id"] for t in list_tasks(partner_id=pid)] == [task["id"]]
    for url in (f"/contacts/partners/{pid}", f"/crm/deals/{deal_id}/edit"):
        resp = logged_in_client.get(url)
        assert "<h2>Delegated tasks</h2>" in resp.text
        assert "Research the practice" in resp.text


def test_deal_page_links_partner_and_company_and_shows_activity(logged_in_client):
    company_id, person_id = _company_with_person()
    sid = create_service("Consulting", "consulting")
    deal_id = create_deal(person_id, sid)
    log_activity(person_id, "note", "Sent the proposal", deal_id=deal_id)
    resp = logged_in_client.get(f"/crm/deals/{deal_id}/edit")
    assert f'<a href="/contacts/partners/{person_id}">Jane Doe</a>' in resp.text
    assert f'at <a href="/contacts/partners/{company_id}">Acme Retail</a>' in resp.text
    assert "<h2>Activity</h2>" in resp.text
    assert "Sent the proposal" in resp.text


def test_lead_page_links_partner(logged_in_client):
    pid = create_partner("Jane Doe")
    lead, _ = create_lead(contact_name="Jane Doe", email="jane@example.com", partner_id=pid)
    resp = logged_in_client.get(f"/crm/leads/{lead['id']}")
    assert f'Linked to <a href="/contacts/partners/{pid}">Jane Doe</a>' in resp.text


def test_converted_lead_url_redirects_to_its_deal(logged_in_client):
    create_service("Consulting", "consulting")
    lead, _ = create_lead(
        contact_name="Jane Doe", email="jane@example.com", service_slug="consulting",
    )
    result, err, *_ = convert_lead(lead["id"], partner_action="create")
    assert err is None, err
    resp = logged_in_client.get(f"/crm/leads/{lead['id']}", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/crm/deals/{lead['id']}/edit"


def test_partners_list_shows_and_sorts_by_company(logged_in_client):
    company_id, person_id = _company_with_person()
    create_partner("Zed Smith")
    rows = {p["id"]: p for p in list_partners(sort="company")}
    assert rows[person_id]["company_name"] == "Acme Retail"
    resp = logged_in_client.get("/contacts/partners")
    assert f'<a href="/contacts/partners/{company_id}">Acme Retail</a>' in resp.text
    assert "sort=company" in resp.text


def test_services_and_offers_pages_show_usage(logged_in_client):
    pid = create_partner("Jane Doe")
    sid = create_service("Consulting", "consulting")
    offer, _ = create_offer("Audit package", service_id=sid)
    create_deal(pid, sid, offer_id=offer["id"])
    create_lead(contact_name="Bob", email="bob@example.com", service_slug="consulting")
    campaign, _ = create_campaign("Spring push", slug="spring-push", service_id=sid)
    assert open_record_counts("service_id")[sid] == {"opportunity": 1, "lead": 1}
    resp = logged_in_client.get("/definitions/services")
    assert "1 open deal" in resp.text and "1 lead" in resp.text
    assert f'href="/crm/campaigns/{campaign["id"]}"' in resp.text
    resp = logged_in_client.get("/definitions/offers")
    assert "1 open deal" in resp.text
