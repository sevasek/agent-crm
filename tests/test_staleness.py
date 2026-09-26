from datetime import date

from app.services.partners import create_partner
from app.services.catalog import create_service
from app.services.deals import create_deal, set_deal_stage
from app.services.activities import list_activities_for_deal, list_activities_for_partner
from app.services.staleness import (
    parse_action_date, deal_due_status, annotate_deal, list_due_deals,
    run_staleness_gate, due_activity_body, DUE, OVERDUE,
)


TODAY = date(2026, 8, 30)


def _setup(next_action_date, stage="new", next_action="Call them", name="Jane Doe"):
    pid = create_partner(name, phone="0400 111 222")
    sid = create_service("Consulting", f"consulting-{pid}")
    deal_id = create_deal(
        pid, sid, next_action=next_action, next_action_date=next_action_date,
    )
    if stage != "new":
        set_deal_stage(deal_id, stage)
    return pid, deal_id


def test_invalid_tz_falls_back_to_utc_and_warns(monkeypatch, caplog):
    import logging
    from app.services import staleness as staleness_mod

    monkeypatch.setenv("TZ", "Not/AZone")
    with caplog.at_level(logging.WARNING, logger="app.services.staleness"):
        today = staleness_mod.operator_today()
    assert today is not None
    assert "Invalid TZ" in caplog.text
    assert "Not/AZone" in caplog.text


def test_parse_action_date():
    assert parse_action_date("2026-08-30") == date(2026, 8, 30)
    assert parse_action_date("2026-08-30T09:00:00") == date(2026, 8, 30)
    assert parse_action_date("") is None
    assert parse_action_date(None) is None
    assert parse_action_date("not-a-date") is None


_CLOSED = {"won", "lost"}


def test_due_today_and_overdue_and_future():
    assert deal_due_status({"stage": "qualified", "next_action_date": "2026-08-30"}, today=TODAY, closed_stage_keys=_CLOSED) == DUE
    assert deal_due_status({"stage": "qualified", "next_action_date": "2026-08-20"}, today=TODAY, closed_stage_keys=_CLOSED) == OVERDUE
    assert deal_due_status({"stage": "qualified", "next_action_date": "2026-09-01"}, today=TODAY, closed_stage_keys=_CLOSED) is None
    assert deal_due_status({"stage": "qualified", "next_action_date": None}, today=TODAY, closed_stage_keys=_CLOSED) is None


def test_closed_deals_are_not_gated():
    assert deal_due_status({"stage": "won", "next_action_date": "2026-08-01"}, today=TODAY, closed_stage_keys=_CLOSED) is None
    assert deal_due_status({"stage": "lost", "next_action_date": "2026-08-01"}, today=TODAY, closed_stage_keys=_CLOSED) is None


def test_annotate_days_overdue():
    overdue = annotate_deal({"stage": "new", "next_action_date": "2026-08-20"}, today=TODAY, closed_stage_keys=_CLOSED)
    assert overdue["due_status"] == OVERDUE
    assert overdue["days_overdue"] == 10
    due = annotate_deal({"stage": "new", "next_action_date": "2026-08-30"}, today=TODAY, closed_stage_keys=_CLOSED)
    assert due["due_status"] == DUE
    assert due["days_overdue"] == 0


def test_list_due_excludes_closed_future_and_undated(db):
    _setup("2026-08-20", name="Overdue Person")
    _setup("2026-08-30", name="Due Today Person")
    _setup("2026-09-15", name="Future Person")
    _setup(None, name="Undated Person")
    pid, deal_id = _setup("2026-08-01", name="Won Person")
    set_deal_stage(deal_id, "won")

    due = list_due_deals(today=TODAY)
    names = [d["partner_name"] for d in due]
    assert names == ["Overdue Person", "Due Today Person"]
    assert due[0]["due_status"] == OVERDUE
    assert due[1]["due_status"] == DUE


def test_gate_logs_once_per_due_date(db):
    pid, deal_id = _setup("2026-08-20")
    first = run_staleness_gate(today=TODAY)
    assert first["logged_ids"] == [deal_id]
    bodies = [a["body"] for a in list_activities_for_deal(deal_id)]
    assert due_activity_body(date(2026, 8, 20), OVERDUE) in bodies

    second = run_staleness_gate(today=TODAY)
    assert second["logged_ids"] == []
    assert len([b for b in [a["body"] for a in list_activities_for_deal(deal_id)]
                if "Follow-up overdue" in (b or "")]) == 1


