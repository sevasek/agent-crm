from pathlib import Path

from app.services.auth import generate_csrf_token
from app.services.catalog import create_service
from app.services.deals import create_deal, get_deal, list_deals, mark_lost, set_deal_stage
from app.services.lead_records import create_lead
from app.services.partners import create_partner


VENDOR_DIR = Path(__file__).resolve().parents[1] / "app" / "static" / "vendor"


def _deal(stage="new", name="Jane Doe", slug=None):
    slug = slug or name.lower().replace(" ", "-")
    pid = create_partner(name, email=f"{slug}@acme.example")
    sid = create_service(f"Consulting {slug}", f"consulting-{slug}")
    return create_deal(pid, sid, stage=stage)


def _csrf(client):
    return generate_csrf_token(client.cookies.get("session"))


def test_sortable_script_is_vendored_and_linked(client):
    resp = client.get("/auth/login")
    assert resp.status_code == 200
    assert 'src="/static/vendor/Sortable.min.js"' in resp.text
    js = client.get("/static/vendor/Sortable.min.js")
    assert js.status_code == 200
    body = js.text
    assert body.lstrip().startswith("/*! Sortable")
    assert "<!DOCTYPE" not in body[:80]
    assert "function" in body
    vendor_md = (VENDOR_DIR / "VENDOR.md").read_text()
    assert "Sortable.min.js" in vendor_md
    assert "1.15.7" in vendor_md
    assert (VENDOR_DIR / "Sortable.min.js").stat().st_size > 10_000


def test_pipeline_board_has_sortable_lists(logged_in_client, db):
    deal_id = _deal()
    resp = logged_in_client.get("/pipeline")
    assert resp.status_code == 200
    html = resp.text
    assert 'class="pipeline-card-list"' in html
    assert f'data-deal-id="{deal_id}"' in html
    assert 'data-stage="new"' in html
    assert 'id="pipeline-col-new"' in html
    assert 'hx-post="' in html
    assert 'hx-swap="none"' in html
    assert "pipeline-empty" in html


def test_drop_post_moves_deal_and_returns_both_columns(logged_in_client, db):
    deal_id = _deal()
    resp = logged_in_client.post(
        f"/deals/{deal_id}/stage",
        data={
            "stage": "contacted",
            "next": "pipeline",
            "position": "0",
            "csrf_token": _csrf(logged_in_client),
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
    deal = get_deal(deal_id)
    assert deal["stage"] == "contacted"
    assert deal["board_position"] == 0


def test_drop_post_requires_csrf(logged_in_client, db):
    deal_id = _deal()
    missing = logged_in_client.post(
        f"/deals/{deal_id}/stage",
        data={"stage": "contacted", "next": "pipeline", "position": "0"},
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert missing.status_code == 422
    assert get_deal(deal_id)["stage"] == "new"

    bad = logged_in_client.post(
        f"/deals/{deal_id}/stage",
        data={
            "stage": "contacted",
            "next": "pipeline",
            "position": "0",
            "csrf_token": "not-a-token",
        },
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert get_deal(deal_id)["stage"] == "new"
    assert get_deal(deal_id)["board_position"] == 0


def test_drop_persists_position_among_siblings(logged_in_client, db):
    first = _deal(stage="contacted", name="Alpha")
    second = _deal(stage="contacted", name="Bravo")
    incoming = _deal(stage="new", name="Charlie")
    assert [d["id"] for d in list_deals(stage="contacted", order="board")] == [
        first, second,
    ]

    resp = logged_in_client.post(
        f"/deals/{incoming}/stage",
        data={
            "stage": "contacted",
            "next": "pipeline",
            "position": "1",
            "csrf_token": _csrf(logged_in_client),
        },
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert get_deal(incoming)["stage"] == "contacted"
    assert [d["id"] for d in list_deals(stage="contacted", order="board")] == [
        first, incoming, second,
    ]
    assert [d["board_position"] for d in list_deals(stage="contacted", order="board")] == [
        0, 1, 2,
    ]


def test_same_column_reorder_does_not_log_stage_change(db):
    from app.services.activities import list_activities_for_deal

    first = _deal(stage="qualified", name="First")
    second = _deal(stage="qualified", name="Second")
    assert set_deal_stage(second, "qualified", position=0)
    assert [d["id"] for d in list_deals(stage="qualified", order="board")] == [
        second, first,
    ]
    bodies = [a["body"] or "" for a in list_activities_for_deal(second)]
    assert not any("Stage changed" in body for body in bodies)


def test_dropdown_without_position_appends(logged_in_client, db):
    first = _deal(stage="proposal", name="Keep Top")
    incoming = _deal(stage="new", name="Appended")
    resp = logged_in_client.post(
        f"/deals/{incoming}/stage",
        data={
            "stage": "proposal",
            "next": "pipeline",
            "csrf_token": _csrf(logged_in_client),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert [d["id"] for d in list_deals(stage="proposal", order="board")] == [
        first, incoming,
    ]


def test_reorder_ignores_hidden_lead_rows_in_same_stage(db):
    """A lead sharing the stage must not shift the visible opportunity order."""
    from app.database import get_db

    alpha = _deal(stage="new", name="Alpha")
    bravo = _deal(stage="new", name="Bravo")
    lead, _error = create_lead(company_name="Hidden Lead", email="hidden@lead.example")
    lead_id = lead["id"]
    # Interleave the lead between the two opportunities on the board.
    with get_db() as conn:
        conn.execute("UPDATE deals SET stage = 'new', board_position = 0 WHERE id = ?", (alpha,))
        conn.execute("UPDATE deals SET stage = 'new', board_position = 1 WHERE id = ?", (lead_id,))
        conn.execute("UPDATE deals SET stage = 'new', board_position = 2 WHERE id = ?", (bravo,))
        conn.commit()

    assert set_deal_stage(alpha, "new", position=1) is True  # drag Alpha below Bravo

    visible = [d["id"] for d in list_deals(stage="new", order="board")]
    assert visible == [bravo, alpha]
    with get_db() as conn:
        lead_pos = conn.execute(
            "SELECT board_position FROM deals WHERE id = ?", (lead_id,),
        ).fetchone()[0]
    assert lead_pos == 1  # untouched by the visible-card renumber


def test_lost_cards_are_view_only_and_not_draggable(logged_in_client, db):
    deal_id = _deal()
    mark_lost(deal_id, lost_reason="Not a fit")
    lost_board = logged_in_client.get("/pipeline?lost=1")
    assert "pipeline-card-lost" in lost_board.text
    open_board = logged_in_client.get("/pipeline")
    assert "pipeline-card-lost" not in open_board.text
    app_js = (Path(__file__).resolve().parents[1] / "app" / "static" / "app.js").read_text()
    assert '".pipeline-card:not(.pipeline-card-lost)"' in app_js
    # The drop's position input is cleared after each request so a later
    # dropdown change appends instead of reusing a stale index.
    assert "htmx:afterRequest" in app_js


def test_app_js_inits_sortable_without_inline_script():
    app_js = (Path(__file__).resolve().parents[1] / "app" / "static" / "app.js").read_text()
    assert "Sortable.create" in app_js
    assert "delayOnTouchOnly" in app_js
    assert "forceFallback" in app_js
    assert "htmx.trigger" in app_js
    base = (Path(__file__).resolve().parents[1] / "app" / "templates" / "base.html").read_text()
    assert 'src="/static/vendor/Sortable.min.js"' in base
    assert "unsafe-inline" not in base
