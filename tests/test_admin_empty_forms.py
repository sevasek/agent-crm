"""Starlette 1.x treats an empty HTML control (name=) as a missing required
Form(...) field and returns JSON 422. User-facing required text/select fields
use Form("") so the handler's HTML 4xx still runs. CSRF stays Form(...).
"""

from app.services.auth import generate_csrf_token
from app.services.catalog import create_service, get_service, get_service_by_slug
from app.services.partners import create_partner, get_partner, list_partners
from app.services.deals import create_deal, get_deal, list_deals
from app.services import icp, offers, pipeline_stages


def _html_client_error(resp, status=400):
    assert resp.status_code == status
    ctype = resp.headers.get("content-type", "")
    assert "text/html" in ctype
    assert "application/json" not in ctype


def test_new_partner_empty_name_is_html_error_not_422(logged_in_client, db):
    before = {p["id"] for p in list_partners()}
    resp = logged_in_client.post("/partners/new", data={
        "csrf_token": generate_csrf_token(logged_in_client.cookies.get("session")),
        "name": "",
    })
    _html_client_error(resp)
    assert "New partner" in resp.text
    assert "Name is required" in resp.text
    assert {p["id"] for p in list_partners()} == before


def test_edit_partner_empty_name_is_html_error_not_422(logged_in_client, db):
    pid = create_partner("Jane Doe", email="jane@acme.example")
    resp = logged_in_client.post(f"/partners/{pid}/edit", data={
        "csrf_token": generate_csrf_token(logged_in_client.cookies.get("session")),
        "name": "",
    })
    _html_client_error(resp)
    assert "Name is required" in resp.text
    assert get_partner(pid)["name"] == "Jane Doe"


def test_partner_missing_csrf_still_422(logged_in_client, db):
    resp = logged_in_client.post("/partners/new", data={"name": "Acme"})
    assert resp.status_code == 422
    assert "application/json" in resp.headers.get("content-type", "")


def test_new_service_empty_name_is_html_error_not_422(logged_in_client, db):
    resp = logged_in_client.post("/services/new", data={
        "csrf_token": generate_csrf_token(logged_in_client.cookies.get("session")),
        "name": "",
        "slug": "consulting",
        "description": "",
    })
    _html_client_error(resp)
    assert "Name is required" in resp.text
    assert get_service_by_slug("consulting") is None


def test_new_service_empty_slug_is_html_error_not_422(logged_in_client, db):
    resp = logged_in_client.post("/services/new", data={
        "csrf_token": generate_csrf_token(logged_in_client.cookies.get("session")),
        "name": "Consulting",
        "slug": "",
        "description": "",
    })
    _html_client_error(resp)
    assert "slug" in resp.text.lower()
    assert get_service_by_slug("consulting") is None


def test_edit_service_empty_name_is_html_error_not_422(logged_in_client, db):
    sid = create_service("Consulting", "consulting")
    resp = logged_in_client.post(f"/services/{sid}/edit", data={
        "csrf_token": generate_csrf_token(logged_in_client.cookies.get("session")),
        "name": "",
        "description": "",
        "active": "1",
    })
    _html_client_error(resp)
    assert "Name is required" in resp.text
    assert get_service(sid)["name"] == "Consulting"


def test_create_stage_empty_key_is_html_error_not_422(logged_in_client, db):
    resp = logged_in_client.post("/stages/new", data={
        "csrf_token": generate_csrf_token(logged_in_client.cookies.get("session")),
        "key": "",
        "label": "Demo scheduled",
    })
    _html_client_error(resp)
    assert "Key must be" in resp.text


def test_create_stage_empty_label_is_html_error_not_422(logged_in_client, db):
    resp = logged_in_client.post("/stages/new", data={
        "csrf_token": generate_csrf_token(logged_in_client.cookies.get("session")),
        "key": "demo-scheduled",
        "label": "",
    })
    _html_client_error(resp)
    assert "Label is required" in resp.text
    assert pipeline_stages.get_stage("demo-scheduled") is None


