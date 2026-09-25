import logging

from fastapi.testclient import TestClient

from app.services.auth import (
    authenticate_user,
    count_users,
    create_user,
    get_user_by_email,
    maybe_bootstrap_admin,
    verify_password,
)


def _set_bootstrap(monkeypatch, email="admin@example.com", password="longenough", name="Admin", password_file=None):
    if email is None:
        monkeypatch.delenv("BOOTSTRAP_ADMIN_EMAIL", raising=False)
    else:
        monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", email)
    if password is None:
        monkeypatch.delenv("BOOTSTRAP_ADMIN_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", password)
    if password_file is None:
        monkeypatch.delenv("BOOTSTRAP_ADMIN_PASSWORD_FILE", raising=False)
    else:
        monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD_FILE", password_file)
    if name is None:
        monkeypatch.delenv("BOOTSTRAP_ADMIN_NAME", raising=False)
    else:
        monkeypatch.setenv("BOOTSTRAP_ADMIN_NAME", name)


def test_empty_db_with_env_creates_one_user(db, monkeypatch, caplog):
    _set_bootstrap(monkeypatch)
    with caplog.at_level(logging.INFO, logger="app.services.auth"):
        maybe_bootstrap_admin()
    assert count_users() == 1
    user = get_user_by_email("admin@example.com")
    assert user is not None
    assert user["name"] == "Admin"
    assert verify_password("longenough", user["password_hash"])
    assert authenticate_user("admin@example.com", "longenough") is not None
    assert "redeploy" in caplog.text.lower()


def test_existing_user_skips_and_does_not_overwrite(db, monkeypatch, caplog):
    create_user("existing@example.com", "Existing", "original-password")
    _set_bootstrap(monkeypatch, email="second@example.com", password="another-password")
    with caplog.at_level(logging.WARNING, logger="app.services.auth"):
        maybe_bootstrap_admin()
    assert count_users() == 1
    assert get_user_by_email("second@example.com") is None
    existing = get_user_by_email("existing@example.com")
    assert verify_password("original-password", existing["password_hash"])
    assert "BOOTSTRAP_ADMIN_PASSWORD" in caplog.text
    assert "Delete" in caplog.text or "delete" in caplog.text.lower()
    assert "redeploy" in caplog.text.lower()


def test_unset_env_is_noop(db, monkeypatch):
    monkeypatch.delenv("BOOTSTRAP_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("BOOTSTRAP_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("BOOTSTRAP_ADMIN_PASSWORD_FILE", raising=False)
    monkeypatch.delenv("BOOTSTRAP_ADMIN_NAME", raising=False)
    maybe_bootstrap_admin()
    assert count_users() == 0


def test_empty_string_env_is_noop(db, monkeypatch):
    _set_bootstrap(monkeypatch, email="", password="", name="")
    maybe_bootstrap_admin()
    assert count_users() == 0


def test_short_password_is_skipped(db, monkeypatch, caplog):
    _set_bootstrap(monkeypatch, password="short")
    with caplog.at_level(logging.ERROR, logger="app.services.auth"):
        maybe_bootstrap_admin()
    assert count_users() == 0
    assert "at least 8" in caplog.text


def test_invalid_email_is_skipped(db, monkeypatch, caplog):
    _set_bootstrap(monkeypatch, email="not-an-email")
    with caplog.at_level(logging.ERROR, logger="app.services.auth"):
        maybe_bootstrap_admin()
    assert count_users() == 0
    assert "valid email" in caplog.text


def test_partial_env_is_skipped(db, monkeypatch, caplog):
    _set_bootstrap(monkeypatch, email="admin@example.com", password=None)
    with caplog.at_level(logging.ERROR, logger="app.services.auth"):
        maybe_bootstrap_admin()
    assert count_users() == 0

    _set_bootstrap(monkeypatch, email=None, password="longenough", name=None)
    with caplog.at_level(logging.ERROR, logger="app.services.auth"):
        maybe_bootstrap_admin()
    assert count_users() == 0


def test_optional_name_defaults_to_none(db, monkeypatch):
    _set_bootstrap(monkeypatch, name=None)
    maybe_bootstrap_admin()
    user = get_user_by_email("admin@example.com")
    assert user is not None
    assert user["name"] is None


def test_lifespan_creates_user_from_env(db, monkeypatch):
    _set_bootstrap(monkeypatch)
    from app.main import create_app
    with TestClient(create_app()) as client:
        assert count_users() == 1
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


def test_misconfigured_bootstrap_does_not_500_health_or_login(db, monkeypatch):
    _set_bootstrap(monkeypatch, email="bad", password="no")
    from app.main import create_app
    with TestClient(create_app()) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}
        login = client.get("/auth/login")
        assert login.status_code == 200
        assert count_users() == 0


def test_partial_bootstrap_does_not_500_health_or_login(db, monkeypatch):
    monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.delenv("BOOTSTRAP_ADMIN_PASSWORD", raising=False)
    from app.main import create_app
    with TestClient(create_app()) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/auth/login").status_code == 200
        assert count_users() == 0


def test_password_file_creates_one_user(db, monkeypatch, tmp_path, caplog):
    secret = tmp_path / "admin.secret"
    secret.write_text("file-password\n", encoding="utf-8")
    _set_bootstrap(monkeypatch, password=None, password_file=str(secret))
    with caplog.at_level(logging.INFO, logger="app.services.auth"):
        maybe_bootstrap_admin()
    assert count_users() == 1
    user = get_user_by_email("admin@example.com")
    assert verify_password("file-password", user["password_hash"])
    assert authenticate_user("admin@example.com", "file-password") is not None
    assert "BOOTSTRAP_ADMIN_PASSWORD_FILE" in caplog.text


def test_password_file_wins_over_env_password(db, monkeypatch, tmp_path):
    secret = tmp_path / "admin.secret"
    secret.write_text("from-file-wins", encoding="utf-8")
    _set_bootstrap(
        monkeypatch,
        password="from-env-ignored",
        password_file=str(secret),
    )
    maybe_bootstrap_admin()
    user = get_user_by_email("admin@example.com")
    assert verify_password("from-file-wins", user["password_hash"])
    assert authenticate_user("admin@example.com", "from-env-ignored") is None


def test_unreadable_password_file_does_not_fall_back_to_env(db, monkeypatch, tmp_path, caplog):
    secret = tmp_path / "missing.secret"
    _set_bootstrap(
        monkeypatch,
        password="env-fallback",
        password_file=str(secret),
    )
    with caplog.at_level(logging.ERROR, logger="app.services.auth"):
        maybe_bootstrap_admin()
    assert count_users() == 0
    assert "BOOTSTRAP_ADMIN_PASSWORD_FILE" in caplog.text


def test_bootstrapped_user_can_log_in_over_http(db, monkeypatch):
    _set_bootstrap(monkeypatch)
    from app.main import create_app
    from app.services.auth import generate_csrf_token
    with TestClient(create_app()) as client:
        resp = client.post(
            "/auth/login",
            data={
                "email": "admin@example.com",
                "password": "longenough",
                "csrf_token": generate_csrf_token(),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/partners"
