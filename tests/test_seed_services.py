from sqlite3 import OperationalError

from app.database import IntegrityConflict
from app.services.catalog import get_or_create_service, get_service_by_slug, list_services
from scripts.seed_services import SEED_SERVICES, main, seed_services


def test_first_seed_creates_rows(db):
    results = seed_services()
    assert all(created for _, created in results)
    assert {s["slug"] for s, _ in results} == {spec["slug"] for spec in SEED_SERVICES}
    assert {s["name"] for s, _ in results} == {spec["name"] for spec in SEED_SERVICES}
    assert len(list_services()) == len(SEED_SERVICES)


def test_second_seed_is_noop(db):
    first = seed_services()
    second = seed_services()
    assert all(created for _, created in first)
    assert all(not created for _, created in second)
    assert [s["id"] for s, _ in first] == [s["id"] for s, _ in second]
    assert len(list_services()) == len(SEED_SERVICES)


def test_slugs_resolve_via_get_service_by_slug(db):
    seed_services()
    consulting = get_service_by_slug("consulting")
    rebuild = get_service_by_slug("website-rebuild")
    retainer = get_service_by_slug("monthly-retainer")
    assert consulting["name"] == "Consulting"
    assert rebuild["name"] == "Website Rebuild"
    assert retainer["name"] == "Monthly Retainer"


def test_slug_from_name():
    from app.services.catalog import slug_from_name

    assert slug_from_name("AI Concierge") == "ai-concierge"
    assert slug_from_name("IT Support") == "it-support"
    assert slug_from_name("Monthly Retainer") == "monthly-retainer"
    assert slug_from_name("x") is None


def test_get_or_create_service_does_not_duplicate_slug(db):
    service, created = get_or_create_service("Consulting", "consulting")
    assert created is True
    again, created_again = get_or_create_service("Consulting", "consulting")
    assert created_again is False
    assert again["id"] == service["id"]
    assert len(list_services()) == 1


def test_get_or_create_integrity_error_rereads(db, monkeypatch):
    first, created = get_or_create_service("Consulting", "consulting")
    assert created is True

    from app.services import catalog
    calls = {"n": 0}
    real_get = catalog.get_service_by_slug

    def fake_get(slug):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return real_get(slug)

    monkeypatch.setattr(catalog, "get_service_by_slug", fake_get)

    def boom(*args, **kwargs):
        raise IntegrityConflict("UNIQUE constraint failed: services.slug")

    monkeypatch.setattr(catalog, "create_service", boom)
    service, created_again = get_or_create_service("Consulting", "consulting")
    assert created_again is False
    assert service["id"] == first["id"]


def test_get_or_create_integrity_error_empty_reread_raises(db, monkeypatch):
    from app.services import catalog

    monkeypatch.setattr(catalog, "get_service_by_slug", lambda slug: None)

    def boom(*args, **kwargs):
        raise IntegrityConflict("UNIQUE constraint failed: services.slug")

    monkeypatch.setattr(catalog, "create_service", boom)
    try:
        get_or_create_service("Consulting", "consulting")
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "could not be re-read" in str(exc)


def test_get_or_create_retries_lock_then_succeeds(db, monkeypatch):
    from app.services import catalog

    calls = {"n": 0}
    real_create = catalog.create_service

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("database is locked")
        return real_create(*args, **kwargs)

    monkeypatch.setattr(catalog, "create_service", flaky)
    monkeypatch.setattr(catalog.time, "sleep", lambda _s: None)
    service, created = get_or_create_service("Consulting", "consulting")
    assert created is True
    assert service["slug"] == "consulting"
    assert calls["n"] == 2


def test_get_or_create_retries_lock_on_lookup(db, monkeypatch):
    from app.services import catalog

    calls = {"n": 0}
    real_get = catalog.get_service_by_slug

    def flaky(slug):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("database is locked")
        return real_get(slug)

    monkeypatch.setattr(catalog, "get_service_by_slug", flaky)
    monkeypatch.setattr(catalog.time, "sleep", lambda _s: None)
    service, created = get_or_create_service("Consulting", "consulting")
    assert created is True
    assert service["slug"] == "consulting"
    assert calls["n"] == 2


def test_get_or_create_retries_lock_on_reread(db, monkeypatch):
    from app.services import catalog

    calls = {"n": 0}
    real_get = catalog.get_service

    def flaky(service_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("database is locked")
        return real_get(service_id)

    monkeypatch.setattr(catalog, "get_service", flaky)
    monkeypatch.setattr(catalog.time, "sleep", lambda _s: None)
    service, created = get_or_create_service("Consulting", "consulting")
    # Insert succeeded; retry sees the row via slug lookup and reports existing.
    assert created is False
    assert service["slug"] == "consulting"
    assert len(list_services()) == 1
    assert calls["n"] == 1


def test_cli_prints_created_then_existing(db, capsys):
    assert main() == 0
    first = capsys.readouterr().out
    assert "created: consulting (Consulting)" in first
    assert "created: website-rebuild (Website Rebuild)" in first
    assert "created: monthly-retainer (Monthly Retainer)" in first
    assert f"{len(SEED_SERVICES)} created, 0 already existed." in first

    assert main() == 0
    second = capsys.readouterr().out
    assert "existing: consulting (Consulting)" in second
    assert "existing: website-rebuild (Website Rebuild)" in second
    assert "existing: monthly-retainer (Monthly Retainer)" in second
    assert f"0 created, {len(SEED_SERVICES)} already existed." in second


def test_cli_unrecoverable_error_exits_one(db, monkeypatch, capsys):
    from scripts import seed_services as seed_mod

    def boom():
        raise RuntimeError("created service 1 could not be re-read")

    monkeypatch.setattr(seed_mod, "seed_services", boom)
    assert main() == 1
    err = capsys.readouterr().err
    assert "error: created service 1 could not be re-read" in err
    assert "Traceback" not in err
