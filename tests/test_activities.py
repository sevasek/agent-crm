from app.services.partners import create_partner
from app.services.activities import log_activity, list_activities_for_partner, list_activities_for_deal


def test_log_and_list_activity(db):
    pid = create_partner("Jane Doe")
    log_activity(pid, "call", "Left a voicemail")
    log_activity(pid, "note", "Phone off, no contact")

    activities = list_activities_for_partner(pid)
    assert len(activities) == 2
    assert activities[0]["body"] == "Phone off, no contact"  # most recent first


def test_invalid_type_falls_back_to_note(db):
    pid = create_partner("Jane Doe")
    log_activity(pid, "not-a-real-type", "something")
    assert list_activities_for_partner(pid)[0]["type"] == "note"


def test_deal_scoped_activity(db):
    pid = create_partner("Jane Doe")
    log_activity(pid, "note", "unrelated to any deal")
    log_activity(pid, "note", "about deal 1", deal_id=1)

    assert len(list_activities_for_deal(1)) == 1
    assert len(list_activities_for_partner(pid)) == 2
