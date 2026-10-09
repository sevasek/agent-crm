"""Admin pages for leads, conversion, loss, and lost reasons."""
from app.services.auth import generate_csrf_token
from app.services.catalog import create_service
from app.services.deals import get_deal
from app.services.lead_records import create_lead, mark_lost
from app.services.partners import create_partner


def _token(client):
    return generate_csrf_token(client.cookies.get("session"))


def test_leads_list_and_form_round_trip(logged_in_client, db):
    resp = logged_in_client.get("/leads")
    assert resp.status_code == 200
    assert "Leads" in resp.text
    created = logged_in_client.post("/leads/new", data={
        "company_name": "Harbour Physio",
        "website": "https://harbour.example",
        "csrf_token": _token(logged_in_client),
    }, follow_redirects=False)
    assert created.status_code == 303
    page = logged_in_client.get(created.headers["location"])
    assert page.status_code == 200
    assert "Harbour Physio" in page.text
    assert "Convert to an opportunity" in page.text


def test_convert_and_lost_round_trip(logged_in_client, db):
    create_service("Consulting", "consulting")
    lead = create_lead(company_name="Convert Co", email="convert@x.example", log_create=False)["lead"]
    converted = logged_in_client.post(f"/leads/{lead['id']}/convert", data={
        "service_slug": "consulting",
        "partner_action": "create",
        "note": "Ready",
        "csrf_token": _token(logged_in_client),
    }, follow_redirects=False)
    assert converted.status_code == 303
    assert get_deal(lead["id"])["type"] == "opportunity"

    other = create_lead(company_name="Lose Co", email="lose@x.example", log_create=False)["lead"]
    lost = logged_in_client.post(f"/leads/{other['id']}/lost", data={
        "lost_reason": "Not a fit",
        "note": "no",
        "csrf_token": _token(logged_in_client),
    }, follow_redirects=False)
    assert lost.status_code == 303
    assert get_deal(other["id"])["active"] == 0
    restored = logged_in_client.post(f"/deals/{other['id']}/restore", data={
        "csrf_token": _token(logged_in_client),
    }, follow_redirects=False)
    assert restored.status_code == 303
    assert get_deal(other["id"])["active"] == 1


def test_lost_reasons_page_rejects_duplicate_name(logged_in_client, db):
    page = logged_in_client.get("/lost-reasons")
    assert page.status_code == 200
    assert "Not a fit" in page.text
    dup = logged_in_client.post("/lost-reasons", data={
        "name": "Not a fit",
        "csrf_token": _token(logged_in_client),
    })
    assert dup.status_code == 400
    assert "already" in dup.text
    redirected = logged_in_client.get("/admin/lost-reasons", follow_redirects=True)
    assert redirected.status_code == 200
    assert "Lost reasons" in redirected.text


def test_pipeline_hides_leads_and_lost_until_filtered(logged_in_client, db):
    pid = create_partner("Ada Board", email="ada-board@x.example")
    sid = create_service("Consulting", "consulting-board")
    from app.services.deals import create_deal, set_deal_stage
    deal_id = create_deal(pid, sid)
    set_deal_stage(deal_id, "lost")
    lead = create_lead(company_name="Hidden Lead", email="hidden@x.example", log_create=False)["lead"]
    board = logged_in_client.get("/pipeline")
    assert "Hidden Lead" not in board.text
    assert "Ada Board" not in board.text
    shown = logged_in_client.get("/pipeline?lost=1")
    assert "Ada Board" in shown.text
    assert "Hidden Lead" not in shown.text
    assert 'hx-post="/deals/' in shown.text
    mark_lost(lead["id"], lost_reason="Not a fit")
    assert "Restore" in logged_in_client.get(f"/leads/{lead['id']}").text
