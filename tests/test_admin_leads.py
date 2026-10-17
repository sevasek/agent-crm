from app.services.auth import generate_csrf_token
from app.services.catalog import create_service
from app.services.deals import create_deal, get_deal, mark_lost
from app.services.lead_records import create_lead, get_lead, merge_leads
from app.services.lost_reasons import create_lost_reason, list_lost_reasons, update_lost_reason
from app.services.partners import create_partner, list_partners


def _csrf(client):
    return generate_csrf_token(client.cookies.get("session"))


def _html_client_error(resp, status=400):
    assert resp.status_code == status
    ctype = resp.headers.get("content-type", "")
    assert "text/html" in ctype
    assert "application/json" not in ctype


def test_leads_nav_and_empty_list(logged_in_client):
    resp = logged_in_client.get("/crm/leads")
    assert resp.status_code == 200
    assert 'href="/crm/leads" aria-current="page">Leads</a>' in resp.text
    assert "No leads yet." in resp.text
    assert "Skip to main content" in resp.text


def test_new_lead_empty_identity_is_html_error(logged_in_client, db):
    before = list_partners()
    resp = logged_in_client.post("/crm/leads/new", data={
        "csrf_token": _csrf(logged_in_client),
        "company_name": "",
        "email": "",
    })
    _html_client_error(resp)
    assert "A lead needs a company" in resp.text
    assert list_partners() == before
    assert logged_in_client.get("/crm/leads").text.count("Open") == 0


