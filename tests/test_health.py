def test_health_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_health_reports_low_disk_space(client, monkeypatch):
    from app import main

    monkeypatch.setattr(main, "HEALTH_MIN_FREE_MB", 10**12)  # nothing has this much free
    resp = client.get("/health")
    assert resp.status_code == 503
    assert resp.json() == {"status": "low_disk_space"}


def test_health_ignores_disk_check_error(client, monkeypatch):
    from app import main

    monkeypatch.setattr(main.shutil, "disk_usage", lambda _path: (_ for _ in ()).throw(OSError("no such device")))
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
