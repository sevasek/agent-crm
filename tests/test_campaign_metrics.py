"""Unit tests for straight-line campaign metrics. No database."""

import ast
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.services.campaign_metrics import (
    AHEAD,
    BEHIND,
    DAYS_PER_MONTH,
    DAYS_PER_WEEK,
    DAYS_PER_YEAR,
    DEADLINE_PASSED,
    NO_CLOSED_DEALS,
    NO_DEADLINE,
    NO_ELAPSED_DAYS,
    NO_LEADS,
    NO_START_DATE,
    ON_TRACK,
    START_IN_FUTURE,
    TARGET_IS_ZERO,
    ZERO_LENGTH_WINDOW,
    compute_campaign_metrics,
)

TODAY = "2026-09-16"
START = "2026-09-01"
DEADLINE = "2026-09-30"


def compute(campaign=None, deals=None, events=None, today=TODAY, tz="UTC", period=None):
    return compute_campaign_metrics(
        {} if campaign is None else campaign,
        [] if deals is None else deals,
        [] if events is None else events,
        today,
        tz,
        period,
    )


def deal(deal_id, outcome, **extra):
    row = {"id": deal_id, "outcome": outcome}
    row.update(extra)
    return row


def _walk_metrics(obj):
    if isinstance(obj, dict) and "formula" in obj:
        assert {"value", "formula", "inputs", "note"} <= set(obj)
        assert isinstance(obj["formula"], str) and obj["formula"]
        assert isinstance(obj["inputs"], dict)
        assert obj["note"] is None or isinstance(obj["note"], str)
        _json_safe(obj["inputs"])
        _json_safe(obj["value"])
        return
    if isinstance(obj, dict):
        for value in obj.values():
            _walk_metrics(value)
    elif isinstance(obj, list):
        for value in obj:
            _walk_metrics(value)


