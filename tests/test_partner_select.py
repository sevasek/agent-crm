"""Deal-form partner picker: Tom Select assets on #partner_id only.

The native select remains the posted control. Other selects are untouched.
"""
from pathlib import Path

from app.services.auth import generate_csrf_token
from app.services.catalog import create_service
from app.services.deals import create_deal, get_deal, list_deals
from app.services.partners import create_partner

VENDOR_DIR = Path(__file__).resolve().parents[1] / "app" / "static" / "vendor"
STATIC_DIR = Path(__file__).resolve().parents[1] / "app" / "static"


def _csrf(client):
    return generate_csrf_token(client.cookies.get("session"))


def test_new_deal_form_loads_tom_select_for_partner_only(logged_in_client, db):
    jane = create_partner("Jane Doe", email="jane@acme.example")
    create_partner("Other Co", email="other@acme.example")
    create_service("Consulting", "consulting")

    resp = logged_in_client.get(f"/deals/new?partner_id={jane}")
    assert resp.status_code == 200
    html = resp.text
    assert 'href="/static/vendor/tom-select.min.css"' in html
    assert 'href="/static/partner-select.css"' in html
    assert 'src="/static/vendor/tom-select.complete.min.js"' in html
    assert 'src="/static/partner-select.js"' in html
    assert "https://" not in html
    assert "http://" not in html

    select = html.split('id="partner_id"', 1)[1].split(">", 1)[0]
    assert 'name="partner_id"' in select
    assert "required" in select
    assert f'<option value="{jane}" selected>' in html
    assert 'id="service_id"' in html
    assert 'name="service_id"' in html
    assert 'name="parent_deal_id"' in html
    # Assets are linked from the deal form; the initializer targets one id.
    js = (STATIC_DIR / "partner-select.js").read_text()
    assert 'getElementById("partner_id")' in js
    assert "service_id" not in js
    assert "parent_deal_id" not in js
    assert "parent_id" not in js


def test_tom_select_is_vendored_not_from_a_cdn():
    vendor_md = (VENDOR_DIR / "VENDOR.md").read_text()
    assert "tom-select.complete.min.js" in vendor_md
    assert "tom-select.min.css" in vendor_md
    assert "2.6.2" in vendor_md
    assert "2026-10-09" in vendor_md
    js = (VENDOR_DIR / "tom-select.complete.min.js").read_text()
    css = (VENDOR_DIR / "tom-select.min.css").read_text()
    assert "Tom Select v2.6.2" in js
    assert ".ts-wrapper" in css
    assert "cdn.jsdelivr.net" not in js
    assert "cdn.jsdelivr.net" not in css
    assert (VENDOR_DIR / "tom-select.complete.min.js").stat().st_size > 10_000
    served_js = STATIC_DIR / "partner-select.js"
    assert "new TomSelect" in served_js.read_text()


def test_tom_select_assets_are_served(logged_in_client):
    for path in (
        "/static/vendor/tom-select.complete.min.js",
        "/static/vendor/tom-select.min.css",
        "/static/partner-select.js",
        "/static/partner-select.css",
    ):
        resp = logged_in_client.get(path)
        assert resp.status_code == 200, path
        assert "<!DOCTYPE" not in resp.text[:200]


def test_other_pages_do_not_load_tom_select(logged_in_client, db):
    create_partner("Jane Doe", email="jane@acme.example")
    for path in ("/partners", "/partners/new", "/services", "/pipeline", "/offers"):
        resp = logged_in_client.get(path)
        assert resp.status_code == 200, path
        assert "tom-select" not in resp.text
        assert "partner-select" not in resp.text


def test_create_deal_post_with_partner_id(logged_in_client, db):
    pid = create_partner("Jane Doe", email="jane@acme.example")
    sid = create_service("Consulting", "consulting")
    resp = logged_in_client.post(
        "/deals/new",
        data={
            "csrf_token": _csrf(logged_in_client),
            "partner_id": str(pid),
            "service_id": str(sid),
            "source": "referral",
            "value_estimate": "1200",
            "pain_points": "slow follow-up",
            "goals": "a pipeline",
            "next_action": "Call",
            "next_action_date": "2026-10-15",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/partners/{pid}"
    deals = list_deals(partner_id=pid)
    assert len(deals) == 1
    assert deals[0]["partner_id"] == pid
    assert deals[0]["service_id"] == sid
    assert deals[0]["source"] == "referral"


def test_edit_deal_post_keeps_selected_partner(logged_in_client, db):
    pid = create_partner("Jane Doe", email="jane@acme.example")
    other = create_partner("Other Co", email="other@acme.example")
    sid = create_service("Consulting", "consulting")
    deal_id = create_deal(pid, sid, source="referral", pain_points="original")

    page = logged_in_client.get(f"/deals/{deal_id}/edit")
    assert page.status_code == 200
    assert "Jane Doe" in page.text
    assert 'name="partner_id"' not in page.text
    assert 'src="/static/vendor/tom-select.complete.min.js"' in page.text

    resp = logged_in_client.post(
        f"/deals/{deal_id}/edit",
        data={
            "csrf_token": _csrf(logged_in_client),
            "partner_id": str(pid),
            "source": "website",
            "value_estimate": "500",
            "pain_points": "updated",
            "goals": "",
            "next_action": "",
            "next_action_date": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/partners/{pid}"
    deal = get_deal(deal_id)
    assert deal["partner_id"] == pid
    assert deal["partner_id"] != other
    assert deal["source"] == "website"
    assert deal["pain_points"] == "updated"
    assert deal["value_estimate"] == 500
