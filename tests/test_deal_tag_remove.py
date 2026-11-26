"""The x on a deal's tag pill, and the warning colour on missing:* pills."""
import re
from pathlib import Path

from app.routers.admin import _PALETTE_SLOTS, _contrast_ratio
from app.services.auth import generate_csrf_token
from app.services.catalog import create_service
from app.services.deal_tags import apply_deal_tags, tags_for_deal
from app.services.deals import create_deal
from app.services.partners import create_partner

CSS = (Path(__file__).resolve().parents[1] / "app" / "static" / "style.css").read_text()


def _deal(tags=("missing:bond", "priority:p1")):
    pid = create_partner("Jane Doe", email="jane@acme.example")
    sid = create_service("Enrolment", "enrolment")
    deal_id = create_deal(pid, sid, stage="new")
    apply_deal_tags(deal_id, add=list(tags))
    return deal_id


def _remove(client, deal_id, tag="missing:bond", csrf=None, **kw):
    data = {"tag": tag}
    if csrf is not False:
        data["csrf_token"] = csrf or generate_csrf_token(client.cookies.get("session"))
    return client.post(f"/crm/deals/{deal_id}/tags/remove", data=data,
                       follow_redirects=False, **kw)


def test_htmx_remove_drops_only_that_tag(logged_in_client, db):
    deal_id = _deal()
    resp = _remove(logged_in_client, deal_id, headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert tags_for_deal(deal_id) == ["priority:p1"]


def test_plain_post_redirects_back_to_a_local_page(logged_in_client, db):
    deal_id = _deal()
    resp = _remove(logged_in_client, deal_id,
                   headers={"Referer": "http://testserver/crm/pipeline?tag=missing:bond"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/crm/pipeline?tag=missing:bond"
    assert tags_for_deal(deal_id) == ["priority:p1"]


def test_requires_csrf(logged_in_client, db):
    deal_id = _deal()
    assert _remove(logged_in_client, deal_id, csrf=False).status_code == 422
    assert _remove(logged_in_client, deal_id, csrf="not-a-token").status_code == 403
    assert "missing:bond" in tags_for_deal(deal_id)


def test_requires_login(client, db):
    deal_id = _deal()
    resp = _remove(client, deal_id, csrf="x")
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/auth/login")
    assert "missing:bond" in tags_for_deal(deal_id)


def test_unknown_deal_404_and_invalid_tag_400(logged_in_client, db):
    assert _remove(logged_in_client, 99999).status_code == 404
    deal_id = _deal()
    assert _remove(logged_in_client, deal_id, tag="Not A Tag!").status_code == 400


def test_pills_render_remove_button_and_warning_class(logged_in_client, db):
    deal_id = _deal()
    for url in ("/crm/pipeline", "/crm/deals"):
        html = logged_in_client.get(url).text
        assert 'aria-label="Remove tag missing:bond"' in html, url
        assert 'aria-label="Remove tag priority:p1"' in html, url
        assert f'hx-post="/crm/deals/{deal_id}/tags/remove"' in html, url
        assert re.search(r'class="pill pill-tag deal-tag pill-tag-missing"[^>]*>\s*<a href="/crm/deals\?tag=missing:bond', html), url
        assert 'class="pill pill-tag deal-tag">' in html, url  # priority:p1 is not a warning
        assert '<button type="submit" class="tag-remove"' in html, url


def test_missing_pill_uses_palette_yellow_and_meets_aa():
    yellow = next(s for s in _PALETTE_SLOTS if s["name"] == "yellow")
    bgs = re.findall(r"--pill-missing-bg:\s*(#[0-9a-fA-F]{3,6})", CSS)
    fgs = re.findall(r"--pill-missing-fg:\s*(#[0-9a-fA-F]{3,6})", CSS)
    # :root (light), prefers-color-scheme dark, [data-theme="dark"]
    assert bgs == [yellow["light"], yellow["dark"], yellow["dark"]]
    assert len(fgs) == 3
    for bg, fg in zip(bgs, fgs):
        fg = "#000000" if fg == "#000" else fg
        assert _contrast_ratio(bg, fg) >= 4.5, (bg, fg)


def test_remove_button_has_24px_target():
    body = re.search(r"\.tag-remove \{([^}]*)\}", CSS).group(1)
    assert "height: 24px" in body and "width: 24px" in body