def _json_safe(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float):
            assert value == value and value not in (float("inf"), float("-inf"))
        return
    if isinstance(value, list):
        for item in value:
            _json_safe(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            assert isinstance(key, str)
            _json_safe(item)
        return
    raise AssertionError(f"not JSON-safe: {value!r}")


def _sep_deals(n, outcome="open", **extra):
    return [
        deal(i + 1, outcome, created_at=f"2026-09-{(i % 28) + 1:02d}", **extra)
        for i in range(n)
    ]


# --- contract shape -------------------------------------------------------

def test_module_imports_stdlib_only():
    tree = ast.parse(Path("app/services/campaign_metrics.py").read_text())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    assert modules <= {"__future__", "collections", "dataclasses", "datetime", "math", "zoneinfo", "re"}
    assert "app" not in modules


def test_rate_lengths_are_the_documented_constants():
    assert (DAYS_PER_WEEK, DAYS_PER_MONTH, DAYS_PER_YEAR) == (7, 30, 365)


def test_every_number_carries_formula_inputs_and_note():
    report = compute(
        campaign={"start_date": START, "deadline": DEADLINE, "targets": {"leads": 30, "won_value": 100}},
        deals=_sep_deals(4, "open", value=10) + _sep_deals(1, "won", value=25),
        events=[{"type": "contact", "timestamp": "2026-09-02T00:00:00", "deal_id": 1}],
        period="week",
    )
    _walk_metrics(report)
    assert report["periods"]["grouping"] == "week"
    assert report["periods"]["rows"]


def test_period_omitted_leaves_periods_none():
    assert compute(period=None)["periods"] is None
    assert compute(period="")["periods"] is None
    assert compute(period="  ")["periods"] is None


def test_none_campaign_deals_and_events_are_empty():
    report = compute_campaign_metrics(None, None, None, TODAY, "UTC")
    assert report["results"]["deals"]["value"] == 0
    assert report["targets"] == {}
    assert report["pacing_status"] is None
    assert report["win_loss"]["win_rate"]["note"] == NO_CLOSED_DEALS


def test_generator_inputs_are_consumed():
    report = compute(deals=(deal(i, "won", value=5) for i in range(2)))
    assert report["results"]["won"]["value"] == 2
    assert report["results"]["won_value"]["value"] == 10


def test_bad_inputs_raise():
    with pytest.raises(ValueError, match="period"):
        compute(period="daily")
    with pytest.raises(ValueError, match="timezone"):
        compute(tz="Not/AZone")
    with pytest.raises(ValueError, match="today"):
        compute(today=None)
    with pytest.raises(ValueError, match="today"):
        compute(today="2026-13-40")
    with pytest.raises(ValueError, match="start_date"):
        compute(campaign={"start_date": "yesterday"})
    with pytest.raises(ValueError, match="invalid target leads"):
        compute(campaign={"targets": {"leads": -1}})
    with pytest.raises(ValueError, match="invalid target"):
        compute(campaign={"targets": {"won": "many"}})
    with pytest.raises(ValueError, match="invalid target"):
        compute(campaign={"targets": {"contacts": True}})
    with pytest.raises(TypeError, match="deals"):
        compute(deals={"id": 1})
    with pytest.raises(TypeError, match="deal"):
        compute(deals=["nope"])
    with pytest.raises(TypeError, match="campaign"):
        compute(campaign=["nope"])
    with pytest.raises(TypeError, match="targets"):
        compute(campaign={"targets": ["leads"]})


def test_unset_targets_are_skipped_and_zero_is_kept():
    report = compute(
        campaign={"targets": {
            "leads": None,
            "contacts": "",
            "conversations": "   ",
            "advanced": 0,
            "ignored": 5,
        }},
    )
    assert list(report["targets"]) == ["advanced"]
    assert report["targets"]["advanced"]["percent_complete"]["value"] is None
    assert report["targets"]["advanced"]["percent_complete"]["note"] == TARGET_IS_ZERO
    assert report["targets"]["advanced"]["status"] is None


def test_numeric_strings_coerce():
    report = compute(
        campaign={"start_date": START, "deadline": DEADLINE, "targets": {"won_value": "1500.50"}},
        deals=[deal(1, "won", value="25.5"), deal(2, "won", value="10")],
    )
    assert report["targets"]["won_value"]["target"]["value"] == 1500.50
    assert report["targets"]["won_value"]["actual"]["value"] == 35.5


# --- win/loss -------------------------------------------------------------

def test_win_loss_ratio_excludes_disqualified_and_open():
    deals = (
        [deal(i, "won", value=100, lost_reason="should ignore", stage="won") for i in range(3)]
        + [
            deal(10, "lost", value=10, lost_reason="No budget"),
            deal(11, "lost", value=10, lost_reason="No budget"),
            deal(12, "lost", value=10, lost_reason="No budget"),
            deal(13, "lost", value=10, lost_reason="No budget"),
            deal(14, "lost", value=10, lost_reason="No budget"),
            deal(15, "lost", value=10, lost_reason="No response"),
            deal(16, "lost", value=10, lost_reason="No response"),
            deal(17, "lost", value=10, lost_reason="No response"),
            deal(18, "lost", value=None, lost_reason=""),
        ]
        + [deal(20, "disqualified", value=999, lost_reason="Wrong type", stage="new")]
        + [deal(21, "disqualified", value=999, lost_reason="Wrong type")]
        + [deal(30 + i, "open", value=5, stage="qualified") for i in range(4)]
    )
    report = compute(deals=deals)
    win = report["win_loss"]
    assert win["ratio"]["value"] == "3 : 9"
    assert win["ratio"]["note"] is None
    assert win["ratio"]["inputs"] == {"won": 3, "lost": 9}
    assert win["win_rate"]["value"] == 25
    assert win["win_rate"]["formula"] == "won / (won + lost) * 100"
    assert win["open"]["value"] == 4
    assert win["disqualified"]["value"] == 2
    assert win["disqualification_rate"]["value"] == pytest.approx(2 / 18 * 100)
    assert win["disqualification_rate"]["inputs"] == {"disqualified": 2, "deals": 18}
    assert win["won_value"]["value"] == 300
    assert win["lost_value"]["value"] == 80
    assert win["lost_value"]["inputs"]["missing_value"] == 1
    assert report["results"]["disqualified_value"]["value"] == 1998
    assert win["lost_by_reason"]["value"] == [
        {"reason": "No budget", "count": 5},
        {"reason": "No response", "count": 3},
        {"reason": "", "count": 1},
    ]
    assert report["results"]["leads"]["value"] == 16
    assert report["results"]["by_stage"]["value"]["qualified"] == 4
    assert report["results"]["by_stage"]["value"]["won"] == 3


def test_no_closed_deals_is_not_zero_percent():
    report = compute(deals=[
        deal(1, "open"),
        deal(2, "disqualified"),
        deal(3, "DISQUALIFIED"),
    ])
    win = report["win_loss"]
    assert win["win_rate"]["value"] is None
    assert win["win_rate"]["note"] == NO_CLOSED_DEALS
    assert win["ratio"]["value"] is None
    assert win["ratio"]["note"] == NO_CLOSED_DEALS
    assert win["win_rate"]["value"] != 0
    assert win["disqualified"]["value"] == 2
    assert report["results"]["leads"]["value"] == 1
    assert report["results"]["open"]["value"] == 1


def test_all_lost_is_a_real_zero_win_rate():
    report = compute(deals=[deal(i, "lost") for i in range(4)])
    assert report["win_loss"]["win_rate"]["value"] == 0
    assert report["win_loss"]["win_rate"]["note"] is None
    assert report["win_loss"]["ratio"]["value"] == "0 : 4"


def test_all_won_ratio_keeps_the_zero_lost_side():
    report = compute(deals=[deal(1, "won"), deal(2, "Won")])
    assert report["win_loss"]["ratio"]["value"] == "2 : 0"
    assert report["win_loss"]["win_rate"]["value"] == 100


def test_no_deals_has_no_disqualification_rate():
    report = compute()
    rate = report["win_loss"]["disqualification_rate"]
    assert rate["value"] is None
    assert rate["note"] == NO_LEADS
    assert report["win_loss"]["win_rate"]["note"] == NO_CLOSED_DEALS


def test_unknown_outcome_counts_as_open():
    report = compute(deals=[deal(1, "pending", stage=None, value="nope")])
    assert report["results"]["open"]["value"] == 1
    assert report["results"]["leads"]["value"] == 1
    assert report["results"]["by_stage"]["value"] == {"": 1}
    assert report["results"]["open_value"]["value"] == 0
    assert report["results"]["open_value"]["inputs"]["missing_value"] == 1


def test_zero_disqualification_rate_is_real_when_there_are_leads():
    report = compute(deals=[deal(1, "open")])
    assert report["win_loss"]["disqualification_rate"]["value"] == 0
    assert report["win_loss"]["disqualification_rate"]["note"] is None


# --- results: contacts ----------------------------------------------------

def test_contacted_is_unique_deals_and_targets_count_events():
    deals = [deal(1, "open"), deal(2, "open"), deal("3", "lost"), deal(4, "disqualified")]
    events = [
        {"type": "contact", "timestamp": "2026-09-02", "deal_id": 1},
        {"type": "Contact Made", "timestamp": "2026-09-03", "deal_id": 1},
        {"type": "conversation", "timestamp": "2026-09-04", "deal_id": "3"},
        {"type": "advanced", "timestamp": "2026-09-05", "deal_id": 99},
        {"type": "note", "timestamp": "2026-09-05", "deal_id": 2},
        {"type": "contact", "timestamp": "2026-09-06"},
    ]
    report = compute(deals=deals, events=events)
    assert report["results"]["contacts"]["value"] == 3
    assert report["results"]["conversations"]["value"] == 1
    assert report["results"]["advanced"]["value"] == 1
    assert report["results"]["contacted"]["value"] == 2
    assert report["results"]["not_touched"]["value"] == 2
    assert report["results"]["contacted"]["value"] + report["results"]["not_touched"]["value"] == 4


def test_occurred_at_is_the_timestamp_fallback():
    report = compute(
        events=[{"type": "conversations", "occurred_at": "2026-09-02T10:00:00"}],
    )
    assert report["results"]["conversations"]["value"] == 1


# --- pacing window --------------------------------------------------------

def _leads_campaign(actual_open, target=30, today=TODAY, **targets):
    body = {"leads": target}
    body.update(targets)
    return compute(
        campaign={"start_date": START, "deadline": DEADLINE, "targets": body},
        deals=_sep_deals(actual_open),
        today=today,
    )


def test_worked_example_from_the_docs():
    """1 Sep–30 Sep, today 16 Sep, target 30, actual 10. Disqualified do not count."""
    report = compute(
        campaign={"start_date": START, "deadline": DEADLINE, "targets": {"leads": 30}},
        deals=_sep_deals(10) + [deal(100, "disqualified"), deal(101, "disqualified")],
    )
    window = report["window"]
    assert window["state"] == "in_progress"
    assert window["deadline_passed"] is False
    assert window["not_started"] is False
    assert window["days_total"]["value"] == 30
    assert window["days_elapsed"]["value"] == 15
    assert window["days_remaining"]["value"] == 15
    assert window["days_elapsed"]["value"] + window["days_remaining"]["value"] == 30
    assert window["percent_elapsed"]["value"] == 50
    assert window["days_total"]["formula"] == "(deadline - start_date).days + 1"

    row = report["targets"]["leads"]
    assert row["actual"]["value"] == 10
    assert row["actual"]["inputs"]["disqualified"] == 2
    assert row["remaining"]["value"] == 20
    assert row["percent_complete"]["value"] == pytest.approx(10 / 30 * 100)
    assert row["required_rate"]["week"]["value"] == pytest.approx(20 / 15 * DAYS_PER_WEEK)
    assert row["required_rate"]["month"]["value"] == 40
    assert row["required_rate"]["year"]["value"] == pytest.approx(20 / 15 * DAYS_PER_YEAR)
    assert row["required_rate"]["week"]["formula"] == "remaining / days_remaining * 7"
    assert row["required_rate"]["week"]["inputs"] == {
        "remaining": 20,
        "days_remaining": 15,
        "days_per_week": 7,
    }
    assert row["actual_rate"]["week"]["value"] == pytest.approx(10 / 15 * 7)
    assert row["actual_rate"]["month"]["value"] == 20
    assert row["actual_rate"]["year"]["value"] == pytest.approx(10 / 15 * 365)
    assert row["projection"]["value"] == 20
    assert row["projection"]["formula"] == "actual / days_elapsed * days_total"
    assert row["days_elapsed"] is window["days_elapsed"]
    assert row["days_remaining"]["value"] == 15
    assert row["percent_elapsed"]["value"] == 50
    assert row["status"] == BEHIND
    assert row["status_inputs"] == {
        "percent_complete": pytest.approx(10 / 30 * 100),
        "percent_elapsed": 50,
    }
    assert report["pacing_status"] == BEHIND


def test_on_track_ahead_and_over_target():
    on_track = _leads_campaign(15)
    assert on_track["targets"]["leads"]["status"] == ON_TRACK
    assert on_track["targets"]["leads"]["percent_complete"]["value"] == 50
    assert on_track["pacing_status"] == ON_TRACK

    ahead = _leads_campaign(20)
    assert ahead["targets"]["leads"]["status"] == AHEAD
    assert ahead["targets"]["leads"]["projection"]["value"] == 40

    over = _leads_campaign(36)
    row = over["targets"]["leads"]
    assert row["remaining"]["value"] == -6
    assert row["percent_complete"]["value"] == 120
    assert row["status"] == AHEAD
    assert row["required_rate"]["week"]["value"] == pytest.approx(-6 / 15 * 7)


def test_pacing_status_rollup():
    behind = compute(
        campaign={"start_date": START, "deadline": DEADLINE, "targets": {"leads": 30, "contacts": 30}},
        deals=_sep_deals(10),
        events=[{"type": "contact", "timestamp": f"2026-09-{i:02d}"} for i in range(1, 21)],
    )
    assert behind["targets"]["leads"]["status"] == BEHIND
    assert behind["targets"]["contacts"]["status"] == AHEAD
    assert behind["pacing_status"] == BEHIND

    mixed = compute(
        campaign={"start_date": START, "deadline": DEADLINE, "targets": {"leads": 30, "contacts": 30}},
        deals=_sep_deals(15),
        events=[{"type": "contact", "timestamp": f"2026-09-{i:02d}"} for i in range(1, 21)],
    )
    assert mixed["targets"]["leads"]["status"] == ON_TRACK
    assert mixed["targets"]["contacts"]["status"] == AHEAD
    assert mixed["pacing_status"] == ON_TRACK

    both = compute(
        campaign={"start_date": START, "deadline": DEADLINE, "targets": {"leads": 30, "won": 30}},
        deals=_sep_deals(20, "won", closed_at="2026-09-10"),
    )
    assert both["targets"]["leads"]["status"] == AHEAD
    assert both["targets"]["won"]["status"] == AHEAD
    assert both["pacing_status"] == AHEAD


def test_no_deadline_has_actual_rate_only():
    report = compute(
        campaign={"start_date": START, "targets": {"leads": 30}},
        deals=_sep_deals(10),
    )
    row = report["targets"]["leads"]
    window = report["window"]
    assert window["state"] == "no_deadline"
    assert window["days_total"]["value"] is None
    assert window["days_total"]["note"] == NO_DEADLINE
    assert window["days_elapsed"]["value"] == 15
    assert window["percent_elapsed"]["value"] is None
    assert row["required_rate"]["week"]["value"] is None
    assert row["required_rate"]["month"]["note"] == NO_DEADLINE
    assert row["required_rate"]["year"]["note"] == NO_DEADLINE
    assert row["projection"]["value"] is None
    assert row["projection"]["note"] == NO_DEADLINE
    assert row["actual_rate"]["week"]["value"] == pytest.approx(10 / 15 * 7)
    assert row["status"] is None
    assert report["pacing_status"] is None
    assert report["results"]["leads"]["value"] == 10


def test_no_targets_keeps_results_and_win_loss():
    report = compute(
        campaign={"start_date": START, "deadline": DEADLINE},
        deals=[deal(1, "won"), deal(2, "lost")],
    )
    assert report["targets"] == {}
    assert report["pacing_status"] is None
    assert report["win_loss"]["ratio"]["value"] == "1 : 1"
    assert report["window"]["percent_elapsed"]["value"] == 50


def test_no_start_date_blocks_rates():
    report = compute(
        campaign={"deadline": DEADLINE, "targets": {"leads": 10}},
        deals=[deal(1, "open")],
    )
    assert report["window"]["state"] == "no_start"
    row = report["targets"]["leads"]
    assert row["actual"]["value"] == 1
    assert row["required_rate"]["week"]["value"] is None
    assert row["required_rate"]["week"]["note"] == NO_START_DATE
    assert row["actual_rate"]["week"]["note"] == NO_START_DATE
    assert row["projection"]["note"] == NO_START_DATE
    assert row["percent_complete"]["value"] == 10


def test_deadline_passed_is_final_with_no_required_rate():
    report = _leads_campaign(20, today="2026-10-01")
    window = report["window"]
    row = report["targets"]["leads"]
    assert window["state"] == "ended"
    assert window["deadline_passed"] is True
    assert window["days_elapsed"]["value"] == 30
    assert window["days_remaining"]["value"] == 0
    assert window["percent_elapsed"]["value"] == 100
    assert row["required_rate"]["week"]["value"] is None
    assert row["required_rate"]["week"]["note"] == DEADLINE_PASSED
    assert row["required_rate"]["month"]["note"] == DEADLINE_PASSED
    assert row["required_rate"]["year"]["note"] == DEADLINE_PASSED
    assert row["projection"]["value"] is None
    assert row["projection"]["note"] == DEADLINE_PASSED
    assert row["actual_rate"]["week"]["value"] == pytest.approx(20 / 30 * 7)
    assert row["status"] == BEHIND

    made = _leads_campaign(30, today="2026-10-01")
    assert made["targets"]["leads"]["status"] == ON_TRACK
    extra = _leads_campaign(40, today="2026-10-01")
    assert extra["targets"]["leads"]["status"] == AHEAD
    assert extra["targets"]["leads"]["remaining"]["value"] == -10


def test_deadline_day_is_still_inside_the_window():
    report = _leads_campaign(0, target=7, today=DEADLINE)
    window = report["window"]
    assert window["state"] == "in_progress"
    assert window["deadline_passed"] is False
    assert window["days_total"]["value"] == 30
    assert window["days_elapsed"]["value"] == 29
    assert window["days_remaining"]["value"] == 1
    assert report["targets"]["leads"]["required_rate"]["week"]["value"] == pytest.approx(7 / 1 * 7)


def test_start_date_in_the_future():
    report = compute(
        campaign={"start_date": "2026-11-01", "deadline": "2026-11-30", "targets": {"leads": 30}},
        deals=_sep_deals(5),
        today="2026-10-24",
    )
    window = report["window"]
    row = report["targets"]["leads"]
    assert window["state"] == "not_started"
    assert window["not_started"] is True
    assert window["days_total"]["value"] == 30
    assert window["days_elapsed"]["value"] == 0
    assert window["days_elapsed"]["note"] == START_IN_FUTURE
    assert window["days_remaining"]["value"] == 30
    assert window["percent_elapsed"]["value"] == 0
    assert row["actual_rate"]["week"]["value"] is None
    assert row["actual_rate"]["week"]["note"] == START_IN_FUTURE
    assert row["projection"]["note"] == START_IN_FUTURE
    assert row["required_rate"]["week"]["value"] == pytest.approx(25 / 30 * 7)
    assert row["status"] == AHEAD


def test_future_start_with_nothing_done_is_on_track():
    report = compute(
        campaign={"start_date": "2026-11-01", "deadline": "2026-11-30", "targets": {"leads": 30}},
        today="2026-10-24",
    )
    assert report["targets"]["leads"]["status"] == ON_TRACK
    assert report["targets"]["leads"]["required_rate"]["month"]["value"] == 30


def test_start_day_has_no_elapsed_time_but_a_required_rate():
    report = compute(
        campaign={"start_date": TODAY, "deadline": DEADLINE, "targets": {"leads": 15}},
        deals=[deal(1, "open")],
    )
    window = report["window"]
    row = report["targets"]["leads"]
    assert window["state"] == "in_progress"
    assert window["not_started"] is False
    assert window["days_total"]["value"] == 15
    assert window["days_elapsed"]["value"] == 0
    assert window["days_elapsed"]["note"] == NO_ELAPSED_DAYS
    assert window["days_remaining"]["value"] == 15
    assert row["actual_rate"]["week"]["value"] is None
    assert row["actual_rate"]["week"]["note"] == NO_ELAPSED_DAYS
    assert row["projection"]["value"] is None
    assert row["required_rate"]["week"]["value"] == pytest.approx(14 / 15 * 7)
    assert row["status"] == AHEAD


def test_zero_length_window_does_not_divide():
    report = compute(
        campaign={"start_date": "2026-09-01", "deadline": "2026-08-01", "targets": {"leads": 10, "won": 4}},
        deals=[deal(1, "won"), deal(2, "won"), deal(3, "lost"), deal(4, "open")],
        period="month",
    )
    window = report["window"]
    assert window["state"] == "zero_length"
    assert window["days_total"]["value"] == 0
    assert window["days_total"]["note"] == ZERO_LENGTH_WINDOW
    assert window["percent_elapsed"]["value"] is None
    assert window["percent_elapsed"]["note"] == ZERO_LENGTH_WINDOW
    assert window["deadline_passed"] is False
    for key in ("leads", "won"):
        row = report["targets"][key]
        for period_name in ("week", "month", "year"):
            assert row["required_rate"][period_name]["value"] is None
            assert row["required_rate"][period_name]["note"] == ZERO_LENGTH_WINDOW
            assert row["actual_rate"][period_name]["value"] is None
            assert row["actual_rate"][period_name]["note"] == ZERO_LENGTH_WINDOW
        assert row["projection"]["value"] is None
        assert row["projection"]["note"] == ZERO_LENGTH_WINDOW
        assert row["status"] is None
    assert report["pacing_status"] is None
    assert report["results"]["won"]["value"] == 2
    assert report["win_loss"]["ratio"]["value"] == "2 : 1"
    assert report["periods"]["window_days"] is None
    assert report["periods"]["rows"]
    assert report["periods"]["rows"][0]["required"]["leads"]["value"] is None
    assert report["periods"]["rows"][0]["required"]["leads"]["note"] == ZERO_LENGTH_WINDOW


def test_same_day_window_is_one_day_not_zero():
    report = compute(
        campaign={"start_date": TODAY, "deadline": TODAY, "targets": {"leads": 7}},
        today=TODAY,
    )
    window = report["window"]
    assert window["state"] == "in_progress"
    assert window["days_total"]["value"] == 1
    assert window["days_elapsed"]["value"] == 0
    assert window["days_remaining"]["value"] == 1
    assert report["targets"]["leads"]["required_rate"]["week"]["value"] == 49
    assert report["targets"]["leads"]["actual_rate"]["week"]["value"] is None

    ended = compute(
        campaign={"start_date": TODAY, "deadline": TODAY, "targets": {"leads": 7}},
        deals=_sep_deals(7),
        today="2026-09-17",
    )
    assert ended["window"]["state"] == "ended"
    assert ended["window"]["days_elapsed"]["value"] == 1
    assert ended["targets"]["leads"]["required_rate"]["week"]["value"] is None
    assert ended["targets"]["leads"]["actual_rate"]["week"]["value"] == pytest.approx(7 / 1 * 7)
    assert ended["targets"]["leads"]["status"] == ON_TRACK


def test_elapsed_plus_remaining_matches_total_across_the_window():
    start = date(2026, 9, 1)
    deadline = date(2026, 9, 30)
    for offset in range(-2, 35):
        today = start + timedelta(days=offset)
        report = compute(
            campaign={"start_date": START, "deadline": DEADLINE, "targets": {"leads": 1}},
            today=today.isoformat(),
        )
        window = report["window"]
        assert window["days_elapsed"]["value"] + window["days_remaining"]["value"] == 30
        if today < start:
            assert window["state"] == "not_started"
        elif today > deadline:
            assert window["state"] == "ended"
            assert report["targets"]["leads"]["required_rate"]["week"]["value"] is None
        else:
            assert window["state"] == "in_progress"


def test_no_deadline_and_future_start():
    report = compute(
        campaign={"start_date": "2026-11-01", "targets": {"leads": 4}},
        today="2026-10-24",
    )
    assert report["window"]["state"] == "no_deadline"
    assert report["window"]["not_started"] is True
    assert report["window"]["days_elapsed"]["value"] == 0
    assert report["targets"]["leads"]["actual_rate"]["week"]["note"] == START_IN_FUTURE
    assert report["targets"]["leads"]["required_rate"]["week"]["note"] == NO_DEADLINE


def test_no_deadline_on_the_start_date_has_no_actual_rate():
    report = compute(
        campaign={"start_date": TODAY, "targets": {"contacts": 3}},
        events=[{"type": "contact", "timestamp": "2026-09-16T01:00:00"}],
    )
    assert report["window"]["days_elapsed"]["value"] == 0
    assert report["targets"]["contacts"]["actual"]["value"] == 1
    assert report["targets"]["contacts"]["actual_rate"]["week"]["note"] == NO_ELAPSED_DAYS


# --- calendar and timezone ------------------------------------------------

def test_timezone_objects_are_accepted():
    from datetime import timezone
    from zoneinfo import ZoneInfo

    utc = compute(tz=timezone.utc)
    assert utc["window"]["timezone"] == "UTC"
    zone = compute(tz=ZoneInfo("Australia/Sydney"))
    assert zone["window"]["timezone"] == "Australia/Sydney"


def test_calendar_dates_do_not_shift_and_event_instants_do():
    report = compute(
        campaign={"start_date": "2026-09-01T23:00:00", "deadline": "2026-09-30"},
        tz="Australia/Sydney",
    )
    assert report["window"]["start_date"] == "2026-09-01"
    assert report["window"]["timezone"] == "Australia/Sydney"

    aware = compute(
        campaign={"start_date": "2026-08-31T16:00:00Z", "deadline": DEADLINE},
        tz="Australia/Sydney",
    )
    assert aware["window"]["start_date"] == "2026-09-01"

    shifted_today = compute(
        campaign={"start_date": START, "deadline": DEADLINE},
        today="2026-09-16T18:00:00Z",
        tz="Australia/Sydney",
    )
    assert shifted_today["window"]["today"] == "2026-09-17"

    naive_today = compute(
        campaign={"start_date": START, "deadline": DEADLINE},
        today="2026-09-16T18:00:00",
        tz="Australia/Sydney",
    )
    assert naive_today["window"]["today"] == "2026-09-16"


def test_weeks_start_monday_and_follow_the_timezone():
    event = {"type": "contact", "timestamp": "2026-09-06T15:00:00Z", "deal_id": 1}
    campaign = {"start_date": START, "deadline": DEADLINE, "targets": {"contacts": 1}}

    utc = compute(campaign=campaign, events=[event], period="week", tz="UTC")
    sydney = compute(campaign=campaign, events=[event], period="Week", tz="Australia/Sydney")

    def row_with_contact(report):
        matches = [row for row in report["periods"]["rows"] if row["actuals"]["contacts"]["value"] == 1]
        assert len(matches) == 1
        return matches[0]

    utc_row = row_with_contact(utc)
    sydney_row = row_with_contact(sydney)
    assert utc_row["key"] == "2026-W36"
    assert utc_row["start"] == "2026-08-31"
    assert utc_row["end"] == "2026-09-06"
    assert sydney_row["key"] == "2026-W37"
    assert sydney_row["start"] == "2026-09-07"
    assert date.fromisoformat(sydney_row["start"]).weekday() == 0

    date_only = compute(
        campaign=campaign,
        events=[{"type": "contact", "timestamp": "2026-09-06", "deal_id": 1}],
        period="week",
        tz="Australia/Sydney",
    )
    assert row_with_contact(date_only)["key"] == "2026-W36"

    for row in utc["periods"]["rows"]:
        start = date.fromisoformat(row["start"])
        end = date.fromisoformat(row["end"])
        assert start.weekday() == 0
        assert end.weekday() == 6
        assert (end - start).days == 6


# --- period breakdown -----------------------------------------------------

def test_week_proration_adds_up_and_skips_disqualified_leads():
    deals = [
        deal(1, "open", created_at="2026-09-01", stage="new"),
        deal(2, "open", created_at="2026-09-10", stage="contacted"),
        deal(3, "disqualified", created_at="2026-09-03", closed_at="2026-09-15", value=50),
        deal(4, "won", created_at="2026-09-02", closed_at="2026-09-16", value=100, stage="won"),
        deal(5, "lost", created_at="2026-09-05", closed_at="2026-09-16", value=40, lost_reason="No budget"),
        deal(6, "lost", created_at="2026-09-08", closed_at="2026-09-09", value=10, lost_reason="No budget"),
        deal(7, "lost", created_at="2026-09-04", closed_at="2026-09-18", value=None, lost_reason="No response"),
        deal(8, "open", created_at=None),
    ]
    events = [
        {"type": "contact", "timestamp": "2026-09-02T01:00:00", "deal_id": 1},
        {"type": "conversation", "timestamp": "2026-09-16T09:00:00", "deal_id": 4},
        {"type": "advance", "timestamp": "not-a-date", "deal_id": 2},
    ]
    report = compute(
        campaign={
            "start_date": START,
            "deadline": DEADLINE,
            "targets": {"leads": 30, "contacts": 8, "won": 4, "won_value": 200},
        },
        deals=deals,
        events=events,
        period="week",
    )
    rows = report["periods"]["rows"]
    assert [row["key"] for row in rows] == [
        "2026-W36", "2026-W37", "2026-W38", "2026-W39", "2026-W40",
    ]
    assert [row["overlap_days"] for row in rows] == [6, 7, 7, 7, 3]
    assert [row["required"]["leads"]["value"] for row in rows] == [6, 7, 7, 7, 3]
    assert [row["cumulative_target"]["leads"]["value"] for row in rows] == [6, 13, 20, 27, 30]
    assert sum(row["required"]["leads"]["value"] for row in rows) == 30
    assert rows[-1]["cumulative_target"]["contacts"]["value"] == 8

    # Disqualified deal 3 is not a lead. Undated open deal 8 is not in a row.
    # Created: 1, 2, 4, 5, 6, 7 → six dated leads. W36 holds 1 Sep, 2 Sep, 4 Sep, 5 Sep.
    assert [row["actuals"]["leads"]["value"] for row in rows] == [4, 2, 0, 0, 0]
    assert rows[-1]["cumulative_actual"]["leads"]["value"] == 6
    assert report["periods"]["undated"]["leads"] == 1
    assert report["results"]["leads"]["value"] == 7

    # Won deal closes 16 Sep (W38). Two lost that week, one lost on 9 Sep (W37).
    assert rows[2]["key"] == "2026-W38"
    assert rows[2]["won"]["value"] == 1
    assert rows[2]["lost"]["value"] == 2
    assert rows[2]["disqualified"]["value"] == 1
    assert rows[2]["win_rate"]["value"] == pytest.approx(1 / 3 * 100)
    assert rows[2]["actuals"]["won"]["value"] == 1
    assert rows[2]["actuals"]["won_value"]["value"] == 100
    assert rows[1]["won"]["value"] == 0
    assert rows[1]["lost"]["value"] == 1
    assert rows[1]["win_rate"]["value"] == 0
    assert rows[0]["win_rate"]["value"] is None
    assert rows[0]["win_rate"]["note"] == NO_CLOSED_DEALS
    # A week with only a disqualification is still "no closed deals".
    # W38 has both, so check a week that only has the dq by moving none here:
    # deal 3 closes in W38 along with wins. Build that case separately below.

    assert rows[0]["actuals"]["contacts"]["value"] == 1
    assert report["periods"]["undated"]["advanced"] == 1
    assert report["results"]["advanced"]["value"] == 1
    assert sum(row["actuals"]["contacts"]["value"] for row in rows) + report["periods"]["undated"]["contacts"] == 1
    _walk_metrics(report)


def test_period_with_only_disqualified_deals_has_no_win_rate():
    report = compute(
        campaign={"start_date": START, "deadline": DEADLINE},
        deals=[deal(1, "disqualified", created_at="2026-09-01", closed_at="2026-09-02")],
        period="week",
    )
    row = report["periods"]["rows"][0]
    assert row["disqualified"]["value"] == 1
    assert row["won"]["value"] == 0
    assert row["lost"]["value"] == 0
    assert row["win_rate"]["value"] is None
    assert row["win_rate"]["note"] == NO_CLOSED_DEALS
    assert report["results"]["leads"]["value"] == 0


def test_partial_weeks_outside_the_window_require_zero():
    report = compute(
        campaign={"start_date": "2026-09-14", "deadline": DEADLINE, "targets": {"contacts": 17}},
        events=[{"type": "contact", "timestamp": "2026-09-01"}],
        period="week",
    )
    rows = report["periods"]["rows"]
    assert [row["key"] for row in rows] == [
        "2026-W36", "2026-W37", "2026-W38", "2026-W39", "2026-W40",
    ]
    assert [row["overlap_days"] for row in rows] == [0, 0, 7, 7, 3]
    assert [row["required"]["contacts"]["value"] for row in rows] == [0, 0, 7, 7, 3]
    assert rows[0]["actuals"]["contacts"]["value"] == 1
    assert rows[0]["cumulative_target"]["contacts"]["value"] == 0
    assert rows[-1]["cumulative_target"]["contacts"]["value"] == 17
    assert sum(row["required"]["contacts"]["value"] for row in rows) == 17
    assert report["periods"]["window_days"] == 17
    assert report["results"]["contacts"]["value"] == 1


def test_month_and_year_grouping():
    month = compute(
        campaign={"start_date": START, "deadline": "2026-10-15", "targets": {"leads": 45}},
        deals=[deal(1, "open", created_at="2026-09-16"), deal(2, "open", created_at="2026-10-02")],
        period="month",
    )
    rows = month["periods"]["rows"]
    assert [row["key"] for row in rows] == ["2026-09", "2026-10"]
    assert [row["overlap_days"] for row in rows] == [30, 15]
    assert month["window"]["days_total"]["value"] == 45
    assert [row["required"]["leads"]["value"] for row in rows] == [30, 15]
    assert [row["actuals"]["leads"]["value"] for row in rows] == [1, 1]
    assert rows[-1]["cumulative_actual"]["leads"]["value"] == 2
    assert rows[-1]["cumulative_target"]["leads"]["value"] == 45

    year = compute(
        campaign={"start_date": "2026-12-15", "deadline": "2027-01-14", "targets": {"won": 31}},
        deals=[
            deal(1, "won", created_at="2026-12-20", closed_at="2026-12-31"),
            deal(2, "lost", created_at="2027-01-02", closed_at="2027-01-05"),
        ],
        period="YEAR",
    )
    yrows = year["periods"]["rows"]
    assert [row["key"] for row in yrows] == ["2026", "2027"]
    assert [row["overlap_days"] for row in yrows] == [17, 14]
    assert year["window"]["days_total"]["value"] == 31
    assert yrows[0]["won"]["value"] == 1
    assert yrows[0]["win_rate"]["value"] == 100
    assert yrows[1]["lost"]["value"] == 1
    assert yrows[1]["win_rate"]["value"] == 0
    # 31 Dec 2026 is a Thursday; its week starts Monday 28 Dec and includes 1 Jan.
    week = compute(
        campaign={"start_date": "2026-12-28", "deadline": "2027-01-03", "targets": {"leads": 7}},
        deals=[deal(1, "open", created_at="2027-01-01")],
        period="week",
    )
    assert len(week["periods"]["rows"]) == 1
    only = week["periods"]["rows"][0]
    assert only["start"] == "2026-12-28"
    assert only["end"] == "2027-01-03"
    assert only["key"] == "2026-W53"
    assert only["actuals"]["leads"]["value"] == 1
    assert only["overlap_days"] == 7


def test_future_dated_events_still_count_and_extend_the_table():
    report = compute(
        campaign={"start_date": START, "deadline": DEADLINE, "targets": {"contacts": 2}},
        events=[{"type": "contact", "timestamp": "2026-10-20"}],
        period="week",
    )
    assert report["results"]["contacts"]["value"] == 1
    assert report["targets"]["contacts"]["actual"]["value"] == 1
    assert report["periods"]["rows"][-1]["start"] <= "2026-10-20" <= report["periods"]["rows"][-1]["end"]
    assert report["periods"]["rows"][-1]["overlap_days"] == 0
    assert report["periods"]["rows"][-1]["required"]["contacts"]["value"] == 0
    assert report["periods"]["rows"][-1]["actuals"]["contacts"]["value"] == 1


def test_no_deadline_period_runs_through_today():
    report = compute(
        campaign={"start_date": "2026-09-01", "targets": {"leads": 4}},
        deals=[deal(1, "open", created_at="2026-09-02")],
        today="2026-09-20",
        period="week",
    )
    assert report["periods"]["rows"][0]["start"] == "2026-08-31"
    assert report["periods"]["rows"][-1]["start"] <= "2026-09-20" <= report["periods"]["rows"][-1]["end"]
    assert report["periods"]["rows"][0]["required"]["leads"]["value"] is None
    assert report["periods"]["rows"][0]["required"]["leads"]["note"] == NO_DEADLINE
    assert report["periods"]["window_days"] is None


def test_empty_period_table_is_the_current_week():
    report = compute(period="week")
    assert len(report["periods"]["rows"]) == 1
    row = report["periods"]["rows"][0]
    assert row["start"] <= TODAY <= row["end"]
    assert row["won"]["value"] == 0
    assert row["win_rate"]["note"] == NO_CLOSED_DEALS
    assert row["actuals"] == {}


def test_ended_campaign_keeps_the_planned_period_required():
    report = compute(
        campaign={"start_date": START, "deadline": DEADLINE, "targets": {"leads": 30}},
        today="2026-10-10",
        period="month",
    )
    assert report["targets"]["leads"]["required_rate"]["week"]["value"] is None
    row = report["periods"]["rows"][0]
    assert row["key"] == "2026-09"
    assert row["required"]["leads"]["value"] == 30
    assert row["required"]["leads"]["note"] is None
    assert row["cumulative_target"]["leads"]["value"] == 30
