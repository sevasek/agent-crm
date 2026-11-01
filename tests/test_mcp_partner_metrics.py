"""MCP tools for metric snapshots and company-metric ICP rules."""
from datetime import datetime, timedelta, timezone

from app.services.partner_metrics import list_snapshots
from app.services.partners import create_partner
from tests.test_mcp_tools import _enable, call


def test_record_list_and_score_company_metrics(client, db, monkeypatch):
    from app.services.catalog import create_service
    from app.services.deals import create_deal

    _enable(monkeypatch)
    company_id = create_partner("Acme Dental", is_company=True)
    person_id = create_partner("Pat Manager", email="pat@acme.example", parent_id=company_id)
    now = datetime(2026, 8, 1, 12, 0, 0)
    baseline = (now - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
    latest = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    empty, is_error = call(client, "list_partner_metrics", {"partner_id": company_id})
    assert is_error is False
    assert empty["snapshots"] == []
    assert empty["latest"] == []

    one, is_error = call(client, "record_partner_metric", {
        "partner_id": company_id,
        "metric_key": "Google-Rating",
        "numeric_value": 4.2,
        "source": "Google-Maps",
        "captured_at": "2026-01-01",
    })
    assert is_error is False
    assert one["snapshot"]["metric_key"] == "google-rating"
    assert one["snapshot"]["source"] == "google-maps"
    assert one["snapshot"]["numeric_value"] == 4.2
    assert one["snapshot"]["captured_at"] == "2026-01-01T00:00:00"

    bulk, is_error = call(client, "record_partner_metrics", {"snapshots": [
        {"partner_id": company_id, "metric_key": "google-rating", "numeric_value": 4.8,
         "source": "google-maps", "captured_at": latest},
        {"partner_id": company_id, "metric_key": "google-review-count", "numeric_value": 312,
         "source": "google-maps", "captured_at": baseline},
        {"partner_id": company_id, "metric_key": "google-review-count", "numeric_value": 340,
         "source": "google-maps", "captured_at": latest},
    ]})
    assert is_error is False
    assert bulk["count"] == 3

    # The January rating is still there.
    history, _ = call(client, "list_partner_metrics", {
        "partner_id": company_id,
        "metric_key": "google-rating",
    })
    assert [row["numeric_value"] for row in history["snapshots"]] == [4.8, 4.2]
    assert history["latest"][0]["numeric_value"] == 4.8

    day, _ = call(client, "list_partner_metrics", {
        "partner_id": company_id,
        "captured_after": latest[:10],
        "captured_before": latest[:10],
        "latest_only": True,
    })
    assert {row["metric_key"]: row["numeric_value"] for row in day["snapshots"]} == {
        "google-rating": 4.8,
        "google-review-count": 340,
    }

    bad, is_error = call(client, "record_partner_metric", {
        "partner_id": company_id,
        "metric_key": "Not A Key",
        "numeric_value": 1,
        "source": "google-maps",
    })
    assert is_error is True
    assert bad["error"] == "invalid_metric_key"
    assert len(list_snapshots(company_id)[0]) == 4

    partial, is_error = call(client, "record_partner_metrics", {"snapshots": [
        {"partner_id": company_id, "metric_key": "google-rating", "numeric_value": 5,
         "source": "google-maps"},
        {"partner_id": 99999, "metric_key": "google-rating", "numeric_value": 1,
         "source": "google-maps"},
    ]})
    assert is_error is True
    assert partial["error"] == "not_found"
    assert partial["index"] == 1
    assert [row["numeric_value"] for row in list_snapshots(company_id, metric_key="google-rating")[0]] == [
        4.8, 4.2,
    ]

    gone, is_error = call(client, "list_partner_metrics", {"partner_id": 99999})
    assert is_error is True
    assert gone["error"] == "not_found"

    got, _ = call(client, "get_partner", {"partner_id": person_id})
    assert got["partner"]["latest_metrics"] == []
    assert {row["metric_key"]: row["numeric_value"] for row in got["company"]["latest_metrics"]} == {
        "google-rating": 4.8,
        "google-review-count": 340,
    }

    catalog, _ = call(client, "list_catalog")
    assert "company_metric" in {row["key"] for row in catalog["icp_fields"]}
    assert {"gte", "lte", "gt", "lt", "eq"} <= {row["key"] for row in catalog["icp_operators"]}

    created, is_error = call(client, "create_icp_criterion", {
        "field": "company_metric",
        "operator": "gte",
        "value": "4.5",
        "metric_key": "google-rating",
        "weight": 5,
        "label": "Strong rating",
    })
    assert is_error is False
    assert created["criterion"]["metric_key"] == "google-rating"
    assert created["criterion"]["period_days"] is None
    criterion_id = created["criterion"]["id"]

    growth, _ = call(client, "create_icp_criterion", {
        "field": "company_metric",
        "operator": "gte",
        "value": "20",
        "metric_key": "google-review-count",
        "period_days": 90,
        "weight": 3,
        "label": "Reviews up",
    })
    assert growth["criterion"]["period_days"] == 90

    rejected, is_error = call(client, "create_icp_criterion", {
        "field": "team_size",
        "operator": "gt",
        "value": "10",
        "metric_key": "google-rating",
    })
    assert is_error is True
    assert rejected["error"] == "metric_fields"

    # The deal is on the person. Fit still reads the company's snapshots.
    # The growth rule depends on "now", so pin the comparison through the
    # service clock by using snapshots relative to today only when the
    # recorded times fall inside a 90-day window from the real clock.
    service_id = create_service("Consulting", "consulting-metrics")
    deal_id = create_deal(person_id, service_id)
    # Re-record the pair against the actual clock so the live score matches,
    # without deleting the earlier history.
    fresh_now = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    call(client, "record_partner_metrics", {"snapshots": [
        {"partner_id": company_id, "metric_key": "google-review-count", "numeric_value": 312,
         "source": "google-maps",
         "captured_at": (fresh_now - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")},
        {"partner_id": company_id, "metric_key": "google-review-count", "numeric_value": 340,
         "source": "google-maps",
         "captured_at": (fresh_now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")},
        {"partner_id": company_id, "metric_key": "google-rating", "numeric_value": 4.8,
         "source": "google-maps",
         "captured_at": (fresh_now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")},
    ]})
    scored, is_error = call(client, "get_deal", {"deal_id": deal_id})
    assert is_error is False
    assert scored["icp"]["score"] == 8
    assert scored["icp"]["max"] == 8
    assert "Strong rating" in scored["icp"]["matched"]
    assert "Reviews up" in scored["icp"]["matched"]

    updated, _ = call(client, "update_icp_criterion", {
        "criterion_id": criterion_id,
        "value": "4.9",
    })
    assert updated["criterion"]["value"] == "4.9"
    assert updated["criterion"]["metric_key"] == "google-rating"
    rescored, _ = call(client, "get_deal", {"deal_id": deal_id})
    assert rescored["icp"]["score"] == 3
    assert "Strong rating" in rescored["icp"]["unmatched"]

    cleared, _ = call(client, "update_icp_criterion", {
        "criterion_id": growth["criterion"]["id"],
        "period_days": None,
        "operator": "gte",
        "value": "300",
    })
    assert cleared["criterion"]["period_days"] is None
    level, _ = call(client, "get_deal", {"deal_id": deal_id})
    assert level["icp"]["score"] == 3
    assert "Reviews up" in level["icp"]["matched"]
