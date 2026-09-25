"""SQLite online backup / restore helpers.

Use sqlite3.Connection.backup() rather than `cp` on a live WAL database.
Safe under writers; the image has no sqlite3 CLI, so this is the supported
path both on the host and inside the app container.

Backup files are created mode 0600 (and their directory 0700) so contact
PII is not world-readable.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path


class BackupError(Exception):
    """Backup or restore failed (missing file, integrity_check, etc.)."""


def resolve_db_path(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    env = os.getenv("CRM_DB_PATH")
    if env:
        return env
    url = os.getenv("DATABASE_URL", "sqlite:///./data/crm.db")
    if url.startswith("sqlite:///"):
        return url[len("sqlite:///"):]
    return url


def default_backup_dir(db_path: str) -> str:
    """Prefer ./backups next to the data dir, not inside the live DB dir.

    Writing snapshots into ./data keeps them in the Docker build context and
    next to the live file. BACKUP_DIR overrides.
    """
    env = os.getenv("BACKUP_DIR")
    if env:
        return env
    parent = Path(db_path).resolve().parent
    if parent.name == "data":
        return str(parent.parent / "backups")
    return str(parent / "backups")


def _secure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def online_backup(source_path: str, dest_path: str, *, timeout: float = 30.0) -> str:
    """Copy source_path to dest_path via the online backup API.

    Creates dest exclusively with mode 0600 and verifies PRAGMA integrity_check.
    Returns the absolute destination path.
    """
    source_path = os.path.abspath(source_path)
    dest_path = os.path.abspath(dest_path)
    if not os.path.isfile(source_path):
        raise BackupError(f"source database not found: {source_path}")
    if os.path.abspath(source_path) == dest_path:
        raise BackupError("source and destination are the same path")

    dest_dir = os.path.dirname(dest_path) or "."
    _secure_dir(dest_dir)

    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(dest_path, flags, 0o600)
        os.close(fd)
    except FileExistsError as exc:
        raise BackupError(f"destination already exists: {dest_path}") from exc

    src = sqlite3.connect(source_path, timeout=timeout)
    dest = sqlite3.connect(dest_path, timeout=timeout)
    try:
        with dest:
            src.backup(dest)
    except Exception:
        src.close()
        dest.close()
        try:
            os.remove(dest_path)
        except OSError:
            pass
        raise
    else:
        dest.close()
        src.close()

    try:
        os.chmod(dest_path, 0o600)
    except OSError:
        pass

    check = sqlite3.connect(f"file:{dest_path}?mode=ro", uri=True)
    try:
        result = check.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        check.close()
    if result != "ok":
        try:
            os.remove(dest_path)
        except OSError:
            pass
        raise BackupError(f"integrity_check failed: {result}")
    return dest_path


def backup_now(
    source_path: str | None = None,
    backup_dir: str | None = None,
    prefix: str = "crm",
) -> str:
    source = resolve_db_path(source_path)
    dest_dir = backup_dir or default_backup_dir(source)
    stamp = time.strftime("%Y-%m-%dT%H%M%SZ", time.gmtime())
    dest = os.path.join(dest_dir, f"{prefix}-{stamp}.db")
    return online_backup(source, dest)


def prune_backups(
    backup_dir: str,
    keep_days: int = 14,
    prefixes: tuple[str, ...] = ("crm-", "pre-migrate-"),
) -> list[str]:
    """Delete backup files older than keep_days. Returns removed paths."""
    if keep_days < 0:
        return []
    if not os.path.isdir(backup_dir):
        return []
    cutoff = time.time() - (keep_days * 86400)
    removed: list[str] = []
    for name in os.listdir(backup_dir):
        if not name.endswith(".db"):
            continue
        if not any(name.startswith(p) for p in prefixes):
            continue
        path = os.path.join(backup_dir, name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed.append(path)
        except OSError:
            continue
    return removed


def assert_sqlite_snapshot(path: str) -> None:
    """Refuse a non-sqlite or corrupt file before it can replace a live DB."""
    if not os.path.isfile(path):
        raise BackupError(f"snapshot not found: {path}")
    with open(path, "rb") as fh:
        header = fh.read(16)
    if header != b"SQLite format 3\x00":
        raise BackupError(f"snapshot is not a sqlite database: {path}")
    check = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        result = check.execute("PRAGMA integrity_check").fetchone()[0]
    except sqlite3.Error as exc:
        raise BackupError(f"snapshot integrity_check failed: {exc}") from exc
    finally:
        check.close()
    if result != "ok":
        raise BackupError(f"snapshot integrity_check failed: {result}")


def restore_snapshot(snapshot_path: str, dest_path: str) -> str:
    """Replace dest_path with snapshot_path and drop WAL/SHM sidecars.

    Callers must stop writers (compose down) first. A leftover WAL next to
    dest would replay against the restored file and corrupt it.

    The snapshot is checked (sqlite header + PRAGMA integrity_check) before
    dest is touched. If dest already exists it is copied aside as
    dest.pre-restore-<stamp>.db so a bad restore is not the only copy.
    """
    import shutil

    snapshot_path = os.path.abspath(snapshot_path)
    dest_path = os.path.abspath(dest_path)
    assert_sqlite_snapshot(snapshot_path)

    dest_dir = os.path.dirname(dest_path) or "."
    os.makedirs(dest_dir, exist_ok=True)
    if os.path.isfile(dest_path):
        stamp = time.strftime("%Y-%m-%dT%H%M%SZ", time.gmtime())
        aside = f"{dest_path}.pre-restore-{stamp}.db"
        shutil.copy2(dest_path, aside)
        try:
            os.chmod(aside, 0o600)
        except OSError:
            pass
    for suffix in ("-wal", "-shm"):
        sidecar = dest_path + suffix
        if os.path.exists(sidecar):
            os.remove(sidecar)

    tmp = dest_path + ".restore-tmp"
    try:
        shutil.copy2(snapshot_path, tmp)
        os.chmod(tmp, 0o600)
        os.replace(tmp, dest_path)
        os.chmod(dest_path, 0o600)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass

    for suffix in ("-wal", "-shm"):
        sidecar = dest_path + suffix
        if os.path.exists(sidecar):
            os.remove(sidecar)
    return dest_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SQLite online backup / restore")
    parser.add_argument("--source", help="Live database path")
    parser.add_argument("--dest", help="Exact backup destination path")
    parser.add_argument("--dest-dir", help="Directory for timestamped backups")
    parser.add_argument("--prefix", default="crm")
    parser.add_argument("--keep-days", type=int, default=int(os.getenv("BACKUP_KEEP_DAYS", "14")))
    parser.add_argument("--restore", help="Snapshot path to restore")
    parser.add_argument("--restore-dest", help="Live DB path to restore into")
    args = parser.parse_args(argv)

    if args.restore:
        dest = args.restore_dest or resolve_db_path(args.source)
        print(restore_snapshot(args.restore, dest))
        return 0

    source = resolve_db_path(args.source)
    if args.dest:
        print(online_backup(source, args.dest))
    else:
        print(backup_now(source, args.dest_dir, prefix=args.prefix))

    dest_dir = args.dest_dir or (os.path.dirname(args.dest) if args.dest else default_backup_dir(source))
    for path in prune_backups(dest_dir, keep_days=args.keep_days):
        print(f"pruned {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BackupError as exc:
        print(f"backup error: {exc}", file=sys.stderr)
        raise SystemExit(1)
