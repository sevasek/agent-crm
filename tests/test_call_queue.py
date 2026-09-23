from datetime import datetime, timedelta

from app.database import get_db
from app.services.partners import create_partner
from app.services.catalog import create_service
from app.services.deals import create_deal, set_deal_stage, stage_changed_body, stage_changed_to_like
from app.services.call_queue import (
    score_deal, list_todays_calls, CALL_QUEUE_LIMIT, SCORE_MAX,
    VALUE_MAX, SOURCE_MAX, CONTACT_MAX, RECENCY_MAX,
)


def test_score_value_buckets():
    assert score_deal({"value_estimate": 8000})["value_points"] == VALUE_MAX
    assert score_deal({"value_estimate": 5000})["value_points"] == VALUE_MAX
    assert score_deal({"value_estimate": 1000})["value_points"] == 20
    assert score_deal({"value_estimate": 500})["value_points"] == 10
    assert score_deal({"value_estimate": None})["value_points"] == 0
    assert score_deal({})["value_points"] == 0
    assert score_deal({"value_estimate": -50})["value_points"] == 0
    assert score_deal({"value_estimate": -50})["reasons"]["value"] == "invalid estimate"
    assert score_deal({"value_estimate": None})["reasons"]["value"] == "no estimate"


def test_score_source_buckets():
    assert score_deal({"source": "referral"})["source_points"] == SOURCE_MAX
    assert score_deal({"source": "Referral from existing client"})["source_points"] == SOURCE_MAX
    assert score_deal({"source": "cold outreach"})["source_points"] == 10
    assert score_deal({"source": "cold outreach via website"})["source_points"] == 10
    assert score_deal({"source": "website form"})["source_points"] == 15
    assert score_deal({"source": "conference booth"})["source_points"] == 5
    assert score_deal({"source": "information request"})["source_points"] == 5
    assert score_deal({"source": "performance marketing"})["source_points"] == 5
    assert score_deal({"source": "web form"})["source_points"] == 15
    assert score_deal({"source": ""})["source_points"] == 0
    assert score_deal({})["source_points"] == 0


def test_score_contact_completeness():
    empty = score_deal({})
    assert empty["contact_points"] == 0

    full = score_deal({
        "partner_email": "jane@acme.example",
        "partner_preferred_channel": "phone",
        "partner_title": "Director",
        "partner_social_url": "https://linkedin.com/in/jane",
        "partner_website": "https://acme.example",
    })
    assert full["contact_points"] == CONTACT_MAX

    email_only = score_deal({"partner_email": "jane@acme.example"})
    assert email_only["contact_points"] == 5


def test_score_recency_buckets():
    now = datetime(2026, 8, 30, 12, 0, 0)
    assert score_deal({"qualified_at": (now - timedelta(days=2)).isoformat()}, now=now)["recency_points"] == RECENCY_MAX
    assert score_deal({"qualified_at": (now - timedelta(days=10)).isoformat()}, now=now)["recency_points"] == 15
    assert score_deal({"qualified_at": (now - timedelta(days=20)).isoformat()}, now=now)["recency_points"] == 10
    assert score_deal({"qualified_at": (now - timedelta(days=45)).isoformat()}, now=now)["recency_points"] == 5
    assert score_deal({"qualified_at": (now - timedelta(days=90)).isoformat()}, now=now)["recency_points"] == 0
    assert score_deal({}, now=now)["recency_points"] == 0
    # updated_at must not stand in for "when they entered qualified"
    assert score_deal({"updated_at": now.isoformat()}, now=now)["recency_points"] == 0


def test_stage_changed_like_matches_written_body():
    body = stage_changed_body("contacted", "qualified")
    like = stage_changed_to_like("qualified")
    prefix, _, suffix = like.partition("%")
    assert body.startswith(prefix) and body.endswith(suffix)
    assert body == "Stage changed: contacted -> qualified"


def test_perfect_lead_scores_100():
    now = datetime(2026, 8, 30, 12, 0, 0)
    result = score_deal({
        "value_estimate": 8000,
        "source": "referral",
        "partner_email": "jane@acme.example",
        "partner_preferred_channel": "phone",
        "partner_title": "Director",
        "partner_social_url": "https://linkedin.com/in/jane",
        "partner_website": "https://acme.example",
        "qualified_at": now.isoformat(),
    }, now=now)
    assert result["score"] == SCORE_MAX


def test_list_only_qualified_with_phone(db):
    sid = create_service("Consulting", "consulting")

    callable_pid = create_partner("Jane Doe", phone="0400 000 001", email="jane@example.com")
    no_phone_pid = create_partner("No Phone", email="nophone@example.com")
    contacted_pid = create_partner("Still Contacted", phone="0400 000 002")

    callable_deal = create_deal(callable_pid, sid)
    set_deal_stage(callable_deal, "qualified")

    no_phone_deal = create_deal(no_phone_pid, sid)
    set_deal_stage(no_phone_deal, "qualified")

    contacted_deal = create_deal(contacted_pid, sid)
    set_deal_stage(contacted_deal, "contacted")

    calls = list_todays_calls()
    ids = [c["id"] for c in calls]
    assert ids == [callable_deal]


def test_blank_phone_excluded(db):
    sid = create_service("Consulting", "consulting")
    pid = create_partner("Whitespace Phone", phone="0400 000 003")
    # Force a blank phone after create, the way a messy import might.
    with get_db() as conn:
        conn.execute("UPDATE partners SET phone = '   ' WHERE id = ?", (pid,))
        conn.commit()
    deal_id = create_deal(pid, sid)
    set_deal_stage(deal_id, "qualified")
    assert list_todays_calls() == []


