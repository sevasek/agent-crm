"""Hermetic backup / restore tests — no Docker, no cloud credentials."""
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from app.backup import (
    BackupError,
    backup_now,
    default_backup_dir,
    online_backup,
    prune_backups,
    restore_snapshot,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _make_db(path: Path, rows: int = 3) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE partners (id INTEGER PRIMARY KEY, name TEXT)")
    conn.executemany(
        "INSERT INTO partners (name) VALUES (?)",
        [(f"row-{i}",) for i in range(rows)],
    )
    conn.commit()
    conn.close()


def _count(path: Path, table: str = "partners") -> int:
    conn = sqlite3.connect(path)
    n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    conn.close()
    return n


def test_online_backup_roundtrip_row_counts(tmp_path):
    src = tmp_path / "live.db"
    _make_db(src, rows=7)
    dest = tmp_path / "backups" / "snap.db"
    online_backup(str(src), str(dest))
    assert dest.is_file()
    assert dest.stat().st_mode & 0o777 == 0o600
    assert _count(dest) == 7

    restored = tmp_path / "scratch" / "crm.db"
    # leftover WAL would be the documented failure mode
    restored.parent.mkdir()
    restored.write_bytes(b"stale")
    (tmp_path / "scratch" / "crm.db-wal").write_text("wal")
    (tmp_path / "scratch" / "crm.db-shm").write_text("shm")
    restore_snapshot(str(dest), str(restored))
    assert _count(restored) == 7
    assert not (tmp_path / "scratch" / "crm.db-wal").exists()
    assert not (tmp_path / "scratch" / "crm.db-shm").exists()
    assert restored.stat().st_mode & 0o777 == 0o600
    asides = list((tmp_path / "scratch").glob("crm.db.pre-restore-*.db"))
    assert len(asides) == 1
    assert asides[0].read_bytes() == b"stale"


def test_online_backup_rejects_missing_source(tmp_path):
    with pytest.raises(BackupError, match="not found"):
        online_backup(str(tmp_path / "missing.db"), str(tmp_path / "out.db"))


def test_online_backup_integrity_check_ok(tmp_path):
    src = tmp_path / "live.db"
    _make_db(src, rows=1)
    dest = tmp_path / "ok.db"
    online_backup(str(src), str(dest))
    conn = sqlite3.connect(dest)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()


def test_default_backup_dir_avoids_live_data_dir(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    db = data / "crm.db"
    db.write_bytes(b"")
    assert default_backup_dir(str(db)) == str(tmp_path / "backups")


def test_backup_now_and_prune(tmp_path, monkeypatch):
    src = tmp_path / "data" / "crm.db"
    src.parent.mkdir()
    _make_db(src, rows=2)
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    dest = backup_now(str(src), backup_dir=str(tmp_path / "backups"))
    assert Path(dest).is_file()
    # Force the file to look old and prune it
    old = Path(dest)
    os.utime(old, (0, 0))
    removed = prune_backups(str(tmp_path / "backups"), keep_days=14)
    assert old in map(Path, removed) or str(old) in removed
    assert not old.exists()


def test_backup_sh_local_python(tmp_path):
    src = tmp_path / "crm.db"
    _make_db(src, rows=5)
    backups = tmp_path / "backups"
    env = {
        **os.environ,
        "CRM_DB_PATH": str(src),
        "BACKUP_DIR": str(backups),
        "BACKUP_KEEP_DAYS": "14",
        "BACKUP_DEST": str(backups / "crm-test.db"),
    }
    env.pop("BACKUP_REMOTE_CMD", None)
    env.pop("BACKUP_RCLONE_DEST", None)
    env.pop("BACKUP_S3_URI", None)
    proc = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "backup.sh")],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    dest = backups / "crm-test.db"
    assert dest.is_file()
    assert dest.stat().st_mode & stat.S_IRWXU == stat.S_IRUSR | stat.S_IWUSR
    assert dest.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO) == 0
    assert _count(dest) == 5


def test_restore_sh_no_docker(tmp_path):
    src = tmp_path / "live.db"
    _make_db(src, rows=4)
    snap = tmp_path / "snap.db"
    online_backup(str(src), str(snap))
    dest = tmp_path / "restored.db"
    dest.write_bytes(b"old")
    (tmp_path / "restored.db-wal").write_text("wal")
    env = {**os.environ, "CRM_DB_PATH": str(dest)}
    proc = subprocess.run(
        [
            "bash",
            str(REPO_ROOT / "scripts" / "restore.sh"),
            "--yes",
            "--no-start",
            str(snap),
            str(dest),
        ],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert _count(dest) == 4
    assert not (tmp_path / "restored.db-wal").exists()


def test_restore_refuses_non_sqlite(tmp_path):
    dest = tmp_path / "live.db"
    _make_db(dest, rows=2)
    junk = tmp_path / "not-sqlite.db"
    junk.write_bytes(b"this is not a database")
    with pytest.raises(BackupError, match="not a sqlite database"):
        restore_snapshot(str(junk), str(dest))
    assert _count(dest) == 2


def test_restore_refuses_corrupt_sqlite(tmp_path):
    dest = tmp_path / "live.db"
    _make_db(dest, rows=2)
    snap = tmp_path / "corrupt.db"
    _make_db(snap, rows=1)
    # Keep the sqlite magic, truncate the first page so integrity_check fails.
    snap.write_bytes(snap.read_bytes()[:64])
    with pytest.raises(BackupError, match="integrity_check"):
        restore_snapshot(str(snap), str(dest))
    assert _count(dest) == 2


def test_prune_includes_pre_migrate_prefix(tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir()
    old = backups / "pre-migrate-v0-to-v1-old.db"
    old.write_bytes(b"x")
    os.utime(old, (0, 0))
    removed = prune_backups(str(backups), keep_days=14)
    assert str(old) in removed
    assert not old.exists()


def test_python_m_backup_cli(tmp_path):
    src = tmp_path / "live.db"
    _make_db(src, rows=3)
    dest = tmp_path / "cli.db"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.backup",
            "--source",
            str(src),
            "--dest",
            str(dest),
            "--keep-days",
            "14",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert _count(dest) == 3
