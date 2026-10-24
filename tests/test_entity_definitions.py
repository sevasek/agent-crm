"""Every entity page has a plain-language definition in docs/WELCOME.md and
links to it. The entity list comes from the admin routes, not a hand-kept
list: each parameterless GET page (other than a /new form) is one entity,
named by its last path segment, which must match a WELCOME.md heading slug."""
import re
from pathlib import Path

from app.routers import admin

WELCOME = Path(__file__).resolve().parent.parent / "docs" / "WELCOME.md"

# entity slug -> why it has no definition. Empty today; add a reason per entry.
EXEMPT = {}


def _slug(heading):
    # Same slugging as generate-help-index.js / GitHub heading anchors.
    return re.sub(r"\s+", "-", re.sub(r"[^\w\s-]", "", heading.lower()).strip())


def entity_pages():
    pages = {}
    routes = admin.router.routes + admin.crm_router.routes + admin.actions_router.routes
    for route in routes:
        path = route.path
        if "GET" not in route.methods or "{" in path or path.endswith("/new"):
            continue
        pages[path.rstrip("/").rsplit("/", 1)[-1]] = path
    return pages


def test_entity_list_is_derived_from_routes():
    pages = entity_pages()
    # Guards the derivation itself: if the filter broke and matched nothing,
    # the definition test below would pass vacuously.
    assert {"deals", "leads", "partners", "campaigns", "lost-reasons"} <= set(pages)


def test_every_entity_has_a_definition_and_help_link(logged_in_client):
    headings = {_slug(h) for h in re.findall(r"^## (.+)$", WELCOME.read_text(), re.M)}
    missing = []
    for entity, path in sorted(entity_pages().items()):
        if entity in EXEMPT:
            continue
        if entity not in headings:
            missing.append(f"{entity}: no '## ' section in docs/WELCOME.md")
        response = logged_in_client.get(path)
        assert response.status_code == 200, f"{entity}: GET {path} returned {response.status_code}"
        if f"WELCOME.md#{entity}" not in response.text:
            missing.append(f"{entity}: {path} has no help_url('{entity}') link")
    assert not missing, "Entities missing a definition:\n" + "\n".join(missing)
