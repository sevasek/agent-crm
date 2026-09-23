import sqlite3

from app.database import db_timeout, get_db


def test_get_db_uses_sqlite_default_timeout(db, monkeypatch):
    captured = {}
    real = sqlite3.connect

    def fake(path, timeout=5.0, **kwargs):
        captured["timeout"] = timeout
        return real(path, timeout=timeout, **kwargs)

    monkeypatch.setattr("app.database.sqlite3.connect", fake)
    with get_db() as conn:
        conn.execute("SELECT 1")
    assert captured["timeout"] == 5.0


def test_db_timeout_scopes_busy_wait_to_nested_get_db(db, monkeypatch):
    captured = []
    real = sqlite3.connect

    def fake(path, timeout=5.0, **kwargs):
        captured.append(timeout)
        return real(path, timeout=timeout, **kwargs)

    monkeypatch.setattr("app.database.sqlite3.connect", fake)
    with get_db() as conn:
        conn.execute("SELECT 1")
    with db_timeout(30):
        with get_db() as conn:
            conn.execute("SELECT 1")
    with get_db() as conn:
        conn.execute("SELECT 1")
    assert captured == [5.0, 30, 5.0]


def test_get_db_explicit_timeout_overrides_context(db, monkeypatch):
    captured = {}
    real = sqlite3.connect

    def fake(path, timeout=5.0, **kwargs):
        captured["timeout"] = timeout
        return real(path, timeout=timeout, **kwargs)

    monkeypatch.setattr("app.database.sqlite3.connect", fake)
    with db_timeout(30):
        with get_db(timeout=5.0) as conn:
            conn.execute("SELECT 1")
    assert captured["timeout"] == 5.0
