import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VENDOR_DIR = ROOT / "app" / "static" / "vendor"
LIGHT_DARK = re.compile(
    r"--([a-z0-9-]+)\s*:\s*light-dark\(\s*"
    r"(#[0-9a-fA-F]{3,8}|rgb\([^)]*\))\s*,\s*"
    r"(#[0-9a-fA-F]{3,8}|rgb\([^)]*\))\s*\)"
)


def _parse_color(raw):
    raw = raw.strip()
    if raw.startswith("#"):
        hex_digits = raw[1:]
        if len(hex_digits) == 3:
            hex_digits = "".join(ch * 2 for ch in hex_digits)
        return tuple(int(hex_digits[i : i + 2], 16) for i in (0, 2, 4))
    match = re.fullmatch(r"rgb\(\s*([0-9.]+)\s*,\s*([0-9.]+)\s*,\s*([0-9.]+)\s*\)", raw)
    assert match, raw
    return tuple(float(part) for part in match.groups())


def _contrast(foreground, background):
    def channel(value):
        value = value / 255
        if value <= 0.04045:
            return value / 12.92
        return ((value + 0.055) / 1.055) ** 2.4

    def luminance(rgb):
        red, green, blue = (channel(part) for part in rgb)
        return 0.2126 * red + 0.7152 * green + 0.0722 * blue

    lighter = max(luminance(foreground), luminance(background))
    darker = min(luminance(foreground), luminance(background))
    return (lighter + 0.05) / (darker + 0.05)


def _theme_pairs():
    css = (ROOT / "app" / "static" / "style.css").read_text()
    return {name: (light, dark) for name, light, dark in LIGHT_DARK.findall(css)}


def test_login_links_vendored_pico_before_app_css(client):
    resp = client.get("/auth/login")
    assert resp.status_code == 200
    html = resp.text
    pico = 'href="/static/vendor/pico.min.css"'
    app_css = 'href="/static/style.css"'
    assert pico in html
    assert app_css in html
    assert html.index(pico) < html.index(app_css)
    html_tag = html[html.lower().find("<html"):html.find(">", html.lower().find("<html"))]
    assert "data-theme" not in html_tag
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


def test_html_does_not_lock_a_color_scheme():
    base = (ROOT / "app" / "templates" / "base.html").read_text()
    html_line = next(line for line in base.splitlines() if line.startswith("<html"))
    assert "data-theme" not in html_line


def test_style_css_dark_pairs_stay_readable():
    css = (ROOT / "app" / "static" / "style.css").read_text()
    assert "light-dark(" in css
    assert ":root:not([data-theme])" in css
    assert '[data-theme="dark"]' in css
    assert "--accent: #2c5aa0;" in css
    assert "--pico-primary-background: var(--accent);" in css

    pairs = _theme_pairs()
    assert pairs["bg-alt"][0] == "#f7f7f7"
    assert pairs["due-bg"][0] == "#fff3cd"
    assert pairs["due-text"][0] == "#856404"
    assert pairs["overdue-bg"][0] == "#fdecea"
    assert pairs["overdue-text"][0] == "#b00020"
    assert pairs["page"][0] == "#fff"
    assert pairs["text"][0] == "#222"
    assert pairs["surface"][0] == "#fff"
    assert pairs["due-bg"][1] != pairs["overdue-bg"][1]
    assert pairs["due-text"][1] != pairs["overdue-text"][1]

    text_pairs = (
        ("text", "page"),
        ("text", "surface"),
        ("text", "bg-alt"),
        ("text", "ok-bg"),
        ("muted", "page"),
        ("muted", "surface"),
        ("muted", "bg-alt"),
        ("accent-text", "page"),
        ("accent-text", "surface"),
        ("accent-text", "bg-alt"),
        ("accent-text", "tag-hover-bg"),
        ("accent-text", "secondary-hover"),
        ("danger", "page"),
        ("danger", "surface"),
        ("due-text", "due-bg"),
        ("overdue-text", "overdue-bg"),
        ("code-text", "bg-alt"),
    )
    for scheme in (0, 1):
        for foreground, background in text_pairs:
            ratio = _contrast(
                _parse_color(pairs[foreground][scheme]),
                _parse_color(pairs[background][scheme]),
            )
            assert ratio >= 4.5, f"{foreground} on {background} scheme {scheme} is {ratio:.2f}"

    for foreground, background in (("border", "page"), ("border", "surface"), ("border", "bg-alt")):
        ratio = _contrast(
            _parse_color(pairs[foreground][1]),
            _parse_color(pairs[background][1]),
        )
        assert ratio >= 3, f"{foreground} on {background} is {ratio:.2f}"

    # Filled brand and won buttons keep white text on a dark fill.
    assert _contrast(_parse_color("#fff"), _parse_color("#2c5aa0")) >= 4.5
    assert _contrast(_parse_color("#fff"), _parse_color("#1e7e34")) >= 4.5


def test_pipeline_and_call_view_markup_uses_themed_classes():
    column = (ROOT / "app" / "templates" / "admin" / "_pipeline_column.html").read_text()
    board = (ROOT / "app" / "templates" / "admin" / "pipeline.html").read_text()
    call = (ROOT / "app" / "templates" / "admin" / "_call_view_body.html").read_text()
    assert "pipeline-board" in board
    assert "pipeline-column" in column
    assert "pipeline-card" in column
    assert "pill-due" in column
    assert "pill-overdue" in column
    assert 'class="call-view"' in call
    assert "btn-tel-big" in call
    assert "call-view-talk-track" in call
    assert "btn-outcome" in call
