from app.services.partners import create_partner
from app.services.catalog import create_service
from app.services.deals import (
    create_deal, get_deal, list_parent_candidates, set_deal_stage,
    update_deal_fields, validate_parent_link,
)
from app.services.activities import list_activities_for_partner


def _setup(db, nurture_list_slug=None, email="jane@acme.example"):
    pid = create_partner("Jane Doe", email=email)
    sid = create_service("Consulting", "consulting", nurture_list_slug=nurture_list_slug)
    deal_id = create_deal(pid, sid, source="referral")
    return pid, sid, deal_id


def test_create_deal_defaults_to_new_stage(db):
    _, _, deal_id = _setup(db)
    assert get_deal(deal_id)["stage"] == "new"


def test_deal_pain_points_and_goals(db):
    pid = create_partner("Jane Doe", email="jane@acme.example")
    sid = create_service("Consulting", "consulting")
    deal_id = create_deal(
        pid, sid,
        pain_points="No visibility on business processes, systems described as scattergun",
        goals="A clear view of what to automate first",
    )
    deal = get_deal(deal_id)
    assert deal["pain_points"] == "No visibility on business processes, systems described as scattergun"
    assert deal["goals"] == "A clear view of what to automate first"


def test_update_deal_pain_points_and_goals(db):
    _, _, deal_id = _setup(db)
    assert update_deal_fields(deal_id, pain_points="Updated pain point", goals="Updated goal")
    deal = get_deal(deal_id)
    assert deal["pain_points"] == "Updated pain point"
    assert deal["goals"] == "Updated goal"


def test_stage_transition_logs_activity(db):
    pid, _, deal_id = _setup(db)
    assert set_deal_stage(deal_id, "contacted")
    assert get_deal(deal_id)["stage"] == "contacted"

    activities = list_activities_for_partner(pid)
    assert any("new -> contacted" in (a["body"] or "") for a in activities)


def test_won_and_lost_set_closed_at(db):
    _, _, deal_id = _setup(db)
    set_deal_stage(deal_id, "won")
    assert get_deal(deal_id)["closed_at"] is not None


def test_invalid_stage_rejected(db):
    _, _, deal_id = _setup(db)
    assert set_deal_stage(deal_id, "not-a-real-stage") is False
    assert get_deal(deal_id)["stage"] == "new"


def test_transition_into_nurture_triggers_enrollment(db, monkeypatch):
    calls = []

    def fake_enroll(partner, service, deal_id=None):
        calls.append((partner["email"], service["nurture_list_slug"]))
        return True, "enrolled on 'automation-interest' (confirmed)"

    monkeypatch.setattr("app.services.deals.enroll_partner_in_nurture", fake_enroll)

    pid, sid, deal_id = _setup(db, nurture_list_slug="automation-interest")
    set_deal_stage(deal_id, "nurture")

    assert calls == [("jane@acme.example", "automation-interest")]
    activities = list_activities_for_partner(pid)
    assert any("Nurture enrollment succeeded" in (a["body"] or "") for a in activities)


def test_non_nurture_transition_does_not_call_enrollment(db, monkeypatch):
    called = []
    monkeypatch.setattr("app.services.deals.enroll_partner_in_nurture", lambda p, s, deal_id=None: called.append(1))

    _, _, deal_id = _setup(db, nurture_list_slug="automation-interest")
    set_deal_stage(deal_id, "contacted")

    assert called == []


def test_parent_link_same_partner_and_rejects_mismatch_and_cycle(db):
    pid = create_partner("Jane Doe", email="jane@acme.example")
    other = create_partner("Other Co", email="other@acme.example")
    sid = create_service("Consulting", "consulting")
    support = create_service("Support", "support")
    parent_id = create_deal(pid, sid)
    child_id = create_deal(pid, support)
    stranger_id = create_deal(other, sid)

    cleaned, error = validate_parent_link(child_id, parent_id, pid)
    assert error is None
    assert cleaned == parent_id

    _cleaned, error = validate_parent_link(child_id, stranger_id, pid)
    assert error == "parent_partner_mismatch"

    assert update_deal_fields(child_id, parent_deal_id=parent_id)
    _cleaned, error = validate_parent_link(parent_id, child_id, pid)
    assert error == "parent_cycle"

    candidates = list_parent_candidates(pid, exclude_deal_id=child_id)
    assert [c["id"] for c in candidates] == [parent_id]
    assert list_parent_candidates(pid, exclude_deal_id=parent_id) == []


def test_enrollment_failure_is_logged_not_raised(db, monkeypatch):
    monkeypatch.setattr(
        "app.services.deals.enroll_partner_in_nurture",
        lambda p, s, deal_id=None: (False, "webhook unreachable"),
    )

    pid, sid, deal_id = _setup(db, nurture_list_slug="automation-interest")
    assert set_deal_stage(deal_id, "nurture") is True  # stage change itself still succeeds

    activities = list_activities_for_partner(pid)
    assert any("Nurture enrollment failed: webhook unreachable" in (a["body"] or "") for a in activities)
