from app.services import icp


def test_no_criteria_by_default(db):
    assert icp.list_criteria() == []


def test_create_criterion(db):
    c, error = icp.create_criterion("team_size", "gt", "10", weight=5, label="Enterprise-sized")
    assert error is None
    assert c["field"] == "team_size"
    assert c["operator"] == "gt"
    assert c["value"] == "10"
    assert c["weight"] == 5


def test_create_criterion_rejects_invalid_field(db):
    c, error = icp.create_criterion("not_a_field", "is_not_null", "")
    assert c is None
    assert error == "invalid_field"


def test_create_criterion_rejects_invalid_operator(db):
    c, error = icp.create_criterion("team_size", "not_an_operator", "")
    assert c is None
    assert error == "invalid_operator"


def test_create_boolean_criterion_requires_true_or_false(db):
    c, error = icp.create_criterion("is_company", "boolean", "yes")
    assert c is None
    assert error == "invalid_value"
    c, error = icp.create_criterion("is_company", "boolean", "true")
    assert error is None


def test_create_numeric_criterion_requires_number(db):
    c, error = icp.create_criterion("team_size", "gt", "not-a-number")
    assert c is None
    assert error == "invalid_value"


def test_create_numeric_criterion_rejects_non_finite(db):
    # float("inf") / float("nan") don't raise, so a bare try/except float()
    # would let them through — must check math.isfinite explicitly.
    c, error = icp.create_criterion("team_size", "gt", "Infinity")
    assert c is None
    assert error == "invalid_value"
    c, error = icp.create_criterion("team_size", "gt", "NaN")
    assert c is None
    assert error == "invalid_value"


def test_match_numeric_ignores_non_finite_stored_field(db):
    # Defense in depth: even if a non-finite value ended up in a matched
    # field somehow, don't let IEEE comparison rules (e.g. 25 < inf) make
    # it match.
    assert icp.match_criterion({"value_estimate": float("inf")}, "value_estimate", "gt", "1000") is False


def test_create_fuzzy_criterion_requires_nonblank_value(db):
    c, error = icp.create_criterion("industry", "fuzzy_match", "")
    assert c is None
    assert error == "invalid_value"


def test_update_criterion(db):
    c, _ = icp.create_criterion("team_size", "gt", "10", weight=5)
    updated, error = icp.update_criterion(c["id"], weight=10)
    assert error is None
    assert updated["weight"] == 10


def test_update_unknown_criterion_not_found(db):
    updated, error = icp.update_criterion(999, weight=1)
    assert updated is None
    assert error == "not_found"


def test_update_criterion_revalidates_merged_state(db):
    c, _ = icp.create_criterion("team_size", "gt", "10", weight=5)
    updated, error = icp.update_criterion(c["id"], operator="boolean")  # value "10" invalid for boolean
    assert updated is None
    assert error == "invalid_value"


def test_delete_criterion(db):
    c, _ = icp.create_criterion("team_size", "gt", "10")
    ok, error = icp.delete_criterion(c["id"])
    assert ok is True
    assert icp.get_criterion(c["id"]) is None


def test_match_is_not_null(db):
    assert icp.match_criterion({"industry": "Retail"}, "industry", "is_not_null", "") is True
    assert icp.match_criterion({"industry": ""}, "industry", "is_not_null", "") is False
    assert icp.match_criterion({}, "industry", "is_not_null", "") is False


def test_match_boolean(db):
    assert icp.match_criterion({"is_company": 1}, "is_company", "boolean", "true") is True
    assert icp.match_criterion({"is_company": 0}, "is_company", "boolean", "true") is False
    assert icp.match_criterion({"is_company": 0}, "is_company", "boolean", "false") is True


def test_match_fuzzy(db):
    assert icp.match_criterion({"industry": "Retail Centre"}, "industry", "fuzzy_match", "retail") is True
    assert icp.match_criterion({"industry": "Reteil"}, "industry", "fuzzy_match", "retail") is True
    assert icp.match_criterion({"industry": "Plumbing"}, "industry", "fuzzy_match", "retail") is False
    assert icp.match_criterion({"industry": None}, "industry", "fuzzy_match", "retail") is False


