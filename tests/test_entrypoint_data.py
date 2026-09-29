"""Regression tests for issue 70: entrypoint must not chown -R on every boot.

Production compose drops CAP_DAC_OVERRIDE. After the first successful boot,
/app/data is APP_UID mode 0700 and root cannot traverse it. Blindly re-running
chown -R crash-loops the container.

Decision logic lives in docker-entrypoint-lib.sh so these tests do not need
Docker. When `docker` is on PATH, test_alpine_cap_drop_skip_repair reproduces
the isolated alpine case from the issue body against the real helper.

Alpine acceptance (issue 70) — requires Docker:

    # Directory already owned by a non-root uid, mode 0700 (state after
    # the entrypoint's own first successful run):
    docker run --rm -v "$PWD/data:/app/data" alpine \\
      sh -c 'chown -R 1000:1000 /app/data && chmod 700 /app/data'

    # Old entrypoint: `chown -R` fails with Permission denied.
    # New helper: decide_data_volume_action → skip (no chown).
    docker run --rm --cap-drop ALL --cap-add CHOWN --cap-add FOWNER \\
      --cap-add SETUID --cap-add SETGID \\
      --security-opt no-new-privileges:true \\
      -v "$PWD/data:/app/data" \\
      -v "$PWD/docker-entrypoint-lib.sh:/lib.sh:ro" \\
      alpine sh -c '. /lib.sh; test "$(decide_data_volume_action /app/data 1000 1000)" = skip'
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LIB = REPO_ROOT / "docker-entrypoint-lib.sh"
ENTRYPOINT = REPO_ROOT / "docker-entrypoint.sh"
COMPOSE_PROD = REPO_ROOT / "docker-compose.prod.yml"

UID = os.getuid()
GID = os.getgid()
OTHER_UID = UID + 1 if UID != 0 else 1001


def _lib(cmd: str, directory: Path, uid: int = UID, gid: int = GID) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(LIB), cmd, str(directory), str(uid), str(gid)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )


def _decide(directory: Path, uid: int = UID, gid: int = GID) -> str:
    proc = _lib("decide", directory, uid, gid)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    return proc.stdout.strip()


def test_prod_compose_keeps_issue_60_hardening_without_dac_override():
    text = COMPOSE_PROD.read_text()
    assert "cap_drop:" in text
    assert "no-new-privileges:true" in text
    assert "read_only: true" in text
    for cap in ("CHOWN", "FOWNER", "SETUID", "SETGID"):
        assert f"- {cap}" in text
    assert "staleness-cron:" in text
    # YAML list item would re-add the capability. Comments may mention it.
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert "DAC_OVERRIDE" not in stripped


def test_decide_skip_when_already_owned_0700(tmp_path: Path):
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    data.chmod(0o700)
    assert _decide(data) == "skip"


def test_decide_repair_when_traversable_and_wrong_owner(tmp_path: Path):
    data = tmp_path / "data"
    data.mkdir(mode=0o755)
    data.chmod(0o755)
    assert _decide(data, uid=OTHER_UID, gid=GID) == "repair"


def test_decide_error_when_cannot_traverse_and_wrong_owner(tmp_path: Path):
    data = tmp_path / "data"
    data.mkdir()
    data.chmod(0o000)
    try:
        assert _decide(data, uid=OTHER_UID, gid=GID) == "error"
    finally:
        data.chmod(0o700)


def test_decide_error_when_cannot_stat(tmp_path: Path):
    parent = tmp_path / "secret"
    parent.mkdir()
    data = parent / "data"
    data.mkdir()
    parent.chmod(0o000)
    try:
        proc = _lib("decide", data)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "error"
    finally:
        parent.chmod(0o700)


def test_decide_repair_when_missing(tmp_path: Path):
    assert _decide(tmp_path / "does-not-exist") == "repair"


def test_prepare_skip_does_not_chmod_already_owned(tmp_path: Path):
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    data.chmod(0o700)
    db = data / "crm.db"
    db.write_text("x", encoding="utf-8")
    db.chmod(0o644)
    proc = _lib("prepare", data)
    assert proc.returncode == 0, proc.stderr
    assert db.stat().st_mode & 0o777 == 0o644
    assert data.stat().st_mode & 0o777 == 0o700


def test_repair_sets_dir_700_files_600(tmp_path: Path):
    data = tmp_path / "data"
    data.mkdir(mode=0o755)
    data.chmod(0o755)
    nested = data / "nested"
    nested.mkdir(mode=0o755)
    nested.chmod(0o755)
    db = data / "crm.db"
    db.write_text("x", encoding="utf-8")
    db.chmod(0o644)
    proc = _lib("repair", data)
    assert proc.returncode == 0, proc.stderr
    assert data.stat().st_mode & 0o777 == 0o700
    assert nested.stat().st_mode & 0o777 == 0o700
    assert db.stat().st_mode & 0o777 == 0o600


def test_prepare_error_message_names_host_chown(tmp_path: Path):
    parent = tmp_path / "secret"
    parent.mkdir()
    data = parent / "data"
    data.mkdir()
    parent.chmod(0o000)
    try:
        proc = _lib("prepare", data, uid=1000, gid=1000)
        assert proc.returncode != 0
        err = proc.stderr
        assert "ERROR:" in err
        assert "chown -R 1000:1000 ./data" in err
        assert "CAP_DAC_OVERRIDE" in err
        assert "chmod 700 ./data" in err
        assert "Permission denied" not in err.split("chown", 1)[0]
    finally:
        parent.chmod(0o700)


def test_non_root_entrypoint_writable_check_passes(tmp_path: Path):
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    data.chmod(0o700)
    proc = subprocess.run(
        ["sh", str(ENTRYPOINT), "sh", "-c", "echo ran-as-$(id -u)"],
        cwd=str(REPO_ROOT),
        env={**os.environ, "DATA_DIR": str(data)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert f"ran-as-{UID}" in proc.stdout


def test_non_root_entrypoint_rejects_unwritable(tmp_path: Path):
    data = tmp_path / "data"
    data.mkdir()
    data.chmod(0o000)
    try:
        proc = subprocess.run(
            ["sh", str(ENTRYPOINT), "true"],
            cwd=str(REPO_ROOT),
            env={**os.environ, "DATA_DIR": str(data)},
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode != 0
        assert "not writable" in proc.stderr
        assert "chown -R" in proc.stderr
    finally:
        data.chmod(0o700)


def test_non_root_entrypoint_rejects_readonly_db(tmp_path: Path):
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    data.chmod(0o700)
    db = data / "crm.db"
    db.write_text("x", encoding="utf-8")
    db.chmod(0o444)
    proc = subprocess.run(
        ["sh", str(ENTRYPOINT), "true"],
        cwd=str(REPO_ROOT),
        env={**os.environ, "DATA_DIR": str(data)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0
    assert "readonly database" in proc.stderr


def _docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    probe = subprocess.run(
        ["docker", "info"],
        capture_output=True,
        check=False,
    )
    return probe.returncode == 0


@pytest.mark.skipif(not _docker_available(), reason="docker is not available")
def test_alpine_cap_drop_skip_repair(tmp_path: Path):
    """Issue 70 alpine repro against the real helper: 0700 + APP_UID → skip."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "crm.db").write_text("x", encoding="utf-8")
    setup = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{data}:/app/data",
            "alpine",
            "sh",
            "-c",
            "chown -R 1000:1000 /app/data && chmod 700 /app/data",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert setup.returncode == 0, setup.stderr

    old = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "CHOWN",
            "--cap-add",
            "FOWNER",
            "--cap-add",
            "SETUID",
            "--cap-add",
            "SETGID",
            "--security-opt",
            "no-new-privileges:true",
            "-v",
            f"{data}:/app/data",
            "alpine",
            "sh",
            "-c",
            "chown -R 1000:1000 /app/data",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert old.returncode != 0
    assert "Permission denied" in (old.stderr + old.stdout)

    helper = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "CHOWN",
            "--cap-add",
            "FOWNER",
            "--cap-add",
            "SETUID",
            "--cap-add",
            "SETGID",
            "--security-opt",
            "no-new-privileges:true",
            "-v",
            f"{data}:/app/data",
            "-v",
            f"{LIB}:/lib.sh:ro",
            "alpine",
            "sh",
            "-c",
            ". /lib.sh; action=$(decide_data_volume_action /app/data 1000 1000); "
            "echo ACTION=$action; "
            "prepare_data_volume_as_root /app/data 1000 1000; "
            "test \"$action\" = skip",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert helper.returncode == 0, helper.stderr + helper.stdout
    assert "ACTION=skip" in helper.stdout
    assert (data / "crm.db").read_text(encoding="utf-8") == "x"
    # Skip must not chmod files already in the volume (regression vs every-boot chmod 600).
    assert stat.S_IMODE((data / "crm.db").stat().st_mode) == 0o644