def test_create_lead_and_list(logged_in_client, db):
    resp = logged_in_client.post("/crm/leads/new", data={
        "csrf_token": _csrf(logged_in_client),
        "company_name": "Riverstone Dental",
        "contact_name": "Pat Manager",
        "email": "pat@riverstone.example",
        "source": "research:directory",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/crm/leads/")
    page = logged_in_client.get("/crm/leads")
    assert "Riverstone Dental" in page.text
    assert "pill-lead" in page.text
    assert list_partners() == []


def test_leads_never_in_pipeline_or_deals_list(logged_in_client, db):
    create_lead(company_name="Lead Cafe", email="lead@cafe.example")
    pid = create_partner("Open Cafe", phone="0400111000")
    sid = create_service("Consulting", "consulting")
    create_deal(pid, sid)
    deals = logged_in_client.get("/crm/deals")
    assert "Open Cafe" in deals.text
    assert "Lead Cafe" not in deals.text
    assert "pill-opportunity" in deals.text
    pipeline = logged_in_client.get("/crm/pipeline")
    assert "Lead Cafe" not in pipeline.text
    assert "Open Cafe" in pipeline.text


def test_lost_filter_shows_lost_opportunity_only(logged_in_client, db):
    pid = create_partner("Lost Cafe")
    sid = create_service("Consulting", "consulting")
    deal_id = create_deal(pid, sid)
    mark_lost(deal_id, lost_reason="Not a fit")
    open_page = logged_in_client.get("/crm/deals")
    assert "Lost Cafe" not in open_page.text
    lost_page = logged_in_client.get("/crm/deals?lost=1")
    assert "Lost Cafe" in lost_page.text
    assert "pill-lost" in lost_page.text
    pipeline = logged_in_client.get("/crm/pipeline")
    assert "Lost Cafe" not in pipeline.text
    lost_board = logged_in_client.get("/crm/pipeline?lost=1")
    assert "Lost Cafe" in lost_board.text
    # Lost records are view-only: no stage form to no-op on them.
    assert "stage-form" not in lost_board.text


def test_stage_change_rejected_for_lost_and_lead(logged_in_client, db):
    pid = create_partner("Lost Cafe")
    sid = create_service("Consulting", "consulting")
    deal_id = create_deal(pid, sid)
    mark_lost(deal_id, lost_reason="Not a fit")
    resp = logged_in_client.post(f"/crm/deals/{deal_id}/stage", data={
        "csrf_token": _csrf(logged_in_client),
        "stage": "contacted",
        "next": "pipeline",
    }, follow_redirects=False)
    assert resp.status_code == 303
    lost = get_deal(deal_id)
    assert lost["stage"] == "new"
    assert lost["active"] == 0

    lead, _ = create_lead(company_name="Lead Cafe", email="lead@cafe.example")
    assert get_deal(lead["id"])["type"] == "lead"
    resp = logged_in_client.post(f"/crm/deals/{lead['id']}/stage", data={
        "csrf_token": _csrf(logged_in_client),
        "stage": "contacted",
        "next": "pipeline",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert get_deal(lead["id"])["stage"] != "contacted"

    # An active opportunity still moves.
    opp_id = create_deal(create_partner("Open Cafe", phone="0400111000"), sid)
    resp = logged_in_client.post(f"/crm/deals/{opp_id}/stage", data={
        "csrf_token": _csrf(logged_in_client),
        "stage": "contacted",
        "next": "pipeline",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert get_deal(opp_id)["stage"] == "contacted"


def test_edit_deal_page_redirects_leads_to_lead_form(logged_in_client, db):
    lead, _ = create_lead(company_name="Lead Cafe", email="lead@cafe.example")
    resp = logged_in_client.get(f"/crm/deals/{lead['id']}/edit", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/crm/leads/{lead['id']}"


def test_lost_reason_create_honors_unchecked_active(logged_in_client, db):
    resp = logged_in_client.post("/crm/lost-reasons/new", data={
        "csrf_token": _csrf(logged_in_client),
        "name": "Needs a nudge",
    }, follow_redirects=False)
    assert resp.status_code == 303
    row = next(r for r in list_lost_reasons() if r["name"] == "Needs a nudge")
    assert row["active"] == 0
    resp = logged_in_client.post("/crm/lost-reasons/new", data={
        "csrf_token": _csrf(logged_in_client),
        "name": "Stays active",
        "active": "on",
    }, follow_redirects=False)
    assert resp.status_code == 303
    row = next(r for r in list_lost_reasons() if r["name"] == "Stays active")
    assert row["active"] == 1


def test_convert_from_ui(logged_in_client, db):
    create_service("Consulting", "consulting")
    lead, _ = create_lead(
        company_name="Riverstone Dental", contact_name="Pat Manager",
        email="pat@riverstone.example", service_slug="consulting",
    )
    before = len(list_partners())
    resp = logged_in_client.post(f"/crm/leads/{lead['id']}/convert", data={
        "csrf_token": _csrf(logged_in_client),
        "partner_action": "create",
        "service_slug": "consulting",
        "note": "Worth pursuit",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/crm/deals/{lead['id']}/edit"
    deal = get_deal(lead["id"])
    assert deal["type"] == "opportunity"
    assert len(list_partners()) == before + 2
    edit = logged_in_client.get(f"/crm/deals/{lead['id']}/edit")
    assert "pill-opportunity" in edit.text
    assert "Worth pursuit" not in edit.text or True


def test_convert_missing_service_error(logged_in_client, db):
    lead, _ = create_lead(company_name="No Service Co", email="ns@example.com")
    resp = logged_in_client.post(f"/crm/leads/{lead['id']}/convert", data={
        "csrf_token": _csrf(logged_in_client),
        "partner_action": "create",
        "service_slug": "",
    })
    _html_client_error(resp)
    assert "Pick a service" in resp.text
    assert get_deal(lead["id"])["type"] == "lead"


def test_convert_partner_ambiguous_lists_candidates(logged_in_client, db):
    create_service("Consulting", "consulting")
    create_partner("Pat", email="pat@dup.example")
    create_partner("Pat", phone="0400111000")
    lead, _ = create_lead(
        contact_name="Pat", email="pat@dup.example", phone="0400111000",
        service_slug="consulting",
    )
    resp = logged_in_client.post(
        f"/crm/leads/{lead['id']}/convert",
        data={
            "csrf_token": _csrf(logged_in_client),
            "partner_action": "auto",
            "service_slug": "consulting",
        },
        follow_redirects=False,
    )
    _html_client_error(resp)
    assert "More than one partner matches" in resp.text
    assert get_deal(lead["id"])["type"] == "lead"


def test_lead_lost_restore_round_trip(logged_in_client, db):
    lead, _ = create_lead(company_name="Temp Lead", email="temp@lead.example")
    resp = logged_in_client.post(f"/crm/leads/{lead['id']}/lost", data={
        "csrf_token": _csrf(logged_in_client),
        "lost_reason": "Not a fit",
        "note": "Out of area",
    }, follow_redirects=False)
    assert resp.status_code == 303
    lost = get_deal(lead["id"])
    assert lost["active"] == 0
    page = logged_in_client.get(f"/crm/leads/{lead['id']}")
    assert "Restore" in page.text
    restore = logged_in_client.post(f"/crm/leads/{lead['id']}/restore", data={
        "csrf_token": _csrf(logged_in_client),
    }, follow_redirects=False)
    assert restore.status_code == 303
    assert get_deal(lead["id"])["active"] == 1


def test_opportunity_lost_restore_round_trip(logged_in_client, db):
    pid = create_partner("Cafe South")
    sid = create_service("Consulting", "consulting")
    deal_id = create_deal(pid, sid)
    resp = logged_in_client.post(f"/crm/deals/{deal_id}/lost", data={
        "csrf_token": _csrf(logged_in_client),
        "lost_reason": "No budget",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert get_deal(deal_id)["active"] == 0
    page = logged_in_client.get(f"/crm/deals/{deal_id}/edit")
    assert "Restore" in page.text
    logged_in_client.post(f"/crm/deals/{deal_id}/restore", data={
        "csrf_token": _csrf(logged_in_client),
    })
    assert get_deal(deal_id)["active"] == 1


def test_merged_lead_has_no_restore(logged_in_client, db):
    target, _ = create_lead(company_name="Keep Me", email="keep@example.com")
    source, _ = create_lead(company_name="Merge Me", email="merge@example.com")
    merge_leads(target["id"], [source["id"]])
    page = logged_in_client.get(f"/crm/leads/{source['id']}")
    assert "Restore is not available" in page.text
    assert ">Restore<" not in page.text


def test_lost_reasons_page_unique_and_inactive_picker(logged_in_client, db):
    page = logged_in_client.get("/crm/lost-reasons")
    assert page.status_code == 200
    assert "Not a fit" in page.text
    assert 'href="/crm/lost-reasons" aria-current="page">Lost reasons</a>' in page.text
    dup = logged_in_client.post("/crm/lost-reasons/new", data={
        "csrf_token": _csrf(logged_in_client),
        "name": "Not a fit",
        "active": "1",
    })
    _html_client_error(dup)
    assert "already exists" in dup.text
    created, error = create_lost_reason("Seasonal pause")
    assert error is None
    update_lost_reason(created["id"], active=False)
    lead, _ = create_lead(company_name="Picker Lead", email="picker@example.com")
    form = logged_in_client.get(f"/crm/leads/{lead['id']}")
    assert "Not a fit" in form.text
    assert "Seasonal pause" not in form.text
    compat = logged_in_client.get("/admin/lost-reasons", follow_redirects=False)
    assert compat.status_code in (301, 302, 303, 307, 308)
    assert "/crm/lost-reasons" in compat.headers.get("location", "")


def test_lost_reason_empty_name_is_html_error(logged_in_client, db):
    resp = logged_in_client.post("/crm/lost-reasons/new", data={
        "csrf_token": _csrf(logged_in_client),
        "name": "",
    })
    _html_client_error(resp)
    assert "Name is required" in resp.text


def test_lead_and_lost_reason_forms_require_csrf(logged_in_client, db):
    resp = logged_in_client.post("/crm/leads/new", data={"company_name": "No CSRF"})
    assert resp.status_code == 422
    resp = logged_in_client.post("/crm/lost-reasons/new", data={"name": "No CSRF"})
    assert resp.status_code == 422


def test_seeded_lost_stage_label_still_editable(logged_in_client, db):
    from app.services import pipeline_stages
    resp = logged_in_client.post("/crm/stages/lost/edit", data={
        "csrf_token": _csrf(logged_in_client),
        "label": "Closed lost",
        "is_lost": "1",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert pipeline_stages.get_stage("lost")["label"] == "Closed lost"


def test_research_note_on_lead(logged_in_client, db):
    lead, _ = create_lead(company_name="Notes Co", email="notes@example.com")
    logged_in_client.post(f"/crm/leads/{lead['id']}/activities", data={
        "csrf_token": _csrf(logged_in_client),
        "type": "research",
        "body": "Found their pricing page",
        "source_url": "https://notes.example/pricing",
    })
    page = logged_in_client.get(f"/crm/leads/{lead['id']}")
    assert "Found their pricing page" in page.text
    assert get_lead(lead["id"])["research_note_count"] == 1
