import logging

from app.services.nurture import FAILED, SENT, SKIPPED, enroll_partner_in_nurture


class FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


PARTNER = {"id": 7, "name": "Jane Doe", "email": "jane@acme.example"}
SERVICE = {"name": "Consulting", "nurture_list_slug": "automation-interest"}


def _configure(monkeypatch, token=None):
    monkeypatch.setenv("NURTURE_WEBHOOK_URL", "http://hooks.test/nurture")
    if token:
        monkeypatch.setenv("NURTURE_WEBHOOK_TOKEN", token)
    else:
        monkeypatch.delenv("NURTURE_WEBHOOK_TOKEN", raising=False)


def test_noop_when_webhook_not_configured(monkeypatch, caplog):
    monkeypatch.delenv("NURTURE_WEBHOOK_URL", raising=False)
    called = []
    monkeypatch.setattr("app.services.nurture.httpx.post", lambda *a, **k: called.append(1))
    with caplog.at_level(logging.DEBUG, logger="app.services.nurture"):
        status, message = enroll_partner_in_nurture(PARTNER, SERVICE)
    assert status == SKIPPED
    assert "not configured" in message
    assert called == []
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


def test_noop_when_service_has_no_list_slug(monkeypatch):
    _configure(monkeypatch)
    status, message = enroll_partner_in_nurture(PARTNER, {"name": "Consulting", "nurture_list_slug": None})
    assert status == FAILED
    assert "nurture_list_slug" in message


def test_noop_when_partner_has_no_email(monkeypatch):
    _configure(monkeypatch)
    status, message = enroll_partner_in_nurture({"name": "Jane Doe", "email": None}, SERVICE)
    assert status == FAILED
    assert "no email" in message


def test_mark_lost_nurture_uses_lead_fields_when_there_is_no_partner(db, monkeypatch):
    from app.services.catalog import create_service, update_service
    from app.services.lead_records import create_lead, mark_lost
    from app.services.activities import list_activities_for_deal

    _configure(monkeypatch)
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None, **kwargs):
        captured.update(json=json)
        return FakeResponse()

    monkeypatch.setattr("app.services.nurture.httpx.post", fake_post)
    service_id = create_service("Consulting", "consulting-nurture", nurture_list_slug="automation-interest")
    assert service_id
    lead = create_lead(
        company_name="Harbour", email="pm@h.example", service_slug="consulting-nurture", log_create=False,
    )["lead"]
    result = mark_lost(lead["id"], lost_reason="Not now", note="next year")
    assert result["ok"] is True
    assert result["nurture"]["status"] == "sent"
    assert captured["json"]["partner_id"] is None
    assert captured["json"]["email"] == "pm@h.example"
    assert captured["json"]["type"] == "lead"
    assert captured["json"]["lost_reason"] == "Not now"

    bare = create_lead(company_name="No Service", email="nosvc@h.example", log_create=False)["lead"]
    missed = mark_lost(bare["id"], lost_reason="Not now")
    assert missed["nurture"]["status"] == "failed"
    assert any("nurture" in (row["body"] or "").lower() for row in list_activities_for_deal(bare["id"]))

    quiet = create_lead(company_name="Budget", email="budget@h.example", log_create=False)["lead"]
    no_send = mark_lost(quiet["id"], lost_reason="No budget")
    assert no_send["nurture"] is None
    update_service  # imported so a missing slug can be set if the create helper ignores it


def test_successful_hand_off_posts_expected_payload(monkeypatch):
    _configure(monkeypatch, token="test-token")
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None, **kwargs):
        captured.update(url=url, json=json, headers=headers)
        return FakeResponse()

    monkeypatch.setattr("app.services.nurture.httpx.post", fake_post)

    status, message = enroll_partner_in_nurture(PARTNER, SERVICE, deal_id=42)

    assert status == SENT
    assert "automation-interest" in message
    assert captured["url"] == "http://hooks.test/nurture"
    assert captured["headers"]["Authorization"] == "Bearer test-token"
    assert captured["json"] == {
        "email": "jane@acme.example",
        "name": "Jane Doe",
        "list_slug": "automation-interest",
        "partner_id": 7,
        "deal_id": 42,
    }


def test_no_auth_header_without_token(monkeypatch):
    _configure(monkeypatch)
    captured = {}
    monkeypatch.setattr(
        "app.services.nurture.httpx.post",
        lambda url, json=None, headers=None, **kw: captured.update(headers=headers) or FakeResponse(),
    )
    assert enroll_partner_in_nurture(PARTNER, SERVICE)[0] == SENT
    assert "Authorization" not in captured["headers"]


def test_http_failure_is_caught_not_raised(monkeypatch):
    _configure(monkeypatch)

    def fake_post(*args, **kwargs):
        raise ConnectionError("receiver is down")

    monkeypatch.setattr("app.services.nurture.httpx.post", fake_post)

    status, message = enroll_partner_in_nurture(PARTNER, SERVICE)
    assert status == FAILED
    assert "receiver is down" in message


def test_invalid_nurture_list_slug_does_not_call_webhook(monkeypatch):
    _configure(monkeypatch)
    called = []
    monkeypatch.setattr("app.services.nurture.httpx.post", lambda *a, **k: called.append(1))
    status, message = enroll_partner_in_nurture(PARTNER, {"name": "Consulting", "nurture_list_slug": "../admin"})
    assert status == FAILED
    assert "invalid nurture_list_slug" in message
    assert called == []
