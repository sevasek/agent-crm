DEFAULT_PIPELINE_KEYS = ["new", "contacted", "qualified", "nurture", "proposal", "won", "lost"]


def _headers(key="test-crm-stages-api-key"):
    return {"X-API-Key": key}


def _set_key(monkeypatch, key="test-crm-stages-api-key"):
    monkeypatch.setenv("CRM_STAGES_API_KEY", key)


def test_list_stages_requires_api_key(client, db):
    resp = client.get("/api/v1/stages")
    assert resp.status_code == 401


def test_leads_api_key_alone_does_not_authorize_stages(client, db, monkeypatch):
    # CRM_API_KEY is the ingest-only secret — stages is a separate,
    # deliberately not-inherited key.
    monkeypatch.setenv("CRM_API_KEY", "test-leads-key")
    resp = client.get("/api/v1/stages", headers=_headers("test-leads-key"))
    assert resp.status_code == 401


def test_list_stages_returns_default_pipeline(client, db, monkeypatch):
    _set_key(monkeypatch)
    resp = client.get("/api/v1/stages", headers=_headers())
    assert resp.status_code == 200
    stages = resp.json()["stages"]
    assert [s["key"] for s in stages] == DEFAULT_PIPELINE_KEYS
    qualified = next(s for s in stages if s["key"] == "qualified")
    assert qualified["is_qualified_pool"] is True
    assert qualified["is_won"] is False


def test_get_single_stage(client, db, monkeypatch):
    _set_key(monkeypatch)
    resp = client.get("/api/v1/stages/won", headers=_headers())
    assert resp.status_code == 200
    assert resp.json()["stage"]["is_won"] is True


def test_get_missing_stage_404(client, db, monkeypatch):
    _set_key(monkeypatch)
    resp = client.get("/api/v1/stages/nope", headers=_headers())
    assert resp.status_code == 404


def test_create_stage_via_api(client, db, monkeypatch):
    _set_key(monkeypatch)
    resp = client.post("/api/v1/stages", json={
        "key": "demo-scheduled", "label": "Demo scheduled", "is_qualified_pool": True,
    }, headers=_headers())
    assert resp.status_code == 201
    stage = resp.json()["stage"]
    assert stage["key"] == "demo-scheduled"
    assert stage["is_qualified_pool"] is True

    resp = client.get("/api/v1/stages", headers=_headers())
    keys = [s["key"] for s in resp.json()["stages"]]
    assert "demo-scheduled" in keys


def test_create_stage_duplicate_key_422(client, db, monkeypatch):
    _set_key(monkeypatch)
    resp = client.post("/api/v1/stages", json={"key": "new", "label": "Duplicate"}, headers=_headers())
    assert resp.status_code == 422
    assert resp.json()["error"] == "duplicate_key"


def test_create_stage_requires_api_key(client, db):
    resp = client.post("/api/v1/stages", json={"key": "demo", "label": "Demo"})
    assert resp.status_code == 401


def test_create_stage_missing_fields_422(client, db, monkeypatch):
    _set_key(monkeypatch)
    resp = client.post("/api/v1/stages", json={"key": "demo"}, headers=_headers())
    assert resp.status_code == 422
    assert resp.json()["error"] == "key_and_label_required"


def test_create_stage_won_and_lost_conflict_422(client, db, monkeypatch):
    _set_key(monkeypatch)
    resp = client.post("/api/v1/stages", json={
        "key": "demo", "label": "Demo", "is_won": True, "is_lost": True,
    }, headers=_headers())
    assert resp.status_code == 422
    assert resp.json()["error"] == "won_lost_conflict"