def test_edit_stage_empty_label_is_html_error_not_422(logged_in_client, db):
    resp = logged_in_client.post("/stages/proposal/edit", data={
        "csrf_token": generate_csrf_token(logged_in_client.cookies.get("session")),
        "label": "",
    })
    _html_client_error(resp)
    assert "Label is required" in resp.text
    assert pipeline_stages.get_stage("proposal")["label"] == "Proposal"


def test_create_icp_empty_field_is_html_error_not_422(logged_in_client, db):
    resp = logged_in_client.post("/icp/new", data={
        "csrf_token": generate_csrf_token(logged_in_client.cookies.get("session")),
        "label": "",
        "field": "",
        "operator": "gt",
        "value": "10",
        "weight": "5",
    })
    _html_client_error(resp)
    assert "Unknown field" in resp.text
    assert icp.list_criteria() == []


def test_create_icp_empty_operator_is_html_error_not_422(logged_in_client, db):
    resp = logged_in_client.post("/icp/new", data={
        "csrf_token": generate_csrf_token(logged_in_client.cookies.get("session")),
        "label": "",
        "field": "team_size",
        "operator": "",
        "value": "10",
        "weight": "5",
    })
    _html_client_error(resp)
    assert "Unknown operator" in resp.text
    assert icp.list_criteria() == []


def test_new_deal_empty_partner_and_service_is_html_error_not_422(logged_in_client, db):
    pid = create_partner("Jane Doe", email="jane@acme.example")
    sid = create_service("Consulting", "consulting")
    resp = logged_in_client.post("/deals/new", data={
        "csrf_token": generate_csrf_token(logged_in_client.cookies.get("session")),
        "partner_id": "",
        "service_id": "",
        "source": "",
        "value_estimate": "",
        "pain_points": "",
        "goals": "",
        "next_action": "",
        "next_action_date": "",
    })
    _html_client_error(resp)
    assert "Pick a partner and a service" in resp.text
    assert list_deals() == []

    resp = logged_in_client.post("/deals/new", data={
        "csrf_token": generate_csrf_token(logged_in_client.cookies.get("session")),
        "partner_id": str(pid),
        "service_id": "",
        "source": "",
        "value_estimate": "",
        "pain_points": "",
        "goals": "",
        "next_action": "",
        "next_action_date": "",
    })
    _html_client_error(resp)
    assert list_deals() == []
    assert get_partner(pid)["id"] == pid
    assert get_service(sid)["id"] == sid


def test_call_outcome_empty_does_not_422(logged_in_client, db):
    from app.services.deals import set_deal_stage

    pid = create_partner("Jane Doe", email="jane@acme.example", phone="0400 111 222")
    sid = create_service("Consulting", "consulting")
    deal_id = create_deal(pid, sid)
    set_deal_stage(deal_id, "contacted")
    set_deal_stage(deal_id, "qualified")
    resp = logged_in_client.post(
        f"/deals/{deal_id}/call-outcome",
        data={"outcome": "", "csrf_token": generate_csrf_token(logged_in_client.cookies.get("session"))},
        follow_redirects=False,
    )
    assert resp.status_code != 422
    assert "application/json" not in resp.headers.get("content-type", "")
    assert get_deal(deal_id)["stage"] == "qualified"


def test_call_view_empty_offer_name_does_not_422(logged_in_client, db):
    pid = create_partner("Jane Doe", email="jane@acme.example", phone="0400 111 222")
    sid = create_service("Consulting", "consulting")
    deal_id = create_deal(pid, sid)
    resp = logged_in_client.post(
        f"/deals/{deal_id}/offer/new",
        data={"name": "", "csrf_token": generate_csrf_token(logged_in_client.cookies.get("session"))},
        follow_redirects=False,
    )
    assert resp.status_code != 422
    assert "application/json" not in resp.headers.get("content-type", "")
    assert offers.list_offers() == []
    assert get_deal(deal_id)["offer_id"] is None