def test_match_numeric_operators(db):
    row = {"team_size": 25}
    assert icp.match_criterion(row, "team_size", "gt", "10") is True
    assert icp.match_criterion(row, "team_size", "gte", "25") is True
    assert icp.match_criterion(row, "team_size", "lt", "10") is False
    assert icp.match_criterion(row, "team_size", "lte", "25") is True
    assert icp.match_criterion(row, "team_size", "eq", "25") is True
    assert icp.match_criterion({"team_size": None}, "team_size", "gt", "10") is False


def test_score_fit_sums_matched_weights(db):
    icp.create_criterion("team_size", "gt", "10", weight=5)
    icp.create_criterion("industry", "fuzzy_match", "retail", weight=3)
    icp.create_criterion("email", "is_not_null", "", weight=2)

    row = {"team_size": 25, "industry": "Retail Centre", "email": None}
    result = icp.score_fit(row)
    assert result["score"] == 8  # team_size + industry match, email doesn't
    assert result["max"] == 10
    assert len(result["matched"]) == 2
    assert len(result["unmatched"]) == 1


def test_score_fit_ignores_inactive_criteria(db):
    icp.create_criterion("team_size", "gt", "10", weight=5, active=False)
    result = icp.score_fit({"team_size": 25})
    assert result["score"] == 0
    assert result["max"] == 0


def test_build_matchable_row():
    partner = {"industry": "Retail", "team_size": 25, "is_company": 1, "email": "a@b.com"}
    deal = {"source": "referral", "value_estimate": 5000, "pain_points": "no crm", "goals": "track leads"}
    row = icp.build_matchable_row(partner, deal)
    assert row["industry"] == "Retail"
    assert row["source"] == "referral"
    assert row["value_estimate"] == 5000


def test_lead_scores_from_own_columns_not_linked_partner():
    partner = {"industry": "Partner Industry", "team_size": 99, "email": "partner@x.example", "is_company": 1}
    lead = {
        "type": "lead",
        "company_name": "Riverstone Dental",
        "contact_name": "",
        "industry": "Dentistry",
        "team_size": 8,
        "email": "pm@riverstone.example",
        "source": "research:directory",
    }
    row = icp.build_matchable_row(partner, lead)
    assert row["industry"] == "Dentistry"
    assert row["team_size"] == 8
    assert row["email"] == "pm@riverstone.example"
    assert row["is_company"] is True
    lead_with_contact = dict(lead, contact_name="Pat Manager")
    assert icp.build_matchable_row(partner, lead_with_contact)["is_company"] is False


def test_tag_rule_stores_normalized_slug(db):
    c, error = icp.create_criterion("company_tag", "has_tag", "Google-Ads", weight=4, label="Ads")
    assert error is None
    assert c["field"] == "company_tag"
    assert c["operator"] == "has_tag"
    assert c["value"] == "google-ads"
    assert c["weight"] == 4


def test_tag_rule_rejects_bad_operator_and_slug(db):
    c, error = icp.create_criterion("company_tag", "eq", "google-ads")
    assert c is None
    assert error == "tag_operator"
    c, error = icp.create_criterion("industry", "has_tag", "google-ads")
    assert c is None
    assert error == "tag_operator"
    c, error = icp.create_criterion("deal_tag", "has_tag", "Not A Tag")
    assert c is None
    assert error == "invalid_value"
    c, error = icp.create_criterion("deal_tag", "has_tag", "campaign:spring")
    assert error is None
    assert c["value"] == "campaign:spring"


def test_has_tag_matches_membership_only():
    row = {"company_tag": ["google-ads", "gtm"], "deal_tag": ["campaign:spring"]}
    assert icp.match_criterion(row, "company_tag", "has_tag", "google-ads") is True
    assert icp.match_criterion(row, "company_tag", "has_tag", "Google-Ads") is True
    assert icp.match_criterion(row, "company_tag", "has_tag", "meta-pixel") is False
    assert icp.match_criterion(row, "deal_tag", "has_tag", "campaign:spring") is True
    assert icp.match_criterion(row, "deal_tag", "has_tag", "google-ads") is False
    assert icp.match_criterion({}, "company_tag", "has_tag", "google-ads") is False


