from app.services.activities import list_activities_for_deal
from app.services.catalog import create_service
from app.services.deals import create_deal
from app.services.delegated_tasks import (
    complete_task,
    create_task,
    get_task,
    list_tasks,
    notify_agent,
    update_task,
)
from app.services.partners import create_partner
from tests.test_mcp_tools import _enable, call


class FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _deal_without_email(db):
    sid = create_service("Consulting", "consulting")
    pid = create_partner("Acme Childcare", is_company=True, phone="0400111222")
    deal_id = create_deal(pid, sid)
    return pid, deal_id


def test_create_rejects_garbage_owner(db):
    _, deal_id = _deal_without_email(db)
    task, error, created = create_task(deal_id, "Send reminder", owner="!!!")
    assert error == "invalid_owner"
    assert created is False
    assert task is None
    assert list_tasks(deal_id=deal_id) == []


def test_create_accepts_free_form_owner_slugs(db):
    _, deal_id = _deal_without_email(db)
    bot, error, _ = create_task(deal_id, "Bot work", owner="Sales Agent")
    assert error is None
    assert bot["owner"] == "sales-agent"
    assert bot["status"] == "proposed"
    default, error, _ = create_task(deal_id, "Default work")
    assert error is None
    assert default["owner"] == "agent"
    assert default["status"] == "delegated"


def test_default_owner_is_configurable(db, monkeypatch):
    monkeypatch.setenv("DELEGATE_DEFAULT_OWNER", "Ops Bot")
    _, deal_id = _deal_without_email(db)
    task, error, _ = create_task(deal_id, "Configured work")
    assert error is None
    assert task["owner"] == "ops-bot"
    assert task["status"] == "delegated"


def test_create_defaults_owner_agent_status_delegated(db):
    _, deal_id = _deal_without_email(db)
    task, error, created = create_task(deal_id, "Send Jane Discovery Meeting reminder", due_date="2026-09-16")
    assert error is None
    assert created is True
    assert task["owner"] == "agent"
    assert task["status"] == "delegated"
    assert task["due_date"] == "2026-09-16"
    assert task["webhook"]["reason"] == "webhook_unconfigured"
    rows = list_tasks(owner="agent", status="delegated")
    assert [t["id"] for t in rows] == [task["id"]]


def test_create_is_idempotent_on_deal_title_owner(db):
    _, deal_id = _deal_without_email(db)
    first, _, created = create_task(deal_id, "Send reminder", owner="agent")
    second, _, created_again = create_task(deal_id, "Send reminder", owner="agent")
    assert created is True
    assert created_again is False
    assert second["id"] == first["id"]
    assert len(list_tasks(deal_id=deal_id)) == 1


def test_partner_email_not_required(db):
    pid, deal_id = _deal_without_email(db)
    from app.services.partners import get_partner
    assert get_partner(pid)["email"] is None
    task, error, _ = create_task(deal_id, "Call without email")
    assert error is None
    assert task["partner_id"] == pid


def test_complete_writes_result_notes_and_timeline(db):
    _, deal_id = _deal_without_email(db)
    task, _, _ = create_task(deal_id, "Send reminder")
    done, error = complete_task(task["id"], result_notes="Reminder emailed to Jane.")
    assert error is None
    assert done["status"] == "done"
    assert done["result_notes"] == "Reminder emailed to Jane."
    bodies = [a["body"] for a in list_activities_for_deal(deal_id)]
    assert any("Delegated task done: Send reminder" in (b or "") for b in bodies)
    assert any("Reminder emailed to Jane." in (b or "") for b in bodies)
    notes = [a for a in list_activities_for_deal(deal_id) if a["type"] == "note"]
    assert len(notes) == 1
    complete_task(task["id"], result_notes="Reminder emailed to Jane.")
    assert len([a for a in list_activities_for_deal(deal_id) if a["type"] == "note"]) == 1


def test_webhook_fires_once_for_agent_delegated(db, monkeypatch):
    monkeypatch.setenv("TASK_WEBHOOK_URL", "http://agent.test/tasks")
    monkeypatch.setenv("TASK_WEBHOOK_TOKEN", "agent-secret")
    monkeypatch.setenv("BASE_URL", "https://crm.example.com")
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None, **kwargs):
        calls.append({"url": url, "json": json, "headers": headers})
        return FakeResponse()

    monkeypatch.setattr("app.services.delegated_tasks.httpx.post", fake_post)
    _, deal_id = _deal_without_email(db)
    task, _, _ = create_task(
        deal_id, "Send Jane Discovery Meeting reminder",
        owner="agent", due_date="2026-09-16",
    )
    assert task["webhook"]["notified"] is True
    assert len(calls) == 1
    payload = calls[0]["json"]
    assert payload["task_id"] == task["id"]
    assert payload["deal_id"] == deal_id
    assert payload["partner_name"] == "Acme Childcare"
    assert payload["title"] == "Send Jane Discovery Meeting reminder"
    assert payload["due_date"] == "2026-09-16"
    assert payload["crm_links"]["deal"] == f"https://crm.example.com/deals/{deal_id}/edit"
    assert calls[0]["headers"]["Authorization"] == "Bearer agent-secret"

    again = notify_agent(get_task(task["id"]))
    assert again["reason"] == "already_notified"
    assert len(calls) == 1


