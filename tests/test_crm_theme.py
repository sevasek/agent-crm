"""Each section theme (/crm orange, /actions aqua, /definitions magenta)
must use the house palette's actual slot (app/routers/admin.py's
_PALETTE_SLOTS) and nothing invented. Any hex literal added to style.css
for these themes should be one of that slot's two values — if a future
edit picks a color by hand, this test catches it.
"""
import re
from pathlib import Path

import pytest

from app.routers.admin import _PALETTE_SLOTS

CSS_PATH = Path(__file__).resolve().parents[1] / "app" / "static" / "style.css"
_SLOTS = {slot["name"]: slot for slot in _PALETTE_SLOTS}

THEMES = [
    pytest.param("orange", ".topnav .nav-app-crm", "body.crm-theme", id="crm"),
    pytest.param("aqua", ".topnav .nav-app-actions", "body.actions-theme", id="actions"),
    pytest.param("magenta", ".topnav .nav-app-definitions", "body.definitions-theme", id="definitions"),
]


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    hex_color = hex_color.lstrip("#")
    return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))


def _blocks(css: str, selector: str) -> list[str]:
    """Every rule's declaration body whose selector text ends with `selector {`."""
    return [m.group(1) for m in re.finditer(re.escape(selector) + r"\s*\{([^}]*)\}", css)]


@pytest.fixture(scope="module")
def css() -> str:
    return CSS_PATH.read_text()


def test_palette_has_not_drifted():
    """If this fails, someone reassigned a tile's palette slot — update
    the matching color literals throughout style.css in the same change."""
    assert _SLOTS["orange"]["light"] == "#eb6834" and _SLOTS["orange"]["dark"] == "#d95926"
    assert _SLOTS["aqua"]["light"] == "#1baf7a" and _SLOTS["aqua"]["dark"] == "#199e70"
    assert _SLOTS["magenta"]["light"] == "#e87ba4" and _SLOTS["magenta"]["dark"] == "#d55181"


@pytest.mark.parametrize("slot_name, nav_selector, theme_selector", THEMES)
def test_nav_label_is_bold_and_the_palettes_color(css, slot_name, nav_selector, theme_selector):
    light, dark = _SLOTS[slot_name]["light"], _SLOTS[slot_name]["dark"]
    blocks = _blocks(css, nav_selector)
    assert len(blocks) == 3  # base + prefers-color-scheme dark + [data-theme=dark]
    assert "font-weight: 700" in blocks[0]
    for block in blocks:
        hexes = re.findall(r"#[0-9a-fA-F]{6}", block)
        assert hexes, f"expected a color in every {nav_selector} variant"
        assert set(h.lower() for h in hexes) <= {light, dark}


@pytest.mark.parametrize("slot_name, nav_selector, theme_selector", THEMES)
def test_theme_tokens_use_only_the_palettes_color(css, slot_name, nav_selector, theme_selector):
    light, dark = _SLOTS[slot_name]["light"], _SLOTS[slot_name]["dark"]
    blocks = _blocks(css, theme_selector)
    assert len(blocks) == 3  # base + prefers-color-scheme dark + [data-theme=dark]
    for block in blocks:
        for hex_color in re.findall(r"#[0-9a-fA-F]{6}", block):
            assert hex_color.lower() in {light, dark}, (
                f"{hex_color} in {theme_selector} isn't the palette's color"
            )
        for r, g, b in re.findall(r"rgba\((\d+),\s*(\d+),\s*(\d+)", block):
            assert (int(r), int(g), int(b)) in {_hex_to_rgb(light), _hex_to_rgb(dark)}


@pytest.mark.parametrize("slot_name, nav_selector, theme_selector", THEMES)
def test_theme_overrides_link_not_just_accent(css, slot_name, nav_selector, theme_selector):
    """--link drives .btn-secondary's text, tag-chip/tel-link colors, and
    topnav hover — without this override those stayed brand-blue even
    though .btn-secondary's border (--accent) picked up the theme color."""
    light = _SLOTS[slot_name]["light"]
    base_block = _blocks(css, theme_selector)[0]
    assert re.search(r"--link:\s*" + re.escape(light), base_block, re.IGNORECASE)