def test_gate_idempotent_when_prose_changes(db):
    """Dedup is the due-gate: marker, not the display sentence."""
    from app.services import staleness as staleness_mod
    from app.services.activities import log_activity

    pid, deal_id = _setup("2026-08-20")
    key = staleness_mod.due_gate_key(date(2026, 8, 20), OVERDUE)
    log_activity(pid, "system", f"{key}\nOld wording", deal_id=deal_id)
    result = run_staleness_gate(today=TODAY)
    assert result["logged_ids"] == []
    bodies = [a["body"] for a in list_activities_for_deal(deal_id)]
    assert sum(1 for b in bodies if (b or "").startswith(key)) == 1


def test_gate_logs_again_when_date_changes(db):
    from app.services.deals import update_deal_fields
    pid, deal_id = _setup("2026-08-20")
    run_staleness_gate(today=TODAY)
    update_deal_fields(deal_id, next_action_date="2026-08-25")
    result = run_staleness_gate(today=TODAY)
    assert result["logged_ids"] == [deal_id]
    bodies = [a["body"] for a in list_activities_for_deal(deal_id)]
    assert due_activity_body(date(2026, 8, 20), OVERDUE) in bodies
    assert due_activity_body(date(2026, 8, 25), OVERDUE) in bodies


def test_due_today_uses_due_wording(db):
    pid, deal_id = _setup("2026-08-30")
    run_staleness_gate(today=TODAY)
    bodies = [a["body"] for a in list_activities_for_partner(pid)]
    assert due_activity_body(TODAY, DUE) in bodies


def test_deals_page_marks_overdue(logged_in_client):
    _setup("2000-01-01", next_action="Call Jane")
    resp = logged_in_client.get("/deals")
    assert resp.status_code == 200
    assert "pill-overdue" in resp.text
    assert "Call Jane" in resp.text
    assert "2000-01-01" in resp.text


def test_due_filter_hides_future_deals(logged_in_client):
    _setup("2000-01-01", name="Overdue Person")
    _setup("2099-12-01", name="Future Person")
    resp = logged_in_client.get("/deals?due=1")
    assert resp.status_code == 200
    assert "Overdue Person" in resp.text
    assert "Future Person" not in resp.text


def test_due_filter_keeps_stage(logged_in_client):
    _setup("2000-01-01", stage="qualified", name="Qualified Due")
    _setup("2000-01-01", stage="contacted", name="Contacted Due")
    resp = logged_in_client.get("/deals?due=1&stage=qualified")
    assert resp.status_code == 200
    assert "Qualified Due" in resp.text
    assert "Contacted Due" not in resp.text


def test_partner_detail_shows_overdue_pill(logged_in_client):
    pid, _ = _setup("2000-01-01", name="Jane Doe")
    resp = logged_in_client.get(f"/partners/{pid}")
    assert resp.status_code == 200
    assert "pill-overdue" in resp.text
    assert 'action="/deals/' in resp.text
    assert "next_action_date" in resp.text


def test_partner_timeline_hides_due_gate_marker(logged_in_client):
    pid, _ = _setup("2000-01-01", name="Jane Doe")
    run_staleness_gate(today=TODAY)
    resp = logged_in_client.get(f"/partners/{pid}")
    assert resp.status_code == 200
    assert "due-gate:" not in resp.text
    assert "Follow-up overdue" in resp.text


def test_due_query_zero_is_not_a_filter(logged_in_client):
    _setup("2099-12-01", name="Future Person")
    resp = logged_in_client.get("/deals?due=0")
    assert resp.status_code == 200
    assert "Future Person" in resp.text


def test_gate_logs_due_then_overdue_as_separate_activities(db):
    pid, deal_id = _setup("2026-08-30")
    run_staleness_gate(today=TODAY)
    result = run_staleness_gate(today=date(2026, 8, 31))
    assert result["logged_ids"] == [deal_id]
    bodies = [a["body"] for a in list_activities_for_deal(deal_id)]
    assert due_activity_body(TODAY, DUE) in bodies
    assert due_activity_body(date(2026, 8, 30), OVERDUE) in bodies


