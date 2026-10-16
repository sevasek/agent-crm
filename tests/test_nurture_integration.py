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


def test_mark_lost_not_now_posts_webhook_for_lead_without_partner(db, monkeypatch):
    from app.services.activities import list_activities_for_deal
    from app.services.catalog import create_service
    from app.services.deals import mark_lost
    from app.services.lead_records import create_lead

    _configure(monkeypatch)
    create_service("Consulting", "consulting", nurture_list_slug="automation-interest")
    lead, error = create_lead(
        company_name="Harbour Physio",
        contact_name="Pat Manager",
        email="pat@harbour.example",
        service_slug="consulting",
    )
    assert error is None
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None, **kwargs):
        captured.update(url=url, json=json, headers=headers)
        return FakeResponse()

    monkeypatch.setattr("app.services.nurture.httpx.post", fake_post)

    updated, lost_error = mark_lost(lead["id"], lost_reason="Not now")
    assert lost_error is None
    assert updated["nurture"]["status"] == SENT
    assert captured["json"] == {
        "email": "pat@harbour.example",
        "name": "Pat Manager",
        "list_slug": "automation-interest",
        "partner_id": None,
        "deal_id": lead["id"],
        "type": "lead",
        "lost_reason": "Not now",
    }
    bodies = [a["body"] for a in list_activities_for_deal(lead["id"])]
    assert any("Nurture enrollment succeeded" in (b or "") for b in bodies)


def test_mark_lost_lead_without_service_logs_failure(db, monkeypatch):
    from app.services.activities import list_activities_for_deal
    from app.services.deals import mark_lost
    from app.services.lead_records import create_lead

    _configure(monkeypatch)
    called = []
    monkeypatch.setattr("app.services.nurture.httpx.post", lambda *a, **k: called.append(1))
    lead, error = create_lead(
        company_name="No Service Co",
        email="lead@noservice.example",
    )
    assert error is None
    updated, lost_error = mark_lost(lead["id"], lost_reason="Not now")
    assert lost_error is None
    assert updated["nurture"]["status"] == "failed"
    assert "no service" in updated["nurture"]["message"].lower()
    assert called == []
    bodies = [a["body"] for a in list_activities_for_deal(lead["id"])]
    assert any("no service" in (b or "").lower() for b in bodies)


def test_mark_lost_without_triggers_nurture_sends_nothing(db, monkeypatch):
    from app.services.catalog import create_service
    from app.services.deals import mark_lost
    from app.services.lead_records import create_lead

    _configure(monkeypatch)
    create_service("Consulting", "consulting", nurture_list_slug="automation-interest")
    called = []
    monkeypatch.setattr("app.services.nurture.httpx.post", lambda *a, **k: called.append(1))
    lead, error = create_lead(
        company_name="Not Fit Co",
        email="lead@notfit.example",
        service_slug="consulting",
    )
    assert error is None
    updated, lost_error = mark_lost(lead["id"], lost_reason="Not a fit")
    assert lost_error is None
    assert "nurture" not in updated
    assert called == []
