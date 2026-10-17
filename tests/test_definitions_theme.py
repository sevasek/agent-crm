"""The /definitions/* magenta theme must use the house palette's actual
"magenta" slot (app/routers/admin.py's _PALETTE_SLOTS) and nothing
invented, and must override --link the same way #145 fixed body.crm-theme
to (otherwise .btn-secondary's text stays brand-blue on a magenta button).
"""
import re
from pathlib import Path

import pytest

from app.routers.admin import _PALETTE_SLOTS

CSS_PATH = Path(__file__).resolve().parents[1] / "app" / "static" / "style.css"
_MAGENTA = next(slot for slot in _PALETTE_SLOTS if slot["name"] == "magenta")
LIGHT, DARK = _MAGENTA["light"], _MAGENTA["dark"]


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
    """If this fails, someone reassigned the Definitions tile's palette slot —
    update the magenta literals throughout style.css in the same change."""
    assert LIGHT == "#e87ba4" and DARK == "#d55181"


def test_nav_app_definitions_label_is_bold_and_the_palettes_magenta(css):
    blocks = _blocks(css, ".topnav .nav-app-definitions")
    assert len(blocks) == 3  # base + prefers-color-scheme dark + [data-theme=dark]
    assert "font-weight: 700" in blocks[0]
    for block in blocks:
        hexes = re.findall(r"#[0-9a-fA-F]{6}", block)
        assert hexes, "expected a color in every .nav-app-definitions variant"
        assert set(h.lower() for h in hexes) <= {LIGHT, DARK}


def test_definitions_theme_tokens_use_only_the_palettes_magenta(css):
    blocks = _blocks(css, "body.definitions-theme")
    assert len(blocks) == 3  # base + prefers-color-scheme dark + [data-theme=dark]
    for block in blocks:
        for hex_color in re.findall(r"#[0-9a-fA-F]{6}", block):
            assert hex_color.lower() in {LIGHT, DARK}, (
                f"{hex_color} in body.definitions-theme isn't the palette's magenta"
            )
        for r, g, b in re.findall(r"rgba\((\d+),\s*(\d+),\s*(\d+)", block):
            assert (int(r), int(g), int(b)) in {_hex_to_rgb(LIGHT), _hex_to_rgb(DARK)}


def test_definitions_theme_overrides_link_not_just_accent(css):
    """--link drives .btn-secondary's text, tag-chip/tel-link colors, and
    topnav hover — without this override those stayed brand-blue even
    though .btn-secondary's border (--accent) went magenta."""
    base_block = _blocks(css, "body.definitions-theme")[0]
    assert re.search(r"--link:\s*" + re.escape(LIGHT), base_block, re.IGNORECASE)
