from app.services.auth import generate_csrf_token
from app.services.catalog import (
    _clean_nurture_list_slug,
    create_service,
    get_service,
    get_service_by_slug,
    update_service,
)


def test_new_service_rejects_path_nurture_slug(logged_in_client):
    resp = logged_in_client.post("/services/new", data={
        "name": "Bad",
        "slug": "bad-nurture",
        "description": "",
        "nurture_list_slug": "../admin",
        "csrf_token": generate_csrf_token(),
    })
    assert resp.status_code == 400
    assert "List slug" in resp.text
    assert get_service_by_slug("bad-nurture") is None


def test_edit_service_rejects_path_nurture_slug_and_keeps_existing(logged_in_client):
    sid = create_service(
        "Consulting", "consulting", nurture_list_slug="automation-interest",
    )
    resp = logged_in_client.post(f"/services/{sid}/edit", data={
        "name": "Consulting",
        "description": "",
        "nurture_list_slug": "../admin",
        "active": "1",
        "csrf_token": generate_csrf_token(),
    })
    assert resp.status_code == 400
    assert get_service(sid)["nurture_list_slug"] == "automation-interest"


def test_clean_nurture_list_slug_drops_path_segments():
    assert _clean_nurture_list_slug("../admin") is None
    assert _clean_nurture_list_slug("automation-interest") == "automation-interest"
    assert _clean_nurture_list_slug("") is None
    assert _clean_nurture_list_slug("a") is None  # is_valid_slug requires length >= 2


def test_update_service_clears_invalid_nurture_slug(db):
    """Bypassing the form check must not store a path-like slug; it clears."""
    sid = create_service(
        "Consulting", "consulting", nurture_list_slug="automation-interest",
    )
    update_service(sid, nurture_list_slug="../admin")
    assert get_service(sid)["nurture_list_slug"] is None


def test_update_catalog_service_slug_conflict_and_active(db):
    from app.services.catalog import update_catalog_service

    first = create_service("Consulting", "consulting")
    second = create_service("GEO", "geo")
    service, error = update_catalog_service(second, slug="consulting")
    assert error == "slug_conflict"
    assert service is None
    assert get_service(second)["slug"] == "geo"

    service, error = update_catalog_service(first, active=False, description="Updated")
    assert error is None
    assert service["active"] == 0
    assert service["description"] == "Updated"

    missing, error = update_catalog_service(999, name="Nope")
    assert error == "not_found"
    assert missing is None
