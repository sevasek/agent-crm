from app.services.auth import generate_csrf_token
from app.services.partners import create_partner
from app.services.catalog import create_service
from app.services.deals import create_deal, set_deal_stage
from app.services.activities import list_activities_for_partner, list_activities_for_deal
from app.services.phone import to_tel_href, has_callable_phone


def _qualified_deal(db, phone="0400 111 222"):
    pid = create_partner("Jane Doe", email="jane@acme.example", phone=phone)
    sid = create_service("Consulting", "consulting")
    deal_id = create_deal(pid, sid, source="referral", pain_points="No CRM", goals="Track leads")
    set_deal_stage(deal_id, "contacted")
    set_deal_stage(deal_id, "qualified")
    return pid, sid, deal_id


def test_to_tel_href_strips_punctuation():
    assert to_tel_href("0400 111 222") == "tel:0400111222"
    assert to_tel_href("+61 (400) 111-222") == "tel:+61400111222"


def test_to_tel_href_keeps_only_leading_plus():
    assert to_tel_href("0400+111+222") == "tel:0400111222"


def test_has_callable_phone():
    assert has_callable_phone("0400 111 222") is True
    assert has_callable_phone("---") is False
    assert has_callable_phone("(ext)") is False
    assert has_callable_phone("") is False
    assert has_callable_phone(None) is False


def test_deal_call_tel_logs(logged_in_client, db):
    pid, _, deal_id = _qualified_deal(db, phone="0400 111 222")
    resp = logged_in_client.post(
        f"/deals/{deal_id}/call-tel",
        data={"csrf_token": generate_csrf_token()},
    )
    assert resp.status_code == 204
    activities = list_activities_for_deal(deal_id)
    assert any(a["type"] == "call" and "tapped phone number" in (a["body"] or "") for a in activities)


def test_deal_call_tel_requires_login(client, db):
    resp = client.post("/deals/1/call-tel", data={"csrf_token": "x"}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


def test_deal_call_tel_bad_csrf_does_not_log(logged_in_client, db):
    pid, _, deal_id = _qualified_deal(db)
    resp = logged_in_client.post(
        f"/deals/{deal_id}/call-tel",
        data={"csrf_token": "not-a-token"},
    )
    assert resp.status_code == 204
    assert not any(a["type"] == "call" for a in list_activities_for_deal(deal_id))


def test_deal_call_tel_missing_phone_does_not_log(logged_in_client, db):
    pid = create_partner("No Phone", email="nophone@example.com")
    sid = create_service("Consulting", "consulting")
    deal_id = create_deal(pid, sid)
    resp = logged_in_client.post(
        f"/deals/{deal_id}/call-tel",
        data={"csrf_token": generate_csrf_token()},
    )
    assert resp.status_code == 204
    assert not any(a["type"] == "call" for a in list_activities_for_deal(deal_id))


def test_deal_call_tel_unparseable_phone_does_not_log(logged_in_client, db):
    pid, _, deal_id = _qualified_deal(db, phone="---")
    resp = logged_in_client.post(
        f"/deals/{deal_id}/call-tel",
        data={"csrf_token": generate_csrf_token()},
    )
    assert resp.status_code == 204
    assert not any(a["type"] == "call" for a in list_activities_for_deal(deal_id))


def test_partner_call_tel_logs_without_deal(logged_in_client, db):
    pid = create_partner("Jane Doe", email="jane@x.example", phone="0400 111 222")
    resp = logged_in_client.post(
        f"/partners/{pid}/call-tel",
        data={"csrf_token": generate_csrf_token()},
    )
    assert resp.status_code == 204
    activities = list_activities_for_partner(pid)
    assert any(a["type"] == "call" and a["deal_id"] is None for a in activities)


def test_partner_call_tel_requires_login(client, db):
    resp = client.post("/partners/1/call-tel", data={"csrf_token": "x"}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


def test_call_view_shows_talk_track_and_dial_link(logged_in_client, db):
    pid, _, deal_id = _qualified_deal(db)
    resp = logged_in_client.get(f"/deals/{deal_id}/call")
    assert resp.status_code == 200
    assert "Jane Doe" in resp.text
    assert "No CRM" in resp.text
    assert "Track leads" in resp.text
    assert 'href="tel:0400111222"' in resp.text
    assert f"logCallTap('/deals/{deal_id}/call-tel'" in resp.text
    assert f"/deals/{deal_id}/call-outcome" in resp.text
    assert "Score" in resp.text  # qualified-pool deal, so score section renders
    assert "unknown when qualified" not in resp.text
    assert "referral/inbound" not in resp.text


def test_call_view_score_uses_actual_qualified_at_recency(logged_in_client, db):
    # Regression: the call view's score must reflect when this deal actually
    # entered its qualified-pool stage, not silently score recency as
    # "unknown" because qualified_at was never populated on the row.
    pid, _, deal_id = _qualified_deal(db)
    resp = logged_in_client.get(f"/deals/{deal_id}/call")
    assert "unknown when qualified" not in resp.text


def test_call_view_hides_score_for_non_qualified_pool_stage(logged_in_client, db):
    pid = create_partner("Jane Doe", email="jane@x.example", phone="0400 111 222")
    sid = create_service("Consulting", "consulting")
    deal_id = create_deal(pid, sid)  # stays in "new"
    resp = logged_in_client.get(f"/deals/{deal_id}/call")
    assert resp.status_code == 200
    assert "Score" not in resp.text


def test_call_view_no_phone_shows_message_not_dial_button(logged_in_client, db):
    pid = create_partner("No Phone", email="nophone@example.com")
    sid = create_service("Consulting", "consulting")
    deal_id = create_deal(pid, sid)
    resp = logged_in_client.get(f"/deals/{deal_id}/call")
    assert resp.status_code == 200
    assert "No phone number on file" in resp.text
    assert "btn-tel-big" not in resp.text


def test_call_view_undialable_phone_shows_message_not_dial_button(logged_in_client, db):
    pid = create_partner("Punctuation Phone", email="punct@example.com", phone="---")
    sid = create_service("Consulting", "consulting")
    deal_id = create_deal(pid, sid)
    resp = logged_in_client.get(f"/deals/{deal_id}/call")
    assert resp.status_code == 200
    assert "No phone number on file" in resp.text
    assert "btn-tel-big" not in resp.text


def test_call_view_requires_login(client, db):
    resp = client.get("/deals/1/call", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


def test_call_view_missing_deal_redirects(logged_in_client, db):
    resp = logged_in_client.get("/deals/999/call", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/calls"


def test_call_outcome_from_call_view_redirects_back_to_call_view(logged_in_client, db):
    pid, _, deal_id = _qualified_deal(db)
    resp = logged_in_client.post(
        f"/deals/{deal_id}/call-outcome",
        data={"outcome": "no_answer", "next": "call_view", "csrf_token": generate_csrf_token()},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/deals/{deal_id}/call"


def test_partner_detail_phone_uses_tel_link(logged_in_client, db):
    pid = create_partner("Jane Doe", email="jane@x.example", phone="0400 111 222")
    resp = logged_in_client.get(f"/partners/{pid}")
    assert 'href="tel:0400111222"' in resp.text
    assert f"logCallTap('/partners/{pid}/call-tel'" in resp.text
