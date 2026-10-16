from pathlib import Path


VENDOR_DIR = Path(__file__).resolve().parents[1] / "app" / "static" / "vendor"


def test_login_links_vendored_pico_before_app_css(client):
    resp = client.get("/auth/login")
    assert resp.status_code == 200
    html = resp.text
    pico = 'href="/static/vendor/pico.min.css"'
    app_css = 'href="/static/style.css"'
    assert pico in html
    assert app_css in html
    assert html.index(pico) < html.index(app_css)
    assert 'data-theme="light"' not in html
    assert "<html lang=\"en\">" in html
    head = html.split("</head>", 1)[0]
    assert "http://" not in head
    assert "https://" not in head


def test_style_css_pairs_custom_colors_for_dark_mode():
    css = (Path(__file__).resolve().parents[1] / "app" / "static" / "style.css").read_text()
    assert "--bg-alt:" in css
    assert '.pill-due { background: var(--pill-due-bg);' in css
    assert '.pill-overdue { background: var(--pill-overdue-bg);' in css
    assert "@media only screen and (prefers-color-scheme: dark)" in css
    assert ':root:not([data-theme])' in css
    assert '[data-theme="dark"]' in css
    assert "--accent: #2c5aa0;" in css
    assert ':root:not([data-theme="dark"])' in css
    assert "data-theme=\"light\" is locked" not in css


def test_pico_stylesheet_is_served_from_static(client):
    resp = client.get("/static/vendor/pico.min.css")
    assert resp.status_code == 200
    body = resp.text
    assert body.lstrip().startswith("@charset") or "Pico CSS" in body
    assert "<!DOCTYPE" not in body[:200]


def test_vendor_md_pins_pico_version():
    vendor_md = (VENDOR_DIR / "VENDOR.md").read_text()
    assert "pico.min.css" in vendor_md
    assert "2.1.1" in vendor_md
    assert (VENDOR_DIR / "pico.min.css").is_file()
    assert (VENDOR_DIR / "pico.min.css").stat().st_size > 50_000
