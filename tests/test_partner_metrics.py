"""Storage for partner metric snapshots, and the partner-page form."""
from datetime import datetime

from app.services.auth import generate_csrf_token
from app.services.partner_metrics import (
    latest_snapshots,
    list_snapshots,
    record_snapshot,
    record_snapshots,
)
from app.services.partners import create_partner, get_partner


def test_record_appends_and_keeps_history(db):
    company_id = create_partner("Acme Dental", is_company=True)
    now = datetime(2026, 8, 1, 15, 4, 0)
    first, error = record_snapshot(
        company_id, "Google-Rating", "Google-Maps",
        numeric_value=4.2, captured_at="2026-01-15", now=now,
    )
    assert error is None
    assert first["metric_key"] == "google-rating"
    assert first["source"] == "google-maps"
    assert first["numeric_value"] == 4.2
    assert first["captured_at"] == "2026-01-15T00:00:00"
    assert first["text_value"] is None

    second, error = record_snapshot(
        company_id, "google-rating", "google-maps",
        numeric_value=4.8, text_value="verified",
        captured_at="2026-08-01T15:04:00Z", now=now,
    )
    assert error is None
    assert second["id"] != first["id"]
    assert second["numeric_value"] == 4.8
    assert second["text_value"] == "verified"
    assert second["captured_at"] == "2026-08-01T15:04:00"

    rows, error = list_snapshots(company_id, metric_key="google-rating")
    assert error is None
    assert [row["numeric_value"] for row in rows] == [4.8, 4.2]
    assert [row["id"] for row in rows] == [second["id"], first["id"]]

    latest, _ = latest_snapshots(company_id)
    assert latest == [second]
    # The first row is still the January reading. Nothing was overwritten.
    stored, _ = list_snapshots(company_id)
    assert stored[1]["numeric_value"] == 4.2
    assert stored[1]["captured_at"] == "2026-01-15T00:00:00"


def test_omitted_captured_at_uses_the_clock(db):
    company_id = create_partner("Acme Dental", is_company=True)
    now = datetime(2026, 8, 1, 12, 0, 0)
    row, error = record_snapshot(
        company_id, "google-review-count", "google-maps", numeric_value=312, now=now,
    )
    assert error is None
    assert row["captured_at"] == "2026-08-01T12:00:00"
    assert row["numeric_value"] == 312
    assert get_partner(company_id)["updated_at"] == "2026-08-01T12:00:00"


def test_text_only_snapshot_is_stored(db):
    company_id = create_partner("Acme Dental", is_company=True)
    row, error = record_snapshot(
        company_id, "website-signal", "website-check", text_value="booking widget",
        now=datetime(2026, 8, 1),
    )
    assert error is None
    assert row["numeric_value"] is None
    assert row["text_value"] == "booking widget"


def test_invalid_snapshot_writes_nothing(db):
    company_id = create_partner("Acme Dental", is_company=True)
    now = datetime(2026, 8, 1)
    for kwargs in (
        {"metric_key": "Not A Key", "source": "google-maps", "numeric_value": 1},
        {"metric_key": "google-rating", "source": "Google Maps", "numeric_value": 1},
        {"metric_key": "google-rating", "source": "google-maps"},
        {"metric_key": "google-rating", "source": "google-maps", "numeric_value": "nope"},
        {"metric_key": "google-rating", "source": "google-maps", "numeric_value": 4.8,
         "captured_at": "yesterday"},
    ):
        row, error = record_snapshot(company_id, now=now, **kwargs)
        assert row is None
        assert error in {
            "invalid_metric_key", "invalid_source", "invalid_value", "invalid_captured_at",
        }
    rows, _ = list_snapshots(company_id)
    assert rows == []

    missing, error = record_snapshot(99999, "google-rating", "google-maps", numeric_value=1)
    assert missing is None
    assert error == "not_found"


def test_list_filters_by_key_and_date_range(db):
    company_id = create_partner("Acme Dental", is_company=True)
    other_id = create_partner("Other Co", is_company=True)
    now = datetime(2026, 8, 2)
    record_snapshot(
        company_id, "google-rating", "google-maps", numeric_value=4.2,
        captured_at="2026-07-01T10:00:00", now=now,
    )
    record_snapshot(
        company_id, "google-rating", "google-maps", numeric_value=4.8,
        captured_at="2026-08-01T10:00:00", now=now,
    )
    record_snapshot(
        company_id, "google-review-count", "google-maps", numeric_value=312,
        captured_at="2026-08-01T11:00:00", now=now,
    )
    record_snapshot(
        other_id, "google-rating", "google-maps", numeric_value=3.0,
        captured_at="2026-08-01T10:00:00", now=now,
    )

    august, error = list_snapshots(
        company_id, captured_after="2026-08-01", captured_before="2026-08-01",
    )
    assert error is None
    assert [row["numeric_value"] for row in august] == [312, 4.8]

    ratings, _ = list_snapshots(company_id, metric_key="Google-Rating")
    assert [row["numeric_value"] for row in ratings] == [4.8, 4.2]

    bad, error = list_snapshots(company_id, captured_after="not-a-date")
    assert bad is None
    assert error == "invalid_captured_at"


