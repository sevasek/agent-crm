from app.services.auth import generate_csrf_token
from app.services.catalog import create_service
from app.services.deals import create_deal, get_deal
from app.services.partners import create_partner


def _deal(db, stage="new"):
    pid = create_partner("Jane Doe", email="jane@acme.example")
    sid = create_service("Consulting", "consulting")
    return create_deal(pid, sid, stage=stage)


def test_htmx_script_is_vendored_and_linked(client):
    resp = client.get("/auth/login")
    assert resp.status_code == 200
    assert 'src="/static/vendor/htmx.min.js"' in resp.text
    js = client.get("/static/vendor/htmx.min.js")
    assert js.status_code == 200
    assert js.text.lstrip().startswith("var htmx")
    assert "<!DOCTYPE" not in js.text[:80]


def test_pipeline_stage_select_has_htmx_attrs(logged_in_client, db):
    _deal(db)
    resp = logged_in_client.get("/pipeline")
    assert resp.status_code == 200
    assert 'id="pipeline-col-new"' in resp.text
    assert 'hx-post="' in resp.text
    assert 'hx-swap="none"' in resp.text
    html = resp.text.lower()
    assert "<html" in html


def test_stage_change_without_hx_still_redirects(logged_in_client, db):
    deal_id = _deal(db)
    resp = logged_in_client.post(
        f"/deals/{deal_id}/stage",
        data={
            "stage": "contacted",
            "next": "pipeline",
            "csrf_token": generate_csrf_token(),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/pipeline"
    assert get_deal(deal_id)["stage"] == "contacted"


def test_stage_change_htmx_returns_column_fragments(logged_in_client, db):
    deal_id = _deal(db)
    resp = logged_in_client.post(
        f"/deals/{deal_id}/stage",
        data={
            "stage": "contacted",
            "next": "pipeline",
            "csrf_token": generate_csrf_token(),
        },
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    html = resp.text
    assert "<html" not in html.lower()
    assert 'id="pipeline-col-new"' in html
    assert 'id="pipeline-col-contacted"' in html
    assert "hx-swap-oob" in html
    assert "Jane Doe" in html
    assert get_deal(deal_id)["stage"] == "contacted"


def _qualified_callable_deal(db):
    from app.services.deals import set_deal_stage
    pid = create_partner("Jane Doe", email="jane@acme.example", phone="555-1234")
    sid = create_service("Consulting", "consulting")
    deal_id = create_deal(pid, sid, source="referral")
    set_deal_stage(deal_id, "contacted")
    set_deal_stage(deal_id, "qualified")
    return deal_id


def test_calls_page_wires_htmx_on_the_outcome_form(logged_in_client, db):
    _qualified_callable_deal(db)
    resp = logged_in_client.get("/calls")
    assert resp.status_code == 200
    assert 'id="calls-table"' in resp.text
    assert 'hx-target="#calls-table"' in resp.text
    assert 'hx-post="/deals/' in resp.text


def test_call_outcome_without_hx_still_redirects(logged_in_client, db):
    deal_id = _qualified_callable_deal(db)
    resp = logged_in_client.post(
        f"/deals/{deal_id}/call-outcome",
        data={
            "outcome": "no_answer",
            "csrf_token": generate_csrf_token(),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/calls"


def test_call_outcome_htmx_from_calls_returns_table(logged_in_client, db):
    deal_id = _qualified_callable_deal(db)
    resp = logged_in_client.post(
        f"/deals/{deal_id}/call-outcome",
        data={
            "outcome": "no_answer",
            "note": "Voicemail",
            "csrf_token": generate_csrf_token(),
        },
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    html = resp.text
    assert "<html" not in html.lower()
    assert 'id="calls-table"' in html
    assert "Jane Doe" in html
    assert get_deal(deal_id)["stage"] == "qualified"


def test_call_outcome_htmx_from_call_view_returns_fragment(logged_in_client, db):
    deal_id = _qualified_callable_deal(db)
    resp = logged_in_client.post(
        f"/deals/{deal_id}/call-outcome",
        data={
            "outcome": "won",
            "note": "Signed today",
            "next": "call_view",
            "csrf_token": generate_csrf_token(),
        },
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    html = resp.text
    assert "<html" not in html.lower()
    assert 'id="call-view"' in html
    assert "Signed today" in html
    assert get_deal(deal_id)["stage"] == "won"
