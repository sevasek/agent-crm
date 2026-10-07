"""Upgrade an unversioned sevasek-shaped database and check what survives."""
import sqlite3

from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.database import SCHEMA_VERSION, get_install_id, get_user_version, init_db
from app.routers.auth import cookie_signer
from app.services.auth import SECRET_KEY, check_env_api_key
from app.services.mcp_oauth import issue_tokens, load_refresh_token, refresh_access
from tests.legacy_sevasek_fixture import build_legacy_sevasek_db


def _counts(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    tables = (
        "users", "partners", "services", "offers", "deals",
        "activities", "delegated_tasks", "mcp_oauth_clients", "pipeline_stages",
    )
    counts = {
        name: conn.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()["n"]
        for name in tables
    }
    conn.close()
    return counts


def _upgrade(tmp_path, monkeypatch):
    path = tmp_path / "data" / "crm.db"
    path.parent.mkdir()
    build_legacy_sevasek_db(path)
    before = _counts(path)
    monkeypatch.setattr("app.database.DB_PATH", str(path))
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("CRM_MCP_API_KEY", "kept-mcp-key")
    # Signed before migrate, the way a connector holds a refresh token.
    issued = issue_tokens("grok-connector")
    old_session = URLSafeTimedSerializer(SECRET_KEY, salt="session").dumps(
        {"user_id": 1, "sv": 0}
    )
    init_db()
    return path, before, issued, old_session


def test_legacy_upgrade_preserves_rows_and_creates_campaign_tags(tmp_path, monkeypatch):
    path, before, issued, old_session = _upgrade(tmp_path, monkeypatch)
    after = _counts(path)
    assert after == before

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION

    tags = {
        (row["deal_id"], row["tag"])
        for row in conn.execute("SELECT deal_id, tag FROM deal_tags ORDER BY deal_id")
    }
    assert tags == {
        (1, "campaign:icp-hc-illawarra-2026-09"),
        (4, "campaign:dead-leads-2026"),
    }
    refs = {
        row["id"]: row["external_ref"]
        for row in conn.execute("SELECT id, external_ref FROM deals ORDER BY id")
    }
    assert refs[1] == "campaign:icp-hc-illawarra-2026-09"
    assert refs[4] == "Campaign:Dead-Leads-2026"
    assert refs[5] == "success-agent:mustard-seed"

    children = {
        row["id"]: (row["parent_deal_id"], row["source"], row["stage"])
        for row in conn.execute(
            "SELECT id, parent_deal_id, source, stage FROM deals WHERE parent_deal_id IS NOT NULL"
        )
    }
    assert children[2] == (1, "hc-spawn:1", "new")
    assert children[3] == (1, "hc-spawn:1", "qualified")

    tasks = {
        row["id"]: (
            row["owner"],
            row["title"],
            row["status"],
            row["webhook_notified_at"],
            row["webhook_last_attempt_at"],
            row["webhook_last_error"],
        )
        for row in conn.execute(
            """SELECT id, owner, title, status, webhook_notified_at,
                      webhook_last_attempt_at, webhook_last_error
               FROM delegated_tasks"""
        )
    }
    assert tasks[1][0] == "willow"
    assert tasks[1][1] == "Send HC kickoff"
    assert tasks[1][3] == "2026-09-01T00:00:00"
    assert tasks[1][5] is None
    assert tasks[2][0] == "hermes"
    assert tasks[2][5] == "HTTP 502"

    joe = conn.execute("SELECT owner_key FROM deals WHERE id = 4").fetchone()["owner_key"]
    assert joe == "joe-bot"

    offer = conn.execute("SELECT price, currency FROM offers WHERE id = 1").fetchone()
    assert offer["price"] == 700
    assert offer["currency"] == "AUD"

    names = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "rate_limit_hits" not in names
    assert "app_install" in names
    assert "mcp_oauth_used_codes" in names
    assert "deal_tags" in names
    user_cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    assert "session_version" in user_cols
    conn.close()

    backups = list((tmp_path / "backups").glob("pre-migrate-v0-to-*.db"))
    assert backups, "expected an automatic pre-upgrade backup"

    install_id = get_install_id()
    assert len(install_id) == 64
    init_db()
    assert get_install_id() == install_id
    assert _counts(path) == before
    with sqlite3.connect(path) as conn:
        assert get_user_version(conn) == SCHEMA_VERSION


def test_oauth_refresh_survives_same_secret_and_browser_session_does_not(tmp_path, monkeypatch):
    _path, _before, issued, old_session = _upgrade(tmp_path, monkeypatch)

    loaded = load_refresh_token(issued["refresh_token"])
    assert loaded is not None
    assert loaded["cid"] == "grok-connector"
    assert loaded["typ"] == "mcp_refresh"
    refreshed, error = refresh_access("grok-connector", issued["refresh_token"])
    assert error is None
    assert refreshed["access_token"]
    assert load_refresh_token(refreshed["refresh_token"])["cid"] == "grok-connector"

    # install_id is created on this upgrade and mixed into the session signer.
    # A cookie from before app_install existed does not verify.
    try:
        cookie_signer.loads(old_session, max_age=60 * 60 * 24 * 30)
        raise AssertionError("pre-upgrade session cookie should not verify")
    except BadSignature:
        pass

    fresh = cookie_signer.dumps({"user_id": 1, "sv": 0})
    assert cookie_signer.loads(fresh, max_age=60 * 60 * 24 * 30)["user_id"] == 1

    # The MCP API key is an env var, not a row. Keeping it is enough.
    assert check_env_api_key("kept-mcp-key", "CRM_MCP_API_KEY") is True
    assert check_env_api_key("some-other-key", "CRM_MCP_API_KEY") is False
