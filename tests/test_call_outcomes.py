from app.services.auth import generate_csrf_token
from app.services.partners import create_partner
from app.services.catalog import create_service
from app.services.deals import create_deal, get_deal, set_deal_stage, record_call_outcome
from app.services.activities import list_activities_for_partner


def _qualified_deal(db, phone="555-1234"):
    pid = create_partner("Jane Doe", email="jane@acme.example", phone=phone)
    sid = create_service("Consulting", "consulting")
    deal_id = create_deal(pid, sid, source="referral")
    set_deal_stage(deal_id, "contacted")
    set_deal_stage(deal_id, "qualified")
    return pid, sid, deal_id


def test_no_answer_logs_activity_and_keeps_stage(db):
    pid, _, deal_id = _qualified_deal(db)
    assert record_call_outcome(deal_id, "no_answer")
    assert get_deal(deal_id)["stage"] == "qualified"
    activities = list_activities_for_partner(pid)
    assert any(a["type"] == "call" and "No answer" in (a["body"] or "") for a in activities)


def test_not_interested_marks_deal_lost(db):
    pid, _, deal_id = _qualified_deal(db)
    assert record_call_outcome(deal_id, "not_interested")
    deal = get_deal(deal_id)
    assert deal["stage"] == "lost"
    assert deal["closed_at"] is not None


def test_interested_moves_to_nurture(db):
    _, _, deal_id = _qualified_deal(db)
    assert record_call_outcome(deal_id, "interested")
    assert get_deal(deal_id)["stage"] == "nurture"


def test_meeting_scheduled_moves_to_proposal(db):
    _, _, deal_id = _qualified_deal(db)
    assert record_call_outcome(deal_id, "meeting_scheduled")
    assert get_deal(deal_id)["stage"] == "proposal"


def test_meeting_scheduled_fails_loud_when_proposal_stage_deleted(db):
    from app.services import pipeline_stages

    pid, _, deal_id = _qualified_deal(db)
    ok, error = pipeline_stages.delete_stage("proposal")
    assert ok, error

    assert record_call_outcome(deal_id, "meeting_scheduled")
    assert get_deal(deal_id)["stage"] == "qualified"  # unchanged, not silently something else
    activities = list_activities_for_partner(pid)
    assert any("no longer exists" in (a["body"] or "") for a in activities)


def test_won_marks_deal_won(db):
    _, _, deal_id = _qualified_deal(db)
    assert record_call_outcome(deal_id, "won")
    deal = get_deal(deal_id)
    assert deal["stage"] == "won"
    assert deal["closed_at"] is not None


def test_note_is_appended_to_call_activity(db):
    pid, _, deal_id = _qualified_deal(db)
    record_call_outcome(deal_id, "interested", note="Wants a proposal by Friday")
    activities = list_activities_for_partner(pid)
    assert any("Wants a proposal by Friday" in (a["body"] or "") for a in activities)


def test_unknown_outcome_rejected(db):
    _, _, deal_id = _qualified_deal(db)
    assert record_call_outcome(deal_id, "bogus") is False
    assert get_deal(deal_id)["stage"] == "qualified"


def test_missing_deal_rejected(db):
    assert record_call_outcome(999, "no_answer") is False


def test_call_outcome_route_requires_login(client, db):
    resp = client.post("/deals/1/call-outcome", data={
        "outcome": "no_answer", "csrf_token": "x",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


def test_call_outcome_route_logs_and_redirects(logged_in_client, db):
    _, _, deal_id = _qualified_deal(db)
    resp = logged_in_client.post(
        f"/deals/{deal_id}/call-outcome",
        data={"outcome": "won", "note": "Signed", "csrf_token": generate_csrf_token()},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/calls"
    assert get_deal(deal_id)["stage"] == "won"


def test_call_outcome_route_bad_csrf_does_nothing(logged_in_client, db):
    _, _, deal_id = _qualified_deal(db)
    logged_in_client.post(
        f"/deals/{deal_id}/call-outcome",
        data={"outcome": "won", "csrf_token": "not-a-token"},
        follow_redirects=False,
    )
    assert get_deal(deal_id)["stage"] == "qualified"


def test_calls_page_renders_outcome_buttons(logged_in_client, db):
    _, _, deal_id = _qualified_deal(db)
    resp = logged_in_client.get("/calls")
    assert resp.status_code == 200
    assert f"/deals/{deal_id}/call-outcome" in resp.text
    assert "No answer" in resp.text
    assert "Not interested" in resp.text