def test_undialable_phone_excluded(db):
    # SQL's non-blank check alone would let this through — no digit in
    # "---", so it's not actually dialable even though it isn't blank.
    sid = create_service("Consulting", "consulting")
    pid = create_partner("Punctuation Phone", phone="---")
    deal_id = create_deal(pid, sid)
    set_deal_stage(deal_id, "qualified")
    assert list_todays_calls() == []


def test_include_new_returns_dialable_new_stage_deals(db):
    sid = create_service("Dead Lead Reactivation", "dead-lead-reactivation")
    pid = create_partner("Cafe North", is_company=True, phone="0400 555 001")
    deal_id = create_deal(pid, sid, source="scrape:cafes")
    assert list_todays_calls() == []
    calls = list_todays_calls(include_new=True, service_slug="dead-lead-reactivation")
    assert [c["id"] for c in calls] == [deal_id]
    staged = list_todays_calls(stage="new", service_slug="dead-lead-reactivation")
    assert [c["id"] for c in staged] == [deal_id]
    assert staged[0]["partner_phone"] == "0400 555 001"


def test_call_queue_owner_filter(db):
    sid = create_service("Consulting", "consulting")
    bob_pid = create_partner("Bob Lead", phone="0400 555 010")
    alice_pid = create_partner("Alice Lead", phone="0400 555 011")
    bob_deal = create_deal(bob_pid, sid, owner_key="bob")
    alice_deal = create_deal(alice_pid, sid, owner_key="alice")
    set_deal_stage(bob_deal, "qualified")
    set_deal_stage(alice_deal, "qualified")
    mine = list_todays_calls(owner_key="bob")
    assert [c["id"] for c in mine] == [bob_deal]


def test_ranked_by_score_value_beats_lower_value(db):
    sid = create_service("Consulting", "consulting")
    low_pid = create_partner("Low Value", phone="0400 000 010")
    high_pid = create_partner("High Value", phone="0400 000 011")
    low_deal = create_deal(low_pid, sid, value_estimate=200)
    high_deal = create_deal(high_pid, sid, value_estimate=8000)
    set_deal_stage(low_deal, "qualified")
    set_deal_stage(high_deal, "qualified")

    calls = list_todays_calls()
    assert [c["id"] for c in calls] == [high_deal, low_deal]
    assert calls[0]["rank"] == 1
    assert calls[0]["score"] > calls[1]["score"]


def test_top_10_cap(db):
    sid = create_service("Consulting", "consulting")
    for i in range(12):
        pid = create_partner(f"Lead {i:02d}", phone=f"0400 000 {i:03d}")
        extra = {"value_estimate": 8000, "source": "referral"} if i >= 2 else {}
        deal_id = create_deal(pid, sid, **extra)
        set_deal_stage(deal_id, "qualified")

    calls = list_todays_calls()
    assert len(calls) == CALL_QUEUE_LIMIT
    assert [c["rank"] for c in calls] == list(range(1, CALL_QUEUE_LIMIT + 1))
    names = {c["partner_name"] for c in calls}
    assert "Lead 00" not in names
    assert "Lead 01" not in names
    assert "Lead 02" in names
    assert "Lead 11" in names


def test_recency_breaks_ties_newer_first(db):
    sid = create_service("Consulting", "consulting")
    older_pid = create_partner("Older Qual", phone="0400 000 020")
    newer_pid = create_partner("Newer Qual", phone="0400 000 021")
    older_deal = create_deal(older_pid, sid, source="conference")
    newer_deal = create_deal(newer_pid, sid, source="conference")
    set_deal_stage(older_deal, "qualified")
    set_deal_stage(newer_deal, "qualified")

    old_ts = (datetime.utcnow() - timedelta(days=20)).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        conn.execute(
            "UPDATE activities SET occurred_at = ? WHERE deal_id = ? AND body LIKE ?",
            (old_ts, older_deal, stage_changed_to_like("qualified")),
        )
        conn.commit()

    calls = list_todays_calls()
    assert [c["id"] for c in calls] == [newer_deal, older_deal]
    assert calls[0]["recency_points"] > calls[1]["recency_points"]


def test_calls_page_requires_login(client):
    resp = client.get("/calls", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


def test_calls_page_empty_state(logged_in_client):
    resp = logged_in_client.get("/calls")
    assert resp.status_code == 200
    assert "Today's calls" in resp.text
    assert "No qualified deals with a phone number" in resp.text
    assert 'href="/calls"' in resp.text  # nav link


def test_calls_page_shows_ranked_row(logged_in_client):
    sid = create_service("Consulting", "consulting")
    pid = create_partner(
        "Jane Doe", phone="0400 111 222", email="jane@acme.example",
    )
    deal_id = create_deal(pid, sid, source="referral", value_estimate=8000, next_action="Call Jane")
    set_deal_stage(deal_id, "qualified")

    resp = logged_in_client.get("/calls")
    assert resp.status_code == 200
    assert "Jane Doe" in resp.text
    assert "0400 111 222" in resp.text
    assert f"/deals/{deal_id}/call-tel" in resp.text
    assert f"/deals/{deal_id}/call" in resp.text
    assert "Consulting" in resp.text
    assert "Call Jane" in resp.text
    assert f"/partners/{pid}" in resp.text
    assert "/100" in resp.text
    assert "How the score is calculated" not in resp.text
    assert "Value estimate" not in resp.text
    assert "Not a prediction" not in resp.text
    assert "the weights are listed below" not in resp.text
    assert "$8000 (≥ $5k)" not in resp.text
