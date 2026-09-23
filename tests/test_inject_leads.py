import importlib.util
import io
import json
from pathlib import Path
from urllib.error import HTTPError, URLError

from app.services.catalog import create_service
from app.services.partners import get_partner_by_email, get_company_by_name, list_partners
from app.services.deals import list_deals

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "tests" / "fixtures" / "leads" / "example.json"
SCRIPT = REPO / "scripts" / "inject_leads.py"


def load_cli():
    spec = importlib.util.spec_from_file_location("inject_leads", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CLI = load_cli()


def test_fixture_matches_part_a_shape():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(payload["leads"], list) and payload["leads"]
    lead = payload["leads"][0]
    for key in (
        "name", "email", "company_name", "service_slug", "phone", "title",
        "source", "value_estimate", "pain_points", "goals",
    ):
        assert key in lead
    assert lead["name"] == "Jane Doe"
    assert lead["company_name"] == "Acme Childcare"
    assert lead["service_slug"] == "consulting"


def test_dry_run_fixture_prints_fields_and_writes_nothing(db, capsys):
    code = CLI.run(["--dry-run", str(FIXTURE)])
    captured = capsys.readouterr()

    assert code == 0
    assert "Jane Doe" in captured.out
    assert "jane@acme.example" in captured.out
    assert "consulting" in captured.out
    assert "dry-run" in captured.out
    assert list_partners() == []


def test_in_process_inject_against_db_fixture(db, capsys):
    create_service("Consulting", "consulting")

    code = CLI.run([str(FIXTURE)])
    captured = capsys.readouterr()

    assert code == 0
    assert "created" in captured.out

    partner = get_partner_by_email("jane@acme.example")
    assert partner is not None
    assert partner["name"] == "Jane Doe"
    company = get_company_by_name("Acme Childcare")
    assert company is not None
    assert partner["parent_id"] == company["id"]
    deals = list_deals(partner_id=partner["id"])
    assert len(deals) == 1
    assert deals[0]["pain_points"].startswith("No visibility on business processes")


def test_in_process_duplicate_exits_zero(db, capsys):
    create_service("Consulting", "consulting")
    assert CLI.run([str(FIXTURE)]) == 0
    capsys.readouterr()

    code = CLI.run([str(FIXTURE)])
    captured = capsys.readouterr()
    assert code == 0
    assert "duplicate_open_deal" in captured.out
    partner = get_partner_by_email("jane@acme.example")
    assert len(list_deals(partner_id=partner["id"])) == 1


def test_in_process_non_string_email_is_coerced(db, tmp_path, capsys):
    """Numeric email is stringified by sanitize_lead_payload; the rest of the file still ingests."""
    create_service("Consulting", "consulting")
    payload = tmp_path / "mixed.json"
    payload.write_text(json.dumps({
        "leads": [
            {"name": "Bad", "email": 12345, "service_slug": "consulting"},
            {
                "name": "Jane Doe",
                "email": "jane@acme.example",
                "service_slug": "consulting",
            },
        ]
    }), encoding="utf-8")

    code = CLI.run([str(payload)])
    captured = capsys.readouterr()

    assert code == 0
    assert "created" in captured.out
    assert "Traceback" not in captured.err
    assert get_partner_by_email("12345") is not None
    assert get_partner_by_email("jane@acme.example") is not None


def test_in_process_invalid_item_does_not_sink_batch(db, tmp_path, capsys):
    """A nested name object is invalid; later leads in the same file still ingest."""
    create_service("Consulting", "consulting")
    payload = tmp_path / "mixed.json"
    payload.write_text(json.dumps({
        "leads": [
            {
                "name": {"first": "Nope"},
                "email": "bad@example.com",
                "service_slug": "consulting",
            },
            {
                "name": "Jane Doe",
                "email": "jane@acme.example",
                "service_slug": "consulting",
            },
        ]
    }), encoding="utf-8")

    code = CLI.run([str(payload)])
    captured = capsys.readouterr()

    assert code == 1
    assert "invalid" in captured.out
    assert "created" in captured.out
    assert "Traceback" not in captured.err
    assert get_partner_by_email("bad@example.com") is None
    assert get_partner_by_email("jane@acme.example") is not None


def test_in_process_ingest_exception_does_not_sink_batch(db, tmp_path, capsys, monkeypatch):
    """An unexpected ingest_lead raise is recorded as invalid and does not abort the file."""
    create_service("Consulting", "consulting")
    payload = tmp_path / "mixed.json"
    payload.write_text(json.dumps({
        "leads": [
            {
                "name": "Boom",
                "email": "explode@example.com",
                "service_slug": "consulting",
            },
            {
                "name": "Jane Doe",
                "email": "jane@acme.example",
                "service_slug": "consulting",
            },
        ]
    }), encoding="utf-8")

    original = CLI.ingest_lead

    def boom(lead):
        if isinstance(lead, dict) and lead.get("email") == "explode@example.com":
            raise RuntimeError("boom")
        return original(lead)

    monkeypatch.setattr(CLI, "ingest_lead", boom)

    code = CLI.run([str(payload)])
    captured = capsys.readouterr()

    assert code == 1
    assert "invalid" in captured.out
    assert "created" in captured.out
    assert "Traceback" not in captured.err
    assert get_partner_by_email("explode@example.com") is None
    assert get_partner_by_email("jane@acme.example") is not None


def test_mixed_results_exits_one():
    assert CLI.exit_code_for_results([
        {"email": "ok@example.com", "status": "created"},
        {"email": "bad@example.com", "status": "invalid_service"},
    ]) == 1
    assert CLI.exit_code_for_results([
        {"email": "ok@example.com", "status": "created"},
        {"email": "dup@example.com", "status": "duplicate_open_deal"},
    ]) == 0


def test_in_process_unknown_service_exits_one(db, capsys):
    code = CLI.run([str(FIXTURE)])
    captured = capsys.readouterr()
    assert code == 1
    assert "invalid_service" in captured.out
    assert get_partner_by_email("jane@acme.example") is None


def test_no_files_exits_one(capsys):
    code = CLI.run([])
    captured = capsys.readouterr()
    assert code == 1
    assert "no files" in captured.err


def test_missing_file_exits_one(capsys):
    code = CLI.run(["/tmp/does-not-exist-leads.json"])
    captured = capsys.readouterr()
    assert code == 1
    assert "file not found" in captured.err


def test_empty_leads_list_exits_one(tmp_path, capsys):
    empty = tmp_path / "empty.json"
    empty.write_text('{"leads": []}', encoding="utf-8")
    code = CLI.run([str(empty)])
    captured = capsys.readouterr()
    assert code == 1
    assert "empty leads list" in captured.err


def test_invalid_json_exits_one(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    code = CLI.run([str(bad)])
    assert code == 1
    assert "invalid JSON" in capsys.readouterr().err


def test_http_401_exits_one(monkeypatch, capsys):
    monkeypatch.setenv("CRM_API_KEY", "wrong-key")

    def fake_urlopen(request, timeout=60):
        raise HTTPError(request.full_url, 401, "Unauthorized", hdrs=None, fp=io.BytesIO(b'{"error":"invalid_api_key"}'))

    monkeypatch.setattr(CLI.urllib.request, "urlopen", fake_urlopen)
    code = CLI.run(["--http", "http://127.0.0.1:8000", str(FIXTURE)])
    captured = capsys.readouterr()
    assert code == 1
    assert "invalid_api_key" in captured.err or "401" in captured.err


def test_http_unset_api_key_exits_one(monkeypatch, capsys):
    monkeypatch.delenv("CRM_API_KEY", raising=False)
    code = CLI.run(["--http", "http://127.0.0.1:8000", str(FIXTURE)])
    captured = capsys.readouterr()
    assert code == 1
    assert "CRM_API_KEY" in captured.err


def test_http_200_created_exits_zero(monkeypatch, capsys):
    monkeypatch.setenv("CRM_API_KEY", "test-crm-api-key")

    class FakeResponse:
        status = 200

        def read(self):
            return b'{"results":[{"email":"jane@acme.example","status":"created"}]}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    posted = {}

    def fake_urlopen(request, timeout=60):
        posted["url"] = request.full_url
        posted["method"] = request.get_method()
        posted["api_key"] = request.headers.get("X-api-key") or request.get_header("X-api-key")
        posted["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr(CLI.urllib.request, "urlopen", fake_urlopen)
    code = CLI.run(["--http", "http://127.0.0.1:8000", str(FIXTURE)])
    captured = capsys.readouterr()

    assert code == 0
    assert "created" in captured.out
    assert posted["url"] == "http://127.0.0.1:8000/api/v1/leads"
    assert posted["method"] == "POST"
    assert posted["api_key"] == "test-crm-api-key"
    assert posted["body"]["leads"][0]["email"] == "jane@acme.example"


def test_http_all_invalid_exits_one(monkeypatch, capsys):
    monkeypatch.setenv("CRM_API_KEY", "test-crm-api-key")

    class FakeResponse:
        status = 200

        def read(self):
            return b'{"results":[{"email":"jane@acme.example","status":"invalid_service"}]}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(CLI.urllib.request, "urlopen", lambda request, timeout=60: FakeResponse())
    code = CLI.run(["--http", "http://localhost:8000", str(FIXTURE)])
    assert code == 1
    assert "invalid_service" in capsys.readouterr().out


def test_http_network_error_exits_one(monkeypatch, capsys):
    monkeypatch.setenv("CRM_API_KEY", "test-crm-api-key")
    monkeypatch.setattr(
        CLI.urllib.request, "urlopen",
        lambda request, timeout=60: (_ for _ in ()).throw(URLError("connection refused")),
    )
    code = CLI.run(["--http", "http://127.0.0.1:8000", str(FIXTURE)])
    assert code == 1
    assert "failed" in capsys.readouterr().err.lower()


def test_dry_run_skips_http(monkeypatch, capsys):
    called = []
    monkeypatch.setenv("CRM_API_KEY", "test-crm-api-key")
    monkeypatch.setattr(
        CLI.urllib.request, "urlopen",
        lambda request, timeout=60: called.append(1),
    )
    code = CLI.run(["--dry-run", "--http", "http://127.0.0.1:8000", str(FIXTURE)])
    assert code == 0
    assert called == []
    assert "dry-run" in capsys.readouterr().out
