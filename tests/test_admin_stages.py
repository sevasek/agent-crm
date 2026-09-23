from app.services.auth import generate_csrf_token
from app.services.partners import create_partner
from app.services.catalog import create_service
from app.services.deals import create_deal
from app.services import pipeline_stages


def test_pipeline_board_requires_login(client, db):
    resp = client.get("/pipeline", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


def test_pipeline_board_shows_columns_and_cards(logged_in_client, db):
    pid = create_partner("Jane Doe", email="jane@x.example")
    sid = create_service("Consulting", "consulting")
    create_deal(pid, sid)
    resp = logged_in_client.get("/pipeline")
    assert resp.status_code == 200
    assert "New" in resp.text
    assert "Jane Doe" in resp.text
    assert 'class="container container-wide"' in resp.text


def test_stages_settings_page_lists_stages(logged_in_client, db):
    resp = logged_in_client.get("/stages")
    assert resp.status_code == 200
    for label in ["New", "Contacted", "Qualified", "Nurture", "Proposal", "Won", "Lost"]:
        assert label in resp.text
    assert 'class="container container-wide"' not in resp.text


def test_create_stage_via_admin_form(logged_in_client, db):
    resp = logged_in_client.post("/stages/new", data={
        "key": "demo-scheduled", "label": "Demo scheduled",
        "csrf_token": generate_csrf_token(),
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert pipeline_stages.get_stage("demo-scheduled") is not None


def test_create_stage_duplicate_shows_error(logged_in_client, db):
    resp = logged_in_client.post("/stages/new", data={
        "key": "new", "label": "Duplicate", "csrf_token": generate_csrf_token(),
    })
    assert resp.status_code == 400
    assert "already exists" in resp.text


def test_edit_stage_label_and_roles(logged_in_client, db):
    resp = logged_in_client.post("/stages/proposal/edit", data={
        "label": "Demo booked", "is_qualified_pool": "on",
        "csrf_token": generate_csrf_token(),
    }, follow_redirects=False)
    assert resp.status_code == 303
    stage = pipeline_stages.get_stage("proposal")
    assert stage["label"] == "Demo booked"
    assert stage["is_qualified_pool"] == 1


def test_edit_stage_unchecking_role_clears_it(logged_in_client, db):
    logged_in_client.post("/stages/qualified/edit", data={
        "label": "Qualified", "csrf_token": generate_csrf_token(),
        # is_qualified_pool omitted == unchecked
    })
    assert pipeline_stages.get_stage("qualified")["is_qualified_pool"] == 0


def test_move_stage_up(logged_in_client, db):
    resp = logged_in_client.post("/stages/contacted/move", data={
        "direction": "up", "csrf_token": generate_csrf_token(),
    }, follow_redirects=False)
    assert resp.status_code == 303
    keys = [s["key"] for s in pipeline_stages.list_stages()]
    assert keys[0] == "contacted"


def test_delete_stage_in_use_shows_error(logged_in_client, db):
    pid = create_partner("Jane Doe", email="jane@x.example")
    sid = create_service("Consulting", "consulting")
    create_deal(pid, sid)
    resp = logged_in_client.post("/stages/new/delete", data={
        "csrf_token": generate_csrf_token(),
    })
    assert resp.status_code == 400
    assert "delete a stage that deals are currently in" in resp.text
    assert pipeline_stages.get_stage("new") is not None


def test_delete_unused_stage(logged_in_client, db):
    resp = logged_in_client.post("/stages/proposal/delete", data={
        "csrf_token": generate_csrf_token(),
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert pipeline_stages.get_stage("proposal") is None


def test_deals_page_tabs_use_dynamic_stage_labels(logged_in_client, db):
    pipeline_stages.update_stage("qualified", label="Sales Qualified")
    resp = logged_in_client.get("/deals")
    assert "Sales Qualified" in resp.text


def test_partner_detail_stage_dropdown_uses_dynamic_stages(logged_in_client, db):
    pipeline_stages.update_stage("nurture", label="Drip campaign")
    pid = create_partner("Jane Doe", email="jane@x.example")
    sid = create_service("Consulting", "consulting")
    create_deal(pid, sid)
    resp = logged_in_client.get(f"/partners/{pid}")
    assert "Drip campaign" in resp.text
