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
    assert icp.match_criterion({"industry": "Childcare"}, "industry", "is_not_null", "") is True
    assert icp.match_criterion({"industry": ""}, "industry", "is_not_null", "") is False
    assert icp.match_criterion({}, "industry", "is_not_null", "") is False


def test_match_boolean(db):
    assert icp.match_criterion({"is_company": 1}, "is_company", "boolean", "true") is True
    assert icp.match_criterion({"is_company": 0}, "is_company", "boolean", "true") is False
    assert icp.match_criterion({"is_company": 0}, "is_company", "boolean", "false") is True


def test_match_fuzzy(db):
    assert icp.match_criterion({"industry": "Childcare Centre"}, "industry", "fuzzy_match", "childcare") is True
    assert icp.match_criterion({"industry": "Chyldkare"}, "industry", "fuzzy_match", "childcare") is True
    assert icp.match_criterion({"industry": "Plumbing"}, "industry", "fuzzy_match", "childcare") is False
    assert icp.match_criterion({"industry": None}, "industry", "fuzzy_match", "childcare") is False


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
    icp.create_criterion("industry", "fuzzy_match", "childcare", weight=3)
    icp.create_criterion("email", "is_not_null", "", weight=2)

    row = {"team_size": 25, "industry": "Childcare Centre", "email": None}
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
    partner = {"industry": "Childcare", "team_size": 25, "is_company": 1, "email": "a@b.com"}
    deal = {"source": "referral", "value_estimate": 5000, "pain_points": "no crm", "goals": "track leads"}
    row = icp.build_matchable_row(partner, deal)
    assert row["industry"] == "Childcare"
    assert row["source"] == "referral"
    assert row["value_estimate"] == 5000