def test_tag_weights_sum_with_other_rules(db):
    icp.create_criterion("company_tag", "has_tag", "google-ads", weight=5)
    icp.create_criterion("deal_tag", "has_tag", "campaign:spring", weight=3)
    icp.create_criterion("team_size", "gt", "10", weight=2)
    icp.create_criterion("company_tag", "has_tag", "call-tracking", weight=9, active=False)
    result = icp.score_fit({
        "company_tag": ["google-ads"],
        "deal_tag": ["campaign:spring"],
        "team_size": 4,
    })
    assert result["score"] == 8
    assert result["max"] == 10


def test_company_tag_uses_company_or_parent_not_the_person(db):
    from app.services.partner_tags import apply_partner_tags
    from app.services.partners import create_partner, get_partner

    company_id = create_partner("Acme Dental", is_company=True)
    person_id = create_partner("Pat Manager", email="pat@acme.example", parent_id=company_id)
    other_person = create_partner("Solo", email="solo@x.example")
    apply_partner_tags(company_id, add=["google-ads", "meta-pixel"])
    apply_partner_tags(person_id, add=["person-only"])
    apply_partner_tags(other_person, add=["google-ads"])

    person = get_partner(person_id)
    company = get_partner(company_id)
    solo = get_partner(other_person)
    deal = {"type": "opportunity", "id": None, "tags": ["campaign:spring"], "source": "referral"}

    person_row = icp.build_matchable_row(person, deal)
    assert person_row["company_tag"] == ["google-ads", "meta-pixel"]
    assert "person-only" not in person_row["company_tag"]
    assert person_row["industry"] == person["industry"]
    assert person_row["deal_tag"] == ["campaign:spring"]

    company_row = icp.build_matchable_row(company, deal)
    assert company_row["company_tag"] == ["google-ads", "meta-pixel"]

    # A person's own tags are not a company, even when the slug matches.
    solo_row = icp.build_matchable_row(solo, {"type": "lead", "tags": []})
    assert solo_row["company_tag"] == []

    # Passed lists are used as-is, including empty, and a lead's profile stays its own.
    lead = {
        "type": "lead",
        "company_name": "Lead Co",
        "industry": "Dentistry",
        "tags": ["region:nsw"],
    }
    overridden = icp.build_matchable_row(
        {"is_company": 1, "industry": "Should not fill the lead", "tags": ["gtm"]},
        lead,
        company_tags=["online-booking"],
    )
    assert overridden["industry"] == "Dentistry"
    assert overridden["company_tag"] == ["online-booking"]
    assert overridden["deal_tag"] == ["region:nsw"]


def test_company_tag_ignores_a_parent_who_is_not_a_company(db):
    from app.services.partner_tags import apply_partner_tags
    from app.services.partners import create_partner, get_partner

    human = create_partner("Not A Company", email="human@x.example")
    child = create_partner("Child", email="child@x.example", parent_id=human)
    apply_partner_tags(human, add=["google-ads"])
    row = icp.build_matchable_row(get_partner(child), {"type": "lead", "tags": []})
    assert row["company_tag"] == []


def test_criterion_display_names_an_unlabeled_tag_rule():
    assert icp.criterion_display({"field": "company_tag", "operator": "has_tag", "value": "google-ads"}) == (
        "Company has tag google-ads"
    )
    assert icp.criterion_display({
        "field": "deal_tag", "operator": "has_tag", "value": "campaign:spring", "label": "Spring",
    }) == "Spring"


def _series(*points):
    """points are (id, key, number, captured_at)."""
    grouped = {}
    for row_id, key, number, captured_at in points:
        grouped.setdefault(key, []).append({
            "id": row_id,
            "metric_key": key,
            "numeric_value": number,
            "captured_at": captured_at,
        })
    return grouped


def test_create_company_metric_rule(db):
    c, error = icp.create_criterion(
        "company_metric", "gte", "4.5",
        metric_key="Google-Rating", weight=5, label="Strong rating",
    )
    assert error is None
    assert c["field"] == "company_metric"
    assert c["metric_key"] == "google-rating"
    assert c["operator"] == "gte"
    assert c["value"] == "4.5"
    assert c["period_days"] is None
    assert c["weight"] == 5

    change, error = icp.create_criterion(
        "company_metric", "gte", "20",
        metric_key="google-review-count", period_days=90, weight=3,
    )
    assert error is None
    assert change["period_days"] == 90
    assert change["value"] == "20"


