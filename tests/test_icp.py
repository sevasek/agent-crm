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