def test_bulk_record_is_all_or_nothing(db):
    company_id = create_partner("Acme Dental", is_company=True)
    other_id = create_partner("Other Co", is_company=True)
    now = datetime(2026, 8, 1, 9, 0, 0)
    rows, error = record_snapshots([
        {"partner_id": company_id, "metric_key": "google-rating", "numeric_value": 4.8,
         "source": "google-maps", "captured_at": "2026-08-01T09:00:00Z"},
        {"partner_id": company_id, "metric_key": "google-review-count", "numeric_value": 312,
         "source": "google-maps"},
        {"partner_id": other_id, "metric_key": "ads-in-library", "numeric_value": 6,
         "text_value": "active", "source": "ad-library"},
    ], now=now)
    assert error is None
    assert [row["metric_key"] for row in rows] == [
        "google-rating", "google-review-count", "ads-in-library",
    ]
    assert rows[1]["captured_at"] == "2026-08-01T09:00:00"
    assert rows[2]["text_value"] == "active"

    failed, error = record_snapshots([
        {"partner_id": company_id, "metric_key": "google-rating", "numeric_value": 5,
         "source": "google-maps"},
        {"partner_id": company_id, "metric_key": "bad key", "numeric_value": 1,
         "source": "google-maps"},
    ], now=now)
    assert failed is None
    assert error["error"] == "invalid_metric_key"
    assert error["index"] == 1
    ratings, _ = list_snapshots(company_id, metric_key="google-rating")
    assert [row["numeric_value"] for row in ratings] == [4.8]

    missing, error = record_snapshots([
        {"partner_id": 99999, "metric_key": "google-rating", "numeric_value": 1,
         "source": "google-maps"},
    ])
    assert missing is None
    assert error["error"] == "not_found"


def test_partner_page_records_and_shows_the_latest_reading(logged_in_client, db):
    company_id = create_partner("Acme Dental", is_company=True)
    person_id = create_partner("Pat Manager", email="pat@acme.example", parent_id=company_id)
    token = generate_csrf_token(logged_in_client.cookies.get("session"))
    resp = logged_in_client.post(f"/contacts/partners/{company_id}/metrics", data={
        "metric_key": "Google-Rating",
        "numeric_value": "4.8",
        "text_value": "",
        "source": "google-maps",
        "captured_at": "2026-08-01T15:04:00Z",
        "csrf_token": token,
    }, follow_redirects=False)
    assert resp.status_code == 303

    logged_in_client.post(f"/contacts/partners/{company_id}/metrics", data={
        "metric_key": "google-review-count",
        "numeric_value": "312",
        "source": "google-maps",
        "captured_at": "2026-05-01",
        "csrf_token": token,
    }, follow_redirects=False)
    logged_in_client.post(f"/contacts/partners/{company_id}/metrics", data={
        "metric_key": "google-review-count",
        "numeric_value": "340",
        "source": "google-maps",
        "captured_at": "2026-08-01",
        "csrf_token": token,
    }, follow_redirects=False)

    page = logged_in_client.get(f"/contacts/partners/{company_id}")
    assert page.status_code == 200
    assert "google-rating" in page.text
    assert "4.8" in page.text
    assert "google-maps" in page.text
    assert "340" in page.text
    assert "312" in page.text  # history is still on the page

    bad = logged_in_client.post(f"/contacts/partners/{company_id}/metrics", data={
        "metric_key": "Not A Key",
        "numeric_value": "1",
        "source": "google-maps",
        "csrf_token": token,
    })
    assert bad.status_code == 400
    assert "slug" in bad.text.lower()
    stored, _ = list_snapshots(company_id, metric_key="google-rating")
    assert len(stored) == 1

    person = logged_in_client.get(f"/contacts/partners/{person_id}")
    assert "Intel on" in person.text
    assert "google-rating" in person.text
    assert "4.8" in person.text


def test_icp_form_saves_a_metric_rule(logged_in_client, db):
    from app.services import icp

    token = generate_csrf_token(logged_in_client.cookies.get("session"))
    resp = logged_in_client.post("/definitions/icp/new", data={
        "label": "Reviews up",
        "field": "company_metric",
        "operator": "gte",
        "value": "20",
        "metric_key": "Google-Review-Count",
        "period_days": "90",
        "weight": "3",
        "active": "on",
        "csrf_token": token,
    }, follow_redirects=False)
    assert resp.status_code == 303
    rule = icp.list_criteria()[0]
    assert rule["metric_key"] == "google-review-count"
    assert rule["period_days"] == 90
    assert rule["value"] == "20"

    page = logged_in_client.get("/definitions/icp")
    assert "Company metric" in page.text
    assert "google-review-count" in page.text
    assert "90" in page.text

    edited = logged_in_client.post(f"/definitions/icp/{rule['id']}/edit", data={
        "label": "Reviews up",
        "field": "company_metric",
        "operator": "gte",
        "value": "20",
        "metric_key": "google-review-count",
        "period_days": "",
        "weight": "3",
        "active": "on",
        "csrf_token": token,
    }, follow_redirects=False)
    assert edited.status_code == 303
    assert icp.get_criterion(rule["id"])["period_days"] is None
