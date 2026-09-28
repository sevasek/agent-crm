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
    assert 'data-theme="light"' in html
    head = html.split("</head>", 1)[0]
    assert "http://" not in head
    assert "https://" not in head


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
