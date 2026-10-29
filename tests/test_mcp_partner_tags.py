"""Partner-tag MCP tools and ICP tag rules over MCP."""
from app.services.partner_tags import tags_for_partner
from app.services.partners import create_partner
from tests.test_mcp_tools import _enable, call


def test_partner_tag_tools_add_list_remove_and_filter(client, db, monkeypatch):
    _enable(monkeypatch)
    company_id = create_partner("Acme Dental", is_company=True, website="https://acme.example")
    person_id = create_partner("Pat Manager", email="pat@acme.example", parent_id=company_id)
    other_id = create_partner("Other Co", is_company=True, email="other@x.example")

    empty, is_error = call(client, "list_partner_tags", {"partner_id": company_id})
    assert is_error is False
    assert empty["tags"] == []

    added, is_error = call(client, "update_partner_tags", {
        "partner_id": company_id,
        "add_tags": ["Google-Ads", "meta-pixel"],
    })
    assert is_error is False
    assert added["tags"] == ["google-ads", "meta-pixel"]
    assert tags_for_partner(company_id) == ["google-ads", "meta-pixel"]

    listed, _ = call(client, "list_partner_tags", {"partner_id": company_id})
    assert listed["tags"] == ["google-ads", "meta-pixel"]

    removed, _ = call(client, "update_partner_tags", {
        "partner_id": company_id,
        "remove_tags": ["meta-pixel"],
        "add_tags": ["gtm"],
    })
    assert removed["tags"] == ["google-ads", "gtm"]

    replaced, _ = call(client, "update_partner_tags", {
        "partner_id": company_id,
        "tags": ["google-ads", "analytics"],
        "add_tags": ["call-tracking"],
        "remove_tags": ["analytics"],
    })
    assert replaced["tags"] == ["call-tracking", "google-ads"]

    bad, is_error = call(client, "update_partner_tags", {
        "partner_id": company_id,
        "add_tags": ["Not A Tag"],
    })
    assert is_error is True
    assert bad["error"] == "invalid_tag"
    assert tags_for_partner(company_id) == ["call-tracking", "google-ads"]

    missing, is_error = call(client, "update_partner_tags", {"partner_id": company_id})
    assert is_error is True
    assert missing["error"] == "tags_required"

    gone, is_error = call(client, "list_partner_tags", {"partner_id": 99999})
    assert is_error is True
    assert gone["error"] == "not_found"

    found, _ = call(client, "search_partners", {"tags": ["google-ads"]})
    assert [row["id"] for row in found["partners"]] == [company_id]
    assert found["partners"][0]["tags"] == ["call-tracking", "google-ads"]
    assert found["tags"] == ["google-ads"]

    both, _ = call(client, "search_partners", {
        "query": "acme",
        "tag": "google-ads",
        "tags": ["call-tracking"],
    })
    assert [row["id"] for row in both["partners"]] == [company_id]

    # The person's own tags are what search filters on, not the parent company's.
    people, _ = call(client, "search_partners", {"query": "Pat", "tags": ["google-ads"]})
    assert people["partners"] == []

    call(client, "update_partner_tags", {"partner_id": person_id, "add_tags": ["person-only"]})
    got, _ = call(client, "get_partner", {"partner_id": person_id})
    assert got["partner"]["tags"] == ["person-only"]
    assert got["company"]["id"] == company_id
    assert got["company"]["tags"] == ["call-tracking", "google-ads"]

    invalid, is_error = call(client, "search_partners", {"tag": "Not A Tag"})
    assert is_error is True
    assert invalid["error"] == "invalid_tag"

    other, _ = call(client, "search_partners", {"query": "Other"})
    assert other["partners"][0]["id"] == other_id
    assert other["partners"][0]["tags"] == []


def test_create_tag_rule_over_mcp_feeds_fit(client, db, monkeypatch):
    from app.services.catalog import create_service
    from app.services.deals import create_deal
    from app.services.partner_tags import apply_partner_tags

    _enable(monkeypatch)
    catalog, is_error = call(client, "list_catalog")
    assert is_error is False
    fields = {row["key"] for row in catalog["icp_fields"]}
    operators = {row["key"] for row in catalog["icp_operators"]}
    assert {"company_tag", "deal_tag", "industry"} <= fields
    assert "has_tag" in operators

    created, is_error = call(client, "create_icp_criterion", {
        "field": "company_tag",
        "operator": "has_tag",
        "value": "Google-Ads",
        "weight": 5,
        "label": "Runs Google Ads",
    })
    assert is_error is False
    assert created["criterion"]["value"] == "google-ads"
    assert created["criterion"]["weight"] == 5
    criterion_id = created["criterion"]["id"]

    deal_rule, _ = call(client, "create_icp_criterion", {
        "field": "deal_tag",
        "operator": "has_tag",
        "value": "campaign:spring",
        "weight": 3,
        "label": "Spring campaign",
    })
    assert deal_rule["criterion"]["value"] == "campaign:spring"

    rejected, is_error = call(client, "create_icp_criterion", {
        "field": "industry",
        "operator": "has_tag",
        "value": "google-ads",
    })
    assert is_error is True
    assert rejected["error"] == "tag_operator"

    company_id = create_partner("Acme Dental", is_company=True)
    person_id = create_partner("Pat Manager", email="pat@acme.example", parent_id=company_id)
    apply_partner_tags(company_id, add=["google-ads"])
    apply_partner_tags(person_id, add=["person-only"])
    service_id = create_service("Consulting", "consulting-tags")
    deal_id = create_deal(person_id, service_id, tags=["campaign:spring"])

    got, is_error = call(client, "get_deal", {"deal_id": deal_id})
    assert is_error is False
    assert got["icp"]["score"] == 8
    assert got["icp"]["max"] == 8
    assert "Runs Google Ads" in got["icp"]["matched"]
    assert "Spring campaign" in got["icp"]["matched"]

    updated, _ = call(client, "update_icp_criterion", {
        "criterion_id": criterion_id,
        "weight": 7,
        "active": False,
    })
    assert updated["criterion"]["weight"] == 7
    assert updated["criterion"]["active"] is False
    rescored, _ = call(client, "get_deal", {"deal_id": deal_id})
    assert rescored["icp"]["score"] == 3

    listed, _ = call(client, "list_icp_criteria", {"active_only": True})
    assert [row["id"] for row in listed["criteria"]] == [deal_rule["criterion"]["id"]]

    deleted, _ = call(client, "delete_icp_criterion", {"criterion_id": criterion_id})
    assert deleted["deleted"] is True
    remaining, _ = call(client, "list_icp_criteria")
    assert criterion_id not in [row["id"] for row in remaining["criteria"]]