def test_company_metric_rejects_a_bad_key_operator_or_period(db):
    c, error = icp.create_criterion("company_metric", "gte", "4.5")
    assert c is None
    assert error == "invalid_metric_key"

    c, error = icp.create_criterion(
        "company_metric", "has_tag", "google-rating", metric_key="google-rating",
    )
    assert error == "tag_operator"

    c, error = icp.create_criterion(
        "company_metric", "fuzzy_match", "high", metric_key="google-rating",
    )
    assert error == "metric_operator"

    c, error = icp.create_criterion(
        "industry", "fuzzy_match", "retail", metric_key="google-rating",
    )
    assert error == "metric_fields"

    c, error = icp.create_criterion(
        "company_metric", "gte", "1", metric_key="google-rating", period_days=0,
    )
    assert error == "invalid_period"

    c, error = icp.create_criterion(
        "company_metric", "gte", "not-a-number", metric_key="google-rating",
    )
    assert error == "invalid_value"


def test_update_metric_rule_can_clear_the_window(db):
    c, _ = icp.create_criterion(
        "company_metric", "gte", "20", metric_key="google-review-count", period_days=90,
    )
    updated, error = icp.update_criterion(c["id"], period_days="")
    assert error is None
    assert updated["period_days"] is None
    assert updated["metric_key"] == "google-review-count"

    cleared, error = icp.update_criterion(c["id"], field="team_size", operator="gt", value="10")
    assert error is None
    assert cleared["field"] == "team_size"
    assert cleared["metric_key"] is None
    assert cleared["period_days"] is None


def test_latest_snapshot_matches_numeric_operators():
    from datetime import datetime

    now = datetime(2026, 8, 1)
    row = {"company_metric_series": _series(
        (1, "google-rating", 4.2, "2026-01-01T00:00:00"),
        (2, "google-rating", 4.8, "2026-07-15T00:00:00"),
        (3, "google-review-count", 312, "2026-07-15T00:00:00"),
    )}
    assert icp.match_criterion(
        row, "company_metric", "gte", "4.5", metric_key="google-rating", now=now,
    ) is True
    assert icp.match_criterion(
        row, "company_metric", "lt", "4.5", metric_key="google-rating", now=now,
    ) is False
    assert icp.match_criterion(
        row, "company_metric", "eq", "312", metric_key="google-review-count", now=now,
    ) is True
    # The January 4.2 is history. The latest 4.8 is what the rule sees.
    assert icp.match_criterion(
        row, "company_metric", "lte", "4.2", metric_key="google-rating", now=now,
    ) is False
    assert icp.match_criterion(
        {"company_metric_series": {}}, "company_metric", "gte", "4.5",
        metric_key="google-rating", now=now,
    ) is False
    assert icp.match_criterion(
        row, "company_metric", "gte", "4.5", metric_key="missing-metric", now=now,
    ) is False


def test_text_only_and_future_readings_do_not_hide_the_latest_number():
    from datetime import datetime

    now = datetime(2026, 8, 1)
    row = {"company_metric_series": {
        "google-rating": [
            {"id": 1, "numeric_value": 4.8, "captured_at": "2026-07-01T00:00:00"},
            {"id": 2, "numeric_value": None, "text_value": "listed", "captured_at": "2026-07-20T00:00:00"},
            {"id": 3, "numeric_value": 1.0, "captured_at": "2026-12-01T00:00:00"},
        ],
    }}
    assert icp.match_criterion(
        row, "company_metric", "eq", "4.8", metric_key="google-rating", now=now,
    ) is True


