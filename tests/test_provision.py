"""Port allocation and instance .env generation — no Docker required."""
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

from scripts.instance_lib import (
    ProvisionError,
    allocate_port,
    generate_secret,
    render_env,
    validate_name,
    write_env_file,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_validate_name_rejects_unsafe():
    with pytest.raises(ProvisionError):
        validate_name("../etc")
    with pytest.raises(ProvisionError):
        validate_name("Has Caps")
    assert validate_name("Clinic-West") == "clinic-west"


def test_allocate_port_increments_and_reuses(tmp_path):
    registry = tmp_path / "ports.tsv"
    p1 = allocate_port(registry, "acme", start=8000, check_bind=False)
    p2 = allocate_port(registry, "beta", start=8000, check_bind=False)
    p1_again = allocate_port(registry, "acme", start=8000, check_bind=False)
    assert p1 == 8000
    assert p2 == 8001
    assert p1_again == 8000
    text = registry.read_text()
    assert "acme\t8000" in text
    assert "beta\t8001" in text


def test_allocate_port_skips_in_use(tmp_path, monkeypatch):
    registry = tmp_path / "ports.tsv"
    monkeypatch.setattr("scripts.instance_lib.port_in_use", lambda port, host="127.0.0.1": port == 8000)
    port = allocate_port(registry, "acme", start=8000, check_bind=True)
    assert port == 8001


def test_write_env_is_mode_600_with_fresh_secrets(tmp_path):
    path = tmp_path / "acme" / ".env"
    text = render_env(
        name="acme",
        tz="Australia/Sydney",
        email="ops@example.com",
        port=8002,
        trusted_proxies="172.16.0.0/12",
    )
    write_env_file(path, text)
    mode = path.stat().st_mode
    assert mode & 0o777 == 0o600
    body = path.read_text()
    assert "CRM_PORT=8002" in body
    assert "COMPOSE_PROJECT_NAME=crm-acme" in body
    assert "TZ=Australia/Sydney" in body
    assert "BOOTSTRAP_ADMIN_EMAIL=ops@example.com" in body
    assert "BOOTSTRAP_ADMIN_PASSWORD" not in body
    assert "TRUSTED_PROXIES=172.16.0.0/12" in body
    secret = re.search(r"^SECRET_KEY=([0-9a-f]+)$", body, re.M).group(1)
    assert len(secret) == 64
    assert generate_secret() != secret


def test_write_env_refuses_overwrite(tmp_path):
    path = tmp_path / ".env"
    write_env_file(path, "SECRET_KEY=one\n")
    with pytest.raises(FileExistsError):
        write_env_file(path, "SECRET_KEY=two\n")
    assert path.read_text() == "SECRET_KEY=one\n"


def test_new_instance_sh_dry_run(tmp_path):
    env = {
        **os.environ,
        "INSTANCE_ROOT": str(tmp_path),
        "INSTANCE_REGISTRY": str(tmp_path / "ports.tsv"),
        "TRUSTED_PROXIES": "172.16.0.0/12",
    }
    proc = subprocess.run(
        [
            "bash",
            str(REPO_ROOT / "scripts" / "new-instance.sh"),
            "--dry-run",
            "acme",
            "Australia/Sydney",
            "ops@example.com",
        ],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    env_path = tmp_path / "acme" / ".env"
    assert env_path.is_file()
    assert env_path.stat().st_mode & stat.S_IRWXO == 0
    assert env_path.stat().st_mode & 0o777 == 0o600
    body = env_path.read_text()
    assert "CRM_PORT=8000" in body
    assert "COMPOSE_PROJECT_NAME=crm-acme" in body
    assert "TZ=Australia/Sydney" in body
    assert "BOOTSTRAP_ADMIN_PASSWORD" not in body
    assert (tmp_path / "ports.tsv").read_text().startswith("acme\t8000")
    assert "reverse_proxy 127.0.0.1:8000" in proc.stdout
    assert "Dry-run" in proc.stdout
