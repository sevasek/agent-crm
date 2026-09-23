from app.services import api_keys
from app.services.auth import create_user
from app.services.catalog import create_service


def test_create_api_key_returns_plaintext_once_and_stores_only_hash(db):
    row, plaintext = api_keys.create_api_key(1, "importer bot")
    assert plaintext.startswith("crm_")
    assert row["label"] == "importer bot"
    assert row["key_prefix"] == plaintext[:12]
    assert row["key_hash"] != plaintext


def test_verify_api_key_finds_match_and_sets_last_used(db):
    _, plaintext = api_keys.create_api_key(1, "bot")
    assert api_keys.verify_api_key("wrong") is None
    row = api_keys.verify_api_key(plaintext)
    assert row is not None
    assert row["last_used_at"] is not None


def test_list_api_keys_scoped_to_user(db):
    api_keys.create_api_key(1, "user1 key")
    api_keys.create_api_key(2, "user2 key")
    assert [k["label"] for k in api_keys.list_api_keys(1)] == ["user1 key"]
    assert [k["label"] for k in api_keys.list_api_keys(2)] == ["user2 key"]


def test_delete_api_key_scoped_to_owner(db):
    row, _ = api_keys.create_api_key(1, "bot")
    assert api_keys.delete_api_key(2, row["id"]) is False
    assert api_keys.delete_api_key(1, row["id"]) is True
    assert api_keys.list_api_keys(1) == []


def test_blank_label_gets_placeholder(db):
    row, _ = api_keys.create_api_key(1, "   ")
    assert row["label"] == "Unlabeled key"


def test_leads_api_accepts_a_created_key(client, db, monkeypatch):
    monkeypatch.delenv("CRM_API_KEY", raising=False)
    create_service("Consulting", "consulting")
    user_id = create_user("test@example.com", "Test User", "password123")
    _, plaintext = api_keys.create_api_key(user_id, "bot")

    resp = client.post(
        "/api/v1/leads",
        json={"leads": [{"name": "X", "service_slug": "consulting"}]},
        headers={"X-API-Key": plaintext},
    )
    assert resp.status_code == 200


def test_settings_key_does_not_authorize_mcp(client, db, monkeypatch):
    monkeypatch.setenv("CRM_MCP_API_KEY", "mcp-only")
    user_id = create_user("test@example.com", "Test User", "password123")
    _, plaintext = api_keys.create_api_key(user_id, "bot")
    resp = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers={"X-API-Key": plaintext, "Accept": "application/json"},
    )
    assert resp.status_code == 401


def test_settings_page_requires_login(client):
    resp = client.get("/settings", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


def test_create_and_revoke_api_key_via_settings_page(logged_in_client):
    page = logged_in_client.get("/settings")
    csrf = page.text.split('name="csrf_token" value="')[1].split('"')[0]

    created = logged_in_client.post(
        "/settings/api-keys/new", data={"label": "importer bot", "csrf_token": csrf},
    )
    assert created.status_code == 200
    assert "importer bot" in created.text
    assert "crm_" in created.text  # plaintext shown once

    key_id = api_keys.list_api_keys(1)[0]["id"]

    deleted = logged_in_client.post(
        f"/settings/api-keys/{key_id}/delete", data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert deleted.status_code == 303
    listing = logged_in_client.get("/settings")
    assert "importer bot" not in listing.text