def test_change_over_period_uses_latest_minus_baseline():
    from datetime import datetime, timedelta

    now = datetime(2026, 8, 1, 12, 0, 0)
    baseline_at = (now - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%S")
    inside_at = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
    stale_at = (now - timedelta(days=120)).strftime("%Y-%m-%dT%H:%M:%S")
    row = {"company_metric_series": _series(
        (1, "google-review-count", 312, baseline_at),
        (2, "google-review-count", 340, inside_at),
    )}
    assert icp.match_criterion(
        row, "company_metric", "gte", "20",
        metric_key="google-review-count", period_days=90, now=now,
    ) is True
    assert icp.match_criterion(
        row, "company_metric", "eq", "28",
        metric_key="google-review-count", period_days=90, now=now,
    ) is True
    assert icp.match_criterion(
        row, "company_metric", "gt", "28",
        metric_key="google-review-count", period_days=90, now=now,
    ) is False
    # No reading from the start of the window.
    only_new = {"company_metric_series": _series((2, "google-review-count", 340, inside_at))}
    assert icp.match_criterion(
        only_new, "company_metric", "gte", "1",
        metric_key="google-review-count", period_days=90, now=now,
    ) is False
    # Newest reading is older than the window, so this is not "the last 90 days".
    stale = {"company_metric_series": _series(
        (1, "google-review-count", 100, (now - timedelta(days=200)).strftime("%Y-%m-%dT%H:%M:%S")),
        (2, "google-review-count", 140, stale_at),
    )}
    assert icp.match_criterion(
        stale, "company_metric", "gte", "20",
        metric_key="google-review-count", period_days=90, now=now,
    ) is False


def test_company_metric_scores_the_parent_company_not_the_person(db):
    from datetime import datetime, timedelta

    from app.services.partner_metrics import record_snapshot
    from app.services.partners import create_partner, get_partner

    now = datetime(2026, 8, 1, 12, 0, 0)
    company_id = create_partner("Acme Dental", is_company=True)
    person_id = create_partner("Pat Manager", email="pat@acme.example", parent_id=company_id)
    record_snapshot(
        company_id, "google-rating", "google-maps",
        numeric_value=4.8, captured_at="2026-07-01T00:00:00", now=now,
    )
    record_snapshot(
        company_id, "google-review-count", "google-maps",
        numeric_value=312, captured_at=(now - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%S"), now=now,
    )
    record_snapshot(
        company_id, "google-review-count", "google-maps",
        numeric_value=340, captured_at=(now - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S"), now=now,
    )
    # The person's own rating must not satisfy, or spoil, the company rule.
    record_snapshot(
        person_id, "google-rating", "google-maps",
        numeric_value=1.0, captured_at="2026-07-02T00:00:00", now=now,
    )
    icp.create_criterion("company_metric", "gte", "4.5", metric_key="google-rating", weight=5, label="Rating")
    icp.create_criterion(
        "company_metric", "gte", "20", metric_key="google-review-count",
        period_days=90, weight=3, label="Reviews up",
    )
    icp.create_criterion("company_metric", "gte", "9", metric_key="google-rating", weight=9, label="Impossible", active=False)

    person = get_partner(person_id)
    fit = icp.score_fit(icp.build_matchable_row(person, {"type": "opportunity"}), now=now)
    assert fit["score"] == 8
    assert fit["max"] == 8
    assert [c["label"] for c in fit["matched"]] == ["Rating", "Reviews up"]

    company = get_partner(company_id)
    company_fit = icp.score_fit(icp.build_matchable_row(company, {"type": "opportunity"}), now=now)
    assert company_fit["score"] == 8

    solo_id = create_partner("Solo", email="solo@x.example")
    record_snapshot(solo_id, "google-rating", "google-maps", numeric_value=5, now=now)
    solo = icp.score_fit(icp.build_matchable_row(get_partner(solo_id), {"type": "lead"}), now=now)
    assert solo["score"] == 0

    lead = {"type": "lead", "industry": "Dentistry"}
    assert icp.build_matchable_row(person, lead)["industry"] == "Dentistry"
    lead_fit = icp.score_fit(icp.build_matchable_row(person, lead), now=now)
    assert lead_fit["score"] == 8


def test_criterion_display_names_a_metric_rule():
    assert icp.criterion_display({
        "field": "company_metric", "operator": "gte", "value": "4.5", "metric_key": "google-rating",
    }) == "google-rating >= 4.5"
    assert icp.criterion_display({
        "field": "company_metric", "operator": "gte", "value": "20",
        "metric_key": "google-review-count", "period_days": 90, "label": "Reviews up",
    }) == "Reviews up"
