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
    assert_unique_secret_key,
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
    assert not re.search(r"^BOOTSTRAP_ADMIN_PASSWORD=", body, re.M)
    assert "TRUSTED_PROXIES=172.16.0.0/12" in body
    secret = re.search(r"^SECRET_KEY=([0-9a-f]+)$", body, re.M).group(1)
    assert len(secret) == 64
    assert generate_secret() != secret


def test_assert_unique_secret_key_rejects_copied_env(tmp_path):
    write_env_file(tmp_path / "acme" / ".env", "SECRET_KEY=copied-from-acme\n")
    with pytest.raises(ProvisionError, match="acme"):
        assert_unique_secret_key(tmp_path, "beta", "copied-from-acme")
    assert_unique_secret_key(tmp_path, "acme", "copied-from-acme")
    assert_unique_secret_key(tmp_path, "beta", "a-different-key")


def test_assert_unique_secret_key_rejects_example_default():
    with pytest.raises(ProvisionError, match="env.example"):
        assert_unique_secret_key("/tmp", "acme", "dev-secret-change-in-prod")


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
        "APP_UID": str(os.getuid()),
        "APP_GID": str(os.getgid()),
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
    data_dir = tmp_path / "acme" / "data"
    assert data_dir.is_dir()
    assert data_dir.stat().st_mode & 0o777 == 0o700
    assert data_dir.stat().st_uid == os.getuid()
    assert data_dir.stat().st_gid == os.getgid()
    body = env_path.read_text()
    port_match = re.search(r"^CRM_PORT=(\d+)$", body, re.M)
    assert port_match, body
    port = port_match.group(1)
    assert int(port) >= 8000
    assert "COMPOSE_PROJECT_NAME=crm-acme" in body
    assert "TZ=Australia/Sydney" in body
    assert not re.search(r"^BOOTSTRAP_ADMIN_PASSWORD=", body, re.M)
    assert (tmp_path / "ports.tsv").read_text().startswith(f"acme\t{port}")
    assert f"reverse_proxy 127.0.0.1:{port}" in proc.stdout
    assert "Dry-run" in proc.stdout


def test_new_instance_sh_rejects_unsafe_name(tmp_path):
    env = {
        **os.environ,
        "INSTANCE_ROOT": str(tmp_path),
        "INSTANCE_REGISTRY": str(tmp_path / "ports.tsv"),
    }
    proc = subprocess.run(
        [
            "bash",
            str(REPO_ROOT / "scripts" / "new-instance.sh"),
            "--dry-run",
            "../etc",
            "UTC",
            "ops@example.com",
        ],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0
    assert "lowercase" in proc.stderr
    assert not (tmp_path / "etc").exists()
