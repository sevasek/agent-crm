from app.services.partners import create_partner
from app.services.catalog import create_service
from app.services.deals import create_deal, get_deal, set_deal_stage, update_deal_fields
from app.services.activities import list_activities_for_partner
from app.services import pipeline_stages
from app.services.offers import create_offer


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


def test_enrollment_failure_is_logged_not_raised(db, monkeypatch):
    monkeypatch.setattr(
        "app.services.deals.enroll_partner_in_nurture",
        lambda p, s, deal_id=None: (False, "webhook unreachable"),
    )

    pid, sid, deal_id = _setup(db, nurture_list_slug="automation-interest")
    assert set_deal_stage(deal_id, "nurture") is True  # stage change itself still succeeds

    activities = list_activities_for_partner(pid)
    assert any("Nurture enrollment failed: webhook unreachable" in (a["body"] or "") for a in activities)


def test_transition_into_won_triggers_webhook(db, monkeypatch):
    calls = []

    def fake_notify(partner, service, deal, stage, offer=None):
        calls.append({
            "email": partner["email"],
            "service": service["slug"],
            "deal_id": deal["id"],
            "stage": stage,
            "offer": offer,
            "value_estimate": deal.get("value_estimate"),
        })
        return True, "sent to deal-won webhook for deal 1"

    monkeypatch.setattr("app.services.deals.notify_deal_won", fake_notify)

    pid, sid, deal_id = _setup(db)
    offer, _ = create_offer("Discovery Session", service_id=sid, price=350, currency="AUD")
    update_deal_fields(deal_id, offer_id=offer["id"], value_estimate=1500)
    set_deal_stage(deal_id, "won")

    assert len(calls) == 1
    assert calls[0]["email"] == "jane@acme.example"
    assert calls[0]["service"] == "consulting"
    assert calls[0]["deal_id"] == deal_id
    assert calls[0]["stage"] == "won"
    assert calls[0]["offer"]["name"] == "Discovery Session"
    assert calls[0]["value_estimate"] == 1500
    activities = list_activities_for_partner(pid)
    assert any("Deal-won webhook succeeded" in (a["body"] or "") for a in activities)


def test_lost_nurture_and_already_won_do_not_call_won_webhook(db, monkeypatch):
    called = []
    monkeypatch.setattr(
        "app.services.deals.notify_deal_won",
        lambda *a, **k: called.append(1) or (True, "sent"),
    )

    _, _, lost_id = _setup(db)
    set_deal_stage(lost_id, "lost")
    assert called == []

    _, _, nurture_id = _setup(db, nurture_list_slug="automation-interest")
    monkeypatch.setattr(
        "app.services.deals.enroll_partner_in_nurture",
        lambda *a, **k: (True, "enrolled"),
    )
    set_deal_stage(nurture_id, "nurture")
    assert called == []

    _, _, won_id = _setup(db)
    set_deal_stage(won_id, "won")
    assert called == [1]
    set_deal_stage(won_id, "won")
    assert called == [1]

    pipeline_stages.create_stage("closed-won", "Closed won", is_won=True)
    set_deal_stage(won_id, "closed-won")
    assert get_deal(won_id)["stage"] == "closed-won"
    assert called == [1]


def test_won_webhook_failure_is_logged_not_raised(db, monkeypatch):
    monkeypatch.setattr(
        "app.services.deals.notify_deal_won",
        lambda *a, **k: (False, "webhook unreachable"),
    )

    pid, _, deal_id = _setup(db)
    assert set_deal_stage(deal_id, "won") is True
    assert get_deal(deal_id)["stage"] == "won"

    activities = list_activities_for_partner(pid)
    assert any("Deal-won webhook failed: webhook unreachable" in (a["body"] or "") for a in activities)
