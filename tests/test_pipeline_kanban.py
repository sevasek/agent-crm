"""Drag-and-drop on the pipeline board posts the existing stage form."""

from pathlib import Path

from app.services.activities import list_activities_for_deal
from app.services.auth import generate_csrf_token
from app.services.catalog import create_service
from app.services.deals import STAGE_CHANGED_PREFIX, create_deal, get_deal
from app.services.partners import create_partner

VENDOR_DIR = Path(__file__).resolve().parents[1] / "app" / "static" / "vendor"


def _deal(stage="new"):
    pid = create_partner("Jane Doe", email="jane@acme.example")
    sid = create_service("Consulting", "consulting")
    return create_deal(pid, sid, stage=stage)


def _csrf(client):
    return generate_csrf_token(client.cookies.get("session"))


def _drop(client, deal_id, stage, csrf_token=None, omit_csrf=False):
    """POST the same fields a cross-column drop submits via the stage select."""
    data = {"stage": stage, "next": "pipeline"}
    if not omit_csrf:
        data["csrf_token"] = _csrf(client) if csrf_token is None else csrf_token
    return client.post(
        f"/deals/{deal_id}/stage",
        data=data,
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )


def _stage_changes(deal_id):
    return [
        a for a in list_activities_for_deal(deal_id)
        if (a["body"] or "").startswith(STAGE_CHANGED_PREFIX)
    ]


def test_pipeline_html_includes_sortable_and_stage_select(logged_in_client, db):
    _deal()
    resp = logged_in_client.get("/pipeline")
    assert resp.status_code == 200
    html = resp.text
    assert 'src="/static/vendor/sortable.min.js"' in html
    assert 'src="/static/pipeline-board.js"' in html
    assert 'href="/static/pipeline-board.css"' in html
    assert "cdn.jsdelivr.net" not in html
    assert 'name="stage"' in html
    assert 'hx-post="/deals/' in html
    assert "/stage" in html
    assert 'hx-swap="none"' in html
    assert 'hx-include="closest form"' in html
    assert 'onchange="if (!window.htmx) this.form.submit()"' in html
    assert 'name="csrf_token"' in html
    assert 'class="pipeline-cards"' in html
    assert 'data-stage="new"' in html
    assert "<noscript><button type=\"submit\">Update stage</button></noscript>" in html
    assert "Order within a column lasts only until the next refresh." in html

    other = logged_in_client.get("/deals")
    assert "sortable.min.js" not in other.text

    js = logged_in_client.get("/static/pipeline-board.js")
    assert js.status_code == 200
    script = js.text
    assert "TOUCH_DELAY_MS = 180" in script
    assert "delay: TOUCH_DELAY_MS" in script
    assert "delayOnTouchOnly: true" in script
    assert "TOUCH_START_THRESHOLD_PX = 8" in script
    assert "forceFallback: true" in script
    assert "evt.from === evt.to" in script
    assert 'new Event("change"' in script
    assert "not saved" in script

    sortable = logged_in_client.get("/static/vendor/sortable.min.js")
    assert sortable.status_code == 200
    body = sortable.text
    assert "Sortable 1.15.7" in body
    assert "<!DOCTYPE" not in body[:80]

    vendor_md = (VENDOR_DIR / "VENDOR.md").read_text()
    assert "sortable.min.js" in vendor_md
    assert "1.15.7" in vendor_md
    assert "2026-10-09" in vendor_md
    assert (VENDOR_DIR / "sortable.min.js").stat().st_size > 10_000


def test_drop_stage_post_moves_deal_and_rejects_bad_csrf(logged_in_client, db):
    deal_id = _deal()
    moved = _drop(logged_in_client, deal_id, "contacted")
    assert moved.status_code == 200
    assert "<html" not in moved.text.lower()
    assert 'id="pipeline-col-new"' in moved.text
    assert 'id="pipeline-col-contacted"' in moved.text
    assert "hx-swap-oob" in moved.text
    source_column, target_and_rest = moved.text.split('id="pipeline-col-contacted"', 1)
    assert "Jane Doe" not in source_column
    assert "Jane Doe" in target_and_rest
    assert get_deal(deal_id)["stage"] == "contacted"
    assert len(_stage_changes(deal_id)) == 1

    rejected = _drop(logged_in_client, deal_id, "qualified", csrf_token="not-a-token")
    assert rejected.status_code != 500
    assert rejected.status_code < 500
    assert get_deal(deal_id)["stage"] == "contacted"
    html = rejected.text
    assert 'id="pipeline-col-contacted"' in html
    assert 'id="pipeline-col-qualified"' in html
    before_qualified, after_qualified = html.split('id="pipeline-col-qualified"', 1)
    assert "Jane Doe" in before_qualified
    assert "Jane Doe" not in after_qualified
    assert len(_stage_changes(deal_id)) == 1

    missing = _drop(logged_in_client, deal_id, "qualified", omit_csrf=True)
    assert missing.status_code == 422
    assert missing.status_code != 500
    assert get_deal(deal_id)["stage"] == "contacted"
    assert len(_stage_changes(deal_id)) == 1


def test_drop_to_current_stage_does_not_duplicate_side_effects(logged_in_client, db, monkeypatch):
    enrolls = []
    wins = []
    automations = []

    def fake_enroll(*_args, **_kwargs):
        enrolls.append(1)
        return "skipped", "test"

    def fake_won(*_args, **_kwargs):
        wins.append(1)
        return "skipped", "test"

    def fake_automations(*_args, **_kwargs):
        automations.append(1)

    monkeypatch.setattr("app.services.deals.enroll_partner_in_nurture", fake_enroll)
    monkeypatch.setattr("app.services.deals.notify_deal_won", fake_won)
    monkeypatch.setattr("app.services.stage_automations.apply_stage_automations", fake_automations)

    deal_id = _deal()
    first = _drop(logged_in_client, deal_id, "nurture")
    assert first.status_code == 200
    assert get_deal(deal_id)["stage"] == "nurture"
    assert len(enrolls) == 1
    assert wins == []
    assert len(automations) == 1
    assert len(_stage_changes(deal_id)) == 1

    again = _drop(logged_in_client, deal_id, "nurture")
    assert again.status_code == 200
    assert get_deal(deal_id)["stage"] == "nurture"
    assert len(enrolls) == 1
    assert wins == []
    assert len(automations) == 1
    assert len(_stage_changes(deal_id)) == 1

    won = _drop(logged_in_client, deal_id, "won")
    assert won.status_code == 200
    assert get_deal(deal_id)["stage"] == "won"
    assert len(enrolls) == 1
    assert len(wins) == 1
    assert len(automations) == 2
    assert len(_stage_changes(deal_id)) == 2

    won_again = _drop(logged_in_client, deal_id, "won")
    assert won_again.status_code == 200
    assert get_deal(deal_id)["stage"] == "won"
    assert len(enrolls) == 1
    assert len(wins) == 1
    assert len(automations) == 2
    assert len(_stage_changes(deal_id)) == 2
