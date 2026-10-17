"""Every button-like control (native <button>, .btn anchors, and the
compact per-form variants) must share one height so they line up with
each other and with text inputs/selects in the same row. See the
--control-height comment in style.css for the box-model rationale.
"""
from pathlib import Path

import pytest


CSS_PATH = Path(__file__).resolve().parents[1] / "app" / "static" / "style.css"


def _block(css: str, selector: str) -> str:
    """Return the declaration body of the first rule whose selector text
    starts with `selector`, e.g. _block(css, ".btn-outcome {")."""
    start = css.index(selector)
    open_brace = css.index("{", start)
    close_brace = css.index("}", open_brace)
    return css[open_brace + 1:close_brace]


@pytest.fixture(scope="module")
def css() -> str:
    return CSS_PATH.read_text()


def test_control_height_token_is_defined_from_pico_vars(css):
    assert "--control-height: calc(" in css
    assert "--pico-form-element-spacing-vertical" in css
    assert "--pico-line-height" in css
    assert "--pico-border-width" in css


@pytest.mark.parametrize("selector", [
    "button { ",
    ".btn, .btn-secondary {",
    ".btn-outcome {",
])
def test_standard_buttons_use_the_shared_control_height(css, selector):
    body = _block(css, selector)
    assert "height: var(--control-height)" in body
    assert "box-sizing: border-box" in body


@pytest.mark.parametrize("selector", [
    ".next-action-form button {",
    ".stage-move-form button {",
])
def test_per_form_button_variants_no_longer_shrink_height(css, selector):
    """These used to set their own smaller vertical padding (e.g. `padding:
    0.35rem 0.75rem`), making the button shorter than the text input/select
    sitting next to it in the same form row. They must not set height or
    vertical padding anymore, so they inherit the native <button> height."""
    body = _block(css, selector)
    assert "height:" not in body
    assert "padding-top:" not in body
    assert "padding-bottom:" not in body
    # a bare `padding:` shorthand would reintroduce a vertical value
    assert "padding:" not in body


@pytest.mark.parametrize("selector", [
    ".topnav-logout button {",
    ".btn-tel-big {",
])
def test_intentionally_different_controls_opt_out_of_shared_height(css, selector):
    """The logout control is a plain inline text button (sits among nav
    links, not form controls) and .btn-tel-big is a deliberately oversized
    tap target — both must explicitly opt out rather than silently inherit
    --control-height from the .btn/button rules they also match."""
    body = _block(css, selector)
    assert "height: auto" in body


def test_btn_tel_big_also_matches_the_btn_rule(css):
    """Guards the scenario the opt-out above exists for: .btn-tel-big is
    applied together with the .btn class in templates, so it genuinely
    needs its own override, not just a coincidentally-passing test."""
    html = (
        Path(__file__).resolve().parents[1]
        / "app" / "templates" / "admin" / "_call_view_body.html"
    ).read_text()
    assert 'class="btn btn-tel btn-tel-big"' in html
