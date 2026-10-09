"""Ingest defaults to a lead. type=opportunity keeps the partner-and-deal path."""
from app.database import get_db
from app.services.catalog import create_service
from app.services.client_import import import_client
from app.services.deals import get_deal
from app.services.leads import ingest_lead
from app.services.partners import create_partner, get_partner, get_partner_by_email


def test_omitted_type_creates_a_lead_and_deprecation_header(client, db, monkeypatch):
    monkeypatch.setenv("CRM_API_KEY", "test-crm-api-key")
    resp = client.post("/api/v1/leads", json={"leads": [{
        "company_name": "Harbour Physio",
        "website": "https://harbour.example",
    }]}, headers={"X-API-Key": "test-crm-api-key"})
    assert resp.status_code == 200
    assert resp.headers["deprecation"] == "type omitted; default is now lead"
    item = resp.json()["results"][0]
    assert item["status"] == "created"
    assert item["type"] == "lead"
    assert item["type_defaulted"] is True
    assert item["lead_id"] == item["deal_id"]
    assert item["partner_id"] is None
    assert get_partner_by_email("pm@harbour.example") is None
    assert get_deal(item["deal_id"])["type"] == "lead"


def test_explicit_opportunity_still_creates_partner_and_deal(db):
    create_service("Consulting", "consulting")
    result = ingest_lead({
        "type": "opportunity",
        "name": "Jane Doe",
        "email": "jane@acme.example",
        "service_slug": "consulting",
    })
    assert result["status"] == "created"
    assert result["type"] == "opportunity"
    assert result["lead_id"] is None
    assert result["partner_id"]
    assert get_partner_by_email("jane@acme.example")["name"] == "Jane Doe"


def test_weak_keys_do_not_auto_merge(db):
    ingest_lead({"company_name": "Harbour", "phone": "0400111222", "website": "https://facebook.com/harbour"})
    second = ingest_lead({"company_name": "Other Clinic", "phone": "0400111222"})
    assert second["status"] == "created"
    assert second["deal_id"] != None
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM deals WHERE type = 'lead'").fetchone()["n"] == 2


def test_two_strong_matches_keep_the_oldest(db):
    first = ingest_lead({"company_name": "Harbour", "email": "pm@h.example"})
    with get_db() as conn:
        conn.execute(
            """INSERT INTO deals (stage, type, email, company_name, active, created_at)
               VALUES ('new', 'lead', 'pm@h.example', 'Later', 1, '2099-01-01')"""
        )
        conn.commit()
        later_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    third = ingest_lead({
        "company_name": "Again",
        "email": "pm@h.example",
        "website": "https://later.example",
    })
    assert third["status"] == "duplicate_open_lead"
    assert third["deal_id"] == first["deal_id"]
    assert later_id in third["also_matched"]
    assert get_deal(first["deal_id"])["website"] == "https://later.example"


def test_single_partner_email_links_without_editing_the_partner(db):
    partner_id = create_partner("Known", email="known@h.example", phone="0400000000")
    result = ingest_lead({"company_name": "Known Co", "email": "known@h.example"})
    assert result["status"] == "created"
    assert result["partner_id"] == partner_id
    partner = get_partner(partner_id)
    assert partner["phone"] == "0400000000"
    assert partner["name"] == "Known"


def test_client_import_lead_flag_skips_partners(db):
    with get_db() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM partners").fetchone()["n"]
    result = import_client({
        "company": "Harbour Physio",
        "name": "Pat",
        "email": "pat@h.example",
    }, record_type="lead")
    assert result["status"] == "ok"
    assert result["type"] == "lead"
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM partners").fetchone()["n"] == count
    default = import_client({
        "company": "Acme",
        "name": "Jane",
        "email": "jane-import@acme.example",
    })
    assert default["partner_id"]
    assert get_partner(default["partner_id"]) is not None
