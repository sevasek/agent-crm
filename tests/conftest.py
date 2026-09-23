import os

os.environ.setdefault("SECRET_KEY", "test-secret-key")
# Docs disable at create_app() import time. A sourced prod .env or CI env
# with BASE_URL=https / SECURE_COOKIES=true must not freeze /docs off for
# the whole session — same isolation as SECRET_KEY above.
os.environ.pop("SECURE_COOKIES", None)
os.environ.pop("BASE_URL", None)
# Bootstrap admin must not leak from a sourced production .env into every test DB.
os.environ.pop("BOOTSTRAP_ADMIN_EMAIL", None)
os.environ.pop("BOOTSTRAP_ADMIN_PASSWORD", None)
os.environ.pop("BOOTSTRAP_ADMIN_NAME", None)
# Bot MCP is fail-closed unless a test sets CRM_MCP_API_KEY.
os.environ.pop("CRM_MCP_API_KEY", None)
os.environ.pop("TASK_WEBHOOK_URL", None)
os.environ.pop("TASK_WEBHOOK_TOKEN", None)

import pytest
from fastapi.testclient import TestClient

from app import database


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Point the app's SQLite connection at a fresh, isolated file per test."""
    monkeypatch.delenv("SECURE_COOKIES", raising=False)
    monkeypatch.delenv("BASE_URL", raising=False)
    monkeypatch.delenv("BOOTSTRAP_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("BOOTSTRAP_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("BOOTSTRAP_ADMIN_NAME", raising=False)
    monkeypatch.delenv("CRM_MCP_API_KEY", raising=False)
    monkeypatch.delenv("TASK_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("TASK_WEBHOOK_TOKEN", raising=False)
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()
    return database


@pytest.fixture()
def client(db, monkeypatch):
    """FastAPI TestClient wired to the isolated per-test DB, with rate limits reset.

    Builds a fresh app so /docs follows this test's env, not a singleton
    created under a leftover BASE_URL / SECURE_COOKIES.
    """
    from app.services import auth as auth_service
    auth_service.clear_rate_limits()
    from app.main import create_app
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture()
def logged_in_client(client):
    """A TestClient with an authenticated session cookie already set."""
    from app.services.auth import create_user
    from app.routers.auth import cookie_signer

    user_id = create_user("test@example.com", "Test User", "password123")
    token = cookie_signer.dumps({"user_id": user_id})
    client.cookies.set("session", token)
    return client