def test_webhook_on_status_change_to_delegated(db, monkeypatch):
    monkeypatch.setenv("TASK_WEBHOOK_URL", "http://agent.test/tasks")
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None, **kwargs):
        calls.append(json)
        return FakeResponse()

    monkeypatch.setattr("app.services.delegated_tasks.httpx.post", fake_post)
    _, deal_id = _deal_without_email(db)
    task, _, _ = create_task(deal_id, "Draft a note", owner="alice", status="proposed")
    assert task["webhook"]["reason"] == "not_agent_delegated"
    assert calls == []
    updated, error = update_task(task["id"], owner="agent", status="delegated")
    assert error is None
    assert updated["webhook"]["notified"] is True
    assert len(calls) == 1
    assert calls[0]["task_id"] == task["id"]


def test_mcp_handoff_to_agent(client, db, monkeypatch):
    _enable(monkeypatch)
    captured = []

    def fake_post(url, json=None, headers=None, timeout=None, **kwargs):
        captured.append(json)
        return FakeResponse()

    monkeypatch.setenv("TASK_WEBHOOK_URL", "http://agent.test/tasks")
    monkeypatch.setattr("app.services.delegated_tasks.httpx.post", fake_post)

    sid = create_service("Consulting", "consulting")
    pid = create_partner("Acme Childcare", is_company=True, phone="0400111222")
    deal_id = create_deal(pid, sid)

    created, _ = call(client, "create_delegated_task", {
        "deal_id": deal_id,
        "title": "Send Jane Discovery Meeting reminder",
        "owner": "agent",
        "due_date": "2026-09-16",
        "created_by": "sales-agent",
    })
    assert created["ok"] is True
    assert created["status"] == "created"
    task_id = created["task"]["id"]
    assert created["task"]["owner"] == "agent"
    assert created["task"]["status"] == "delegated"
    assert created["webhook"]["notified"] is True
    assert captured[0]["task_id"] == task_id

    listed, _ = call(client, "list_delegated_tasks", {
        "owner": "agent", "status": "delegated",
    })
    assert [t["id"] for t in listed["tasks"]] == [task_id]

    got, _ = call(client, "get_deal", {"deal_id": deal_id})
    assert [t["id"] for t in got["delegated_tasks"]] == [task_id]

    done, _ = call(client, "complete_delegated_task", {
        "task_id": task_id,
        "result_notes": "Reminder emailed to Jane.",
    })
    assert done["task"]["status"] == "done"
    assert done["task"]["result_notes"] == "Reminder emailed to Jane."
    empty, _ = call(client, "list_delegated_tasks", {
        "owner": "agent", "status": "delegated",
    })
    assert empty["tasks"] == []


def test_webhook_failure_persists_last_error_and_retries(db, monkeypatch):
    monkeypatch.setenv("TASK_WEBHOOK_URL", "http://agent.test/tasks")
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None, **kwargs):
        calls.append(json)
        if len(calls) == 1:
            return FakeResponse(status_code=502)
        return FakeResponse()

    monkeypatch.setattr("app.services.delegated_tasks.httpx.post", fake_post)
    _, deal_id = _deal_without_email(db)
    task, _, _ = create_task(deal_id, "Send reminder", owner="agent")
    assert task["webhook"]["notified"] is False
    assert task["webhook"]["reason"] == "webhook_failed"
    row = get_task(task["id"])
    assert row["webhook_notified_at"] is None
    assert row["webhook_last_attempt_at"]
    assert row["webhook_last_error"]
    assert "502" in row["webhook_last_error"] or "HTTP" in row["webhook_last_error"]

    retried = notify_agent(get_task(task["id"]))
    assert retried["notified"] is True
    assert len(calls) == 2
    row = get_task(task["id"])
    assert row["webhook_notified_at"]
    assert row["webhook_last_attempt_at"]
    assert row["webhook_last_error"] is None


def test_webhook_unconfigured_persists_last_error(db):
    _, deal_id = _deal_without_email(db)
    task, _, _ = create_task(deal_id, "Send reminder")
    assert task["webhook"]["reason"] == "webhook_unconfigured"
    row = get_task(task["id"])
    assert row["webhook_notified_at"] is None
    assert row["webhook_last_attempt_at"] is None
    assert row["webhook_last_error"] == "webhook_unconfigured"


def test_mcp_get_delegated_task_exposes_webhook_delivery(client, db, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setenv("TASK_WEBHOOK_URL", "http://agent.test/tasks")
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None, **kwargs):
        calls.append(json)
        return FakeResponse()

    monkeypatch.setattr("app.services.delegated_tasks.httpx.post", fake_post)
    sid = create_service("Consulting", "consulting")
    pid = create_partner("Acme Childcare", is_company=True, phone="0400111222")
    deal_id = create_deal(pid, sid)
    created, _ = call(client, "create_delegated_task", {
        "deal_id": deal_id,
        "title": "Send Jane Discovery Meeting reminder",
        "owner": "agent",
    })
    task_id = created["task"]["id"]
    assert created["task"]["webhook"]["notified"] is True
    assert created["task"]["webhook"]["notified_at"]
    assert created["task"]["webhook"]["last_attempt_at"]
    assert created["task"]["webhook"]["last_error"] is None

    got, _ = call(client, "get_delegated_task", {"task_id": task_id})
    assert got["ok"] is True
    assert got["task"]["id"] == task_id
    assert got["task"]["webhook"]["notified"] is True
    assert got["task"]["webhook"]["notified_at"]
    assert got["task"]["webhook"]["last_attempt_at"]
    assert got["task"]["webhook"]["last_error"] is None
    assert got["partner"]["id"] == pid
    assert got["deal"]["id"] == deal_id

    listed, _ = call(client, "list_delegated_tasks", {"owner": "agent"})
    assert listed["tasks"][0]["webhook"]["notified"] is True
    assert listed["tasks"][0]["webhook"]["last_error"] is None

    missing, is_error = call(client, "get_delegated_task", {"task_id": 999})
    assert is_error is True
    assert missing["error"] == "not_found"
