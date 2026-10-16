from pathlib import Path

from app.services.catalog import create_service
from app.services.deals import create_deal
from app.services.partners import create_partner


VENDOR_DIR = Path(__file__).resolve().parents[1] / "app" / "static" / "vendor"


def test_tom_select_script_is_vendored_and_linked(client):
    resp = client.get("/auth/login")
    assert resp.status_code == 200
    assert 'src="/static/vendor/tom-select.min.js"' in resp.text
    assert 'href="/static/vendor/tom-select.min.css"' in resp.text
    js = client.get("/static/vendor/tom-select.min.js")
    assert js.status_code == 200
    assert js.text.lstrip().startswith("/**")
    assert "Tom Select v2.6.2" in js.text[:200]
    assert "<!DOCTYPE" not in js.text[:80]
    css = client.get("/static/vendor/tom-select.min.css")
    assert css.status_code == 200
    assert css.text.lstrip().startswith(".ts-control")
    assert "<!DOCTYPE" not in css.text[:80]


def test_vendor_md_pins_tom_select_version():
    vendor_md = (VENDOR_DIR / "VENDOR.md").read_text()
    assert "tom-select.min.js" in vendor_md
    assert "tom-select.min.css" in vendor_md
    assert "2.6.2" in vendor_md
    assert (VENDOR_DIR / "tom-select.min.js").is_file()
    assert (VENDOR_DIR / "tom-select.min.css").is_file()
    assert (VENDOR_DIR / "tom-select.min.js").stat().st_size > 10_000


def test_new_deal_form_keeps_native_partner_select(logged_in_client, db):
    create_partner("Jane Doe", email="jane@acme.example")
    create_service("Consulting", "consulting")
    resp = logged_in_client.get("/deals/new")
    assert resp.status_code == 200
    html = resp.text
    assert 'id="partner_id"' in html
    assert 'name="partner_id"' in html
    assert '<select id="partner_id" name="partner_id" required>' in html
    assert '<select id="service_id" name="service_id" required>' in html
    assert 'id="parent_deal_id"' in html
    assert 'src="/static/vendor/tom-select.min.js"' in html
    assert 'src="/static/app.js"' in html


def test_edit_deal_form_has_no_partner_select(logged_in_client, db):
    pid = create_partner("Jane Doe", email="jane@acme.example")
    sid = create_service("Consulting", "consulting")
    deal_id = create_deal(pid, sid)
    resp = logged_in_client.get(f"/deals/{deal_id}/edit")
    assert resp.status_code == 200
    html = resp.text
    assert 'name="partner_id"' not in html
    assert 'id="partner_id"' not in html
    assert 'name="service_id"' not in html


def test_tom_dropdown_follows_pico_light_and_dark_colors():
    """The dropdown must not hardcode white: in dark mode the light-gray
    inherited text would sit on it at ~2:1 contrast, and the search input's
    UA field text goes invisible."""
    css = (Path(__file__).resolve().parents[1] / "app" / "static" / "style.css").read_text()
    tom_block = css[css.index("/* Tom Select is only initialised"):]
    assert "background: #fff;" not in tom_block
    # Anchor on the background declaration: ".ts-dropdown {" alone also matches
    # the font/color group rule above it, which has no background at all.
    for selector in (".ts-dropdown {\n    background:", ".ts-dropdown .dropdown-input {"):
        start = tom_block.index(selector)
        rule = tom_block[start:tom_block.index("}", start)]
        assert "pico-form-element-background-color" in rule, selector
    start = tom_block.index(".ts-dropdown .dropdown-input {")
    rule = tom_block[start:tom_block.index("}", start)]
    assert "color:" in rule


def test_app_js_inits_tom_select_only_on_partner_id(client):
    js = client.get("/static/app.js")
    assert js.status_code == 200
    body = js.text
    assert "TomSelect" in body
    assert 'getElementById("partner_id")' in body
    init = body.split("typeof TomSelect")[1]
    assert "service_id" not in init
    assert "parent_deal_id" not in init
    assert "parent_id" not in init