def test_partner_detail_can_snooze_next_action(logged_in_client):
    import re
    pid, deal_id = _setup("2000-01-01", next_action="Call Jane")
    page = logged_in_client.get(f"/partners/{pid}")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    resp = logged_in_client.post(
        f"/deals/{deal_id}/next-action",
        data={
            "csrf_token": token,
            "next_action": "Call next week",
            "next_action_date": "2099-12-01",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "Call next week" in resp.text
    assert "2099-12-01" in resp.text
    assert "pill-overdue" not in resp.text


def test_date_picker_uses_yyyy_mm_dd_even_if_stored_iso_datetime(logged_in_client):
    pid, _ = _setup("2026-08-30T09:00:00")
    resp = logged_in_client.get(f"/partners/{pid}")
    assert resp.status_code == 200
    assert 'value="2026-08-30"' in resp.text
    assert "2026-08-30T09:00:00" not in resp.text


def test_next_run_at_is_today_before_eight_and_tomorrow_at_or_after():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from scripts.staleness_cron import next_run_at

    tz = ZoneInfo("Australia/Sydney")
    before = datetime(2026, 9, 8, 7, 59, tzinfo=tz)
    assert next_run_at(before) == datetime(2026, 9, 8, 8, 0, tzinfo=tz)
    at = datetime(2026, 9, 8, 8, 0, tzinfo=tz)
    assert next_run_at(at) == datetime(2026, 9, 9, 8, 0, tzinfo=tz)
    after = datetime(2026, 9, 8, 15, 30, tzinfo=tz)
    assert next_run_at(after) == datetime(2026, 9, 9, 8, 0, tzinfo=tz)


def test_cron_loop_keeps_running_when_the_gate_finds_due_deals(db, monkeypatch):
    """Exit 1 from the CLI is expected; the sidecar must not die on due deals."""
    from scripts import staleness_cron

    calls = {"n": 0}

    def fake_report():
        calls["n"] += 1
        return 1

    def fake_sleep(**kwargs):
        raise StopIteration("one cycle")

    monkeypatch.setattr(staleness_cron, "run_and_report", fake_report)
    monkeypatch.setattr(staleness_cron, "sleep_until_next_run", fake_sleep)
    try:
        staleness_cron.loop()
    except StopIteration:
        pass
    assert calls["n"] == 1


def test_cron_run_once_uses_thirty_second_busy_timeout(monkeypatch):
    from scripts import staleness_cron

    seen = []

    class FakeTimeout:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_timeout(seconds):
        seen.append(seconds)
        return FakeTimeout()

    monkeypatch.setattr(staleness_cron, "db_timeout", fake_timeout)
    monkeypatch.setattr(staleness_cron, "run_and_report", lambda: 0)
    assert staleness_cron.run_once() == 0
    assert seen == [30]


def test_cron_retries_transient_failures_before_waiting_until_tomorrow(monkeypatch):
    from scripts import staleness_cron

    calls = {"n": 0, "pauses": []}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("database is locked")
        return 0

    def record_pause(seconds):
        calls["pauses"].append(seconds)

    def fake_sleep(**kwargs):
        raise StopIteration("one cycle")

    monkeypatch.setattr(staleness_cron, "run_and_report", flaky)
    monkeypatch.setattr(staleness_cron, "retry_pause", record_pause)
    monkeypatch.setattr(staleness_cron, "sleep_until_next_run", fake_sleep)
    try:
        staleness_cron.loop()
    except StopIteration:
        pass
    assert calls["n"] == 3
    assert calls["pauses"] == [staleness_cron.RETRY_DELAY_SECONDS, staleness_cron.RETRY_DELAY_SECONDS]


def test_cron_gives_up_after_retries_without_dying(monkeypatch):
    from scripts import staleness_cron

    calls = {"n": 0, "pauses": 0}

    def always_fail():
        calls["n"] += 1
        raise RuntimeError("boom")

    def count_pause(seconds):
        calls["pauses"] += 1

    def fake_sleep(**kwargs):
        raise StopIteration("one cycle")

    monkeypatch.setattr(staleness_cron, "run_and_report", always_fail)
    monkeypatch.setattr(staleness_cron, "retry_pause", count_pause)
    monkeypatch.setattr(staleness_cron, "sleep_until_next_run", fake_sleep)
    try:
        staleness_cron.loop()
    except StopIteration:
        pass
    assert calls["n"] == staleness_cron.RETRY_ATTEMPTS
    assert calls["pauses"] == staleness_cron.RETRY_ATTEMPTS - 1


import pytest


@pytest.mark.parametrize("zone,offset_hours", [
    ("Australia/Sydney", (10, 11)),
    ("Australia/Melbourne", (10, 11)),
    ("Australia/Canberra", (10, 11)),
    ("Australia/Hobart", (10, 11)),
    ("Australia/Brisbane", (10, 10)),
    ("Australia/Adelaide", (9.5, 10.5)),
    ("Australia/Darwin", (9.5, 9.5)),
    ("Australia/Perth", (8, 8)),
])
def test_australian_timezones_set_operator_date(monkeypatch, zone, offset_hours):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app.services.staleness import operator_today

    monkeypatch.setenv("TZ", zone)
    expected = datetime.now(ZoneInfo(zone)).date()
    assert operator_today() == expected
    off = datetime.now(ZoneInfo(zone)).utcoffset().total_seconds() / 3600
    assert off in offset_hours
