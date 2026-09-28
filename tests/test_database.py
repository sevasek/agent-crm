import sqlite3
import threading

import pytest

from app.database import (
    MIGRATIONS,
    SCHEMA_VERSION,
    SchemaVersionError,
    db_timeout,
    get_db,
    get_user_version,
    init_db,
    migrate_001,
    migrate_002,
    migrate_003,
)


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


def test_init_db_sets_user_version(db):
    with get_db() as conn:
        assert get_user_version(conn) == SCHEMA_VERSION


def test_schema_version_is_only_applied_via_numbered_migration():
    """Future columns must be a new migrate_00N + SCHEMA_VERSION bump."""
    assert SCHEMA_VERSION == 3
    assert set(MIGRATIONS) == {1, 2, 3}
    assert MIGRATIONS[1] is migrate_001
    assert MIGRATIONS[2] is migrate_002
    assert MIGRATIONS[3] is migrate_003


def test_init_db_refuses_newer_schema(tmp_path, monkeypatch):
    path = tmp_path / "newer.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
    conn.execute("PRAGMA user_version = 999")
    conn.commit()
    conn.close()
    monkeypatch.setattr("app.database.DB_PATH", str(path))
    with pytest.raises(SchemaVersionError, match="newer than this code"):
        init_db()
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 999
    conn.close()


def test_init_db_migrates_legacy_version_0(tmp_path, monkeypatch):
    path = tmp_path / "data" / "crm.db"
    path.parent.mkdir()
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE partners (id INTEGER PRIMARY KEY, name TEXT NOT NULL, social_url TEXT)"
    )
    conn.execute(
        "INSERT INTO partners (name, social_url) VALUES (?, ?)",
        ("Legacy Co", "https://x.com/legacy"),
    )
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.close()
    monkeypatch.setattr("app.database.DB_PATH", str(path))
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    init_db()
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(partners)")}
        assert "linkedin_url" in cols
        assert "owner_key" in cols
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "deals" in tables
        assert "users" in tables
        assert "deal_tags" in tables
        assert not any(name.startswith("rate_limit_") for name in tables)
    pre = list((tmp_path / "backups").glob("pre-migrate-v0-to-*.db"))
    assert pre, "expected an online backup next to data/ before migrating"


def _rate_limit_sqlite_names(conn):
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'rate_limit_%'"
        )
    }


def test_fresh_db_has_no_rate_limit_tables(db):
    with get_db() as conn:
        assert _rate_limit_sqlite_names(conn) == set()


def test_init_db_migrates_v2_drops_rate_limit_hits(tmp_path, monkeypatch):
    """A live v2 file that still has rate_limit_hits must drop it at v3."""
    path = tmp_path / "data" / "crm.db"
    path.parent.mkdir()
    monkeypatch.setattr("app.database.DB_PATH", str(path))
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    with get_db() as db:
        migrate_001(db)
        migrate_002(db)
        db.execute(
            """
            CREATE TABLE rate_limit_hits (
                bucket TEXT NOT NULL,
                hit_at REAL NOT NULL
            )
            """
        )
        db.execute(
            "CREATE INDEX idx_rate_limit_hits_bucket_hit "
            "ON rate_limit_hits (bucket, hit_at)"
        )
        db.execute(
            "INSERT INTO rate_limit_hits (bucket, hit_at) VALUES (?, ?)",
            ("login:1.2.3.4", 1.0),
        )
        db.execute("PRAGMA user_version = 2")
        db.commit()
    init_db()
    with get_db() as conn:
        assert get_user_version(conn) == SCHEMA_VERSION
        assert _rate_limit_sqlite_names(conn) == set()
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "deal_tags" in tables
    pre = list((tmp_path / "backups").glob("pre-migrate-v2-to-*.db"))
    assert pre, "expected an online backup before migrating a live v2 DB"


def test_init_db_skips_backup_on_fresh_empty_db(tmp_path, monkeypatch):
    path = tmp_path / "data" / "crm.db"
    path.parent.mkdir()
    monkeypatch.setattr("app.database.DB_PATH", str(path))
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    init_db()
    backups = tmp_path / "backups"
    assert not backups.exists() or not list(backups.glob("pre-migrate-*.db"))


def test_init_db_idempotent_at_current_version(db):
    init_db()
    init_db()
    with get_db() as conn:
        assert get_user_version(conn) == SCHEMA_VERSION
        n = conn.execute("SELECT COUNT(*) AS n FROM pipeline_stages").fetchone()["n"]
        assert n == 7


def test_init_db_lock_serializes_concurrent_migrators(tmp_path, monkeypatch):
    path = tmp_path / "race.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE partners (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.close()
    monkeypatch.setattr("app.database.DB_PATH", str(path))
    errors = []

    def worker():
        try:
            init_db()
        except Exception as exc:  # noqa: BLE001 — collect any race failure
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    conn.close()