def test_create_stage_nan_position_rejected(client, db, monkeypatch):
    _set_key(monkeypatch)
    # httpx's own JSON encoder rejects NaN/Infinity before it can be sent, so
    # send the raw (Python's json module parses these as an extension, same
    # as the code under test) body directly rather than via json=.
    resp = client.post(
        "/api/v1/stages",
        content=b'{"key": "demo", "label": "Demo", "position": NaN}',
        headers={**_headers(), "Content-Type": "application/json"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"] == "invalid_position"


def test_create_stage_infinite_position_rejected(client, db, monkeypatch):
    _set_key(monkeypatch)
    resp = client.post(
        "/api/v1/stages",
        content=b'{"key": "demo", "label": "Demo", "position": Infinity}',
        headers={**_headers(), "Content-Type": "application/json"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"] == "invalid_position"


def test_create_stage_out_of_sqlite_range_position_rejected(client, db, monkeypatch):
    # Python ints are unbounded; SQLite INTEGER is signed 64-bit. Binding
    # 2**63 would raise OverflowError on INSERT if this weren't clamped.
    _set_key(monkeypatch)
    resp = client.post("/api/v1/stages", json={
        "key": "demo", "label": "Demo", "position": 2 ** 63,
    }, headers=_headers())
    assert resp.status_code == 422
    assert resp.json()["error"] == "invalid_position"


def test_create_stage_string_bool_rejected(client, db, monkeypatch):
    _set_key(monkeypatch)
    # Python's bool("false") is True — a JSON string must not silently flip
    # a role on when the caller meant "off".
    resp = client.post("/api/v1/stages", json={
        "key": "demo", "label": "Demo", "is_won": "false",
    }, headers=_headers())
    assert resp.status_code == 422
    assert resp.json()["error"] == "invalid_is_won"


def test_update_stage_label_and_roles(client, db, monkeypatch):
    _set_key(monkeypatch)
    resp = client.patch("/api/v1/stages/proposal", json={
        "label": "Demo booked", "is_qualified_pool": True,
    }, headers=_headers())
    assert resp.status_code == 200
    stage = resp.json()["stage"]
    assert stage["label"] == "Demo booked"
    assert stage["is_qualified_pool"] is True
    assert stage["key"] == "proposal"


def test_update_stage_creating_won_lost_conflict_422(client, db, monkeypatch):
    _set_key(monkeypatch)
    resp = client.patch("/api/v1/stages/won", json={"is_lost": True}, headers=_headers())
    assert resp.status_code == 422
    assert resp.json()["error"] == "won_lost_conflict"


def test_update_missing_stage_404(client, db, monkeypatch):
    _set_key(monkeypatch)
    resp = client.patch("/api/v1/stages/nope", json={"label": "x"}, headers=_headers())
    assert resp.status_code == 404


def test_delete_stage_success(client, db, monkeypatch):
    _set_key(monkeypatch)
    resp = client.delete("/api/v1/stages/proposal", headers=_headers())
    assert resp.status_code == 204
    resp = client.get("/api/v1/stages/proposal", headers=_headers())
    assert resp.status_code == 404


def test_delete_stage_in_use_409(client, db, monkeypatch):
    from app.services.partners import create_partner
    from app.services.catalog import create_service
    from app.services.deals import create_deal

    _set_key(monkeypatch)
    pid = create_partner("Jane Doe", email="jane@x.example")
    sid = create_service("Consulting", "consulting")
    create_deal(pid, sid)  # lands in "new"

    resp = client.delete("/api/v1/stages/new", headers=_headers())
    assert resp.status_code == 409
    assert resp.json()["error"] == "stage_in_use"


def test_reorder_stages_via_api(client, db, monkeypatch):
    _set_key(monkeypatch)
    new_order = list(reversed(DEFAULT_PIPELINE_KEYS))
    resp = client.post("/api/v1/stages/reorder", json={"order": new_order}, headers=_headers())
    assert resp.status_code == 200
    assert [s["key"] for s in resp.json()["stages"]] == new_order


def test_reorder_stages_invalid_set_422(client, db, monkeypatch):
    _set_key(monkeypatch)
    resp = client.post("/api/v1/stages/reorder", json={"order": ["new", "contacted"]}, headers=_headers())
    assert resp.status_code == 422
    assert resp.json()["error"] == "key_set_mismatch"


def test_wrong_api_key_rejected_on_mutating_endpoints(client, db, monkeypatch):
    _set_key(monkeypatch)
    resp = client.post("/api/v1/stages", json={"key": "demo", "label": "Demo"}, headers=_headers("wrong-key"))
    assert resp.status_code == 401


def test_query_string_stages_key_does_not_authenticate(client, db, monkeypatch):
    _set_key(monkeypatch)
    resp = client.get("/api/v1/stages", params={"X-API-Key": "test-crm-stages-api-key"})
    assert resp.status_code == 401


def test_leads_key_does_not_authenticate_stages_post(client, db, monkeypatch):
    monkeypatch.setenv("CRM_API_KEY", "test-leads-key")
    monkeypatch.setenv("CRM_STAGES_API_KEY", "test-stages-key")
    resp = client.post(
        "/api/v1/stages",
        json={"key": "demo", "label": "Demo"},
        headers=_headers("test-leads-key"),
    )
    assert resp.status_code == 401


def test_unset_stages_key_fails_closed(client, db, monkeypatch):
    monkeypatch.delenv("CRM_STAGES_API_KEY", raising=False)
    resp = client.get("/api/v1/stages", headers=_headers("anything"))
    assert resp.status_code == 401
