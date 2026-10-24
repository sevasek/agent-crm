#!/usr/bin/env bash
# Pre-deploy migration dry run: run <sha>'s init_db() on a copy of live data.
# Usage (on the server, in the deploy checkout): scripts/migration-dryrun.sh <sha>
#
# Read-only against production: the live DB is only read (online sqlite
# backup, run as APP_UID inside the running app container) and streamed out,
# because `docker cp` can't read the container's tmpfs /tmp. The copy is
# migrated in a throwaway image built from `git archive <sha>`, with
# --network none. Checks: user_version == SCHEMA_VERSION, integrity_check ok,
# foreign_key_check empty, and row counts of existing tables unchanged.
# The copy and the image are deleted on exit, pass or fail.
#
# CRM_CONTAINER overrides the live container (default: compose service app).
set -euo pipefail
umask 077

[[ $# -eq 1 ]] || { echo "usage: $0 <sha>" >&2; exit 2; }
sha=$(git rev-parse --verify "$1^{commit}")
cid=${CRM_CONTAINER:-$(docker compose ps -q app)}
[[ -n "$cid" ]] || { echo "no running app container (set CRM_CONTAINER)" >&2; exit 1; }
# Root in prod has no DAC_OVERRIDE, so read as the owner of /app/data.
uid=$(docker exec "$cid" stat -c %u /app/data)
gid=$(docker exec "$cid" stat -c %g /app/data)

work=$(mktemp -d)
image="crm-migration-dryrun:${sha:0:12}"
cleanup() {
  rm -rf "$work"
  docker image rm -f "$image" >/dev/null 2>&1 || true
}
trap cleanup EXIT
mkdir "$work/data" "$work/backups"

echo "Copying live DB from container ${cid:0:12} as uid $uid" >&2
# ponytail: the copy is held in memory inside the app container (256m limit);
# write to a host-mounted path instead if the DB grows past ~100 MB.
docker exec -u "$uid:$gid" "$cid" python -c '
import sqlite3, sys
src = sqlite3.connect("file:/app/data/crm.db?mode=ro", uri=True)
mem = sqlite3.connect(":memory:")
src.backup(mem)
sys.stdout.buffer.write(mem.serialize())
' > "$work/data/crm.db"
[[ -s "$work/data/crm.db" ]] || { echo "live backup is empty" >&2; exit 1; }

echo "Building $image from git archive $sha" >&2
git archive "$sha" | docker build -q -t "$image" - >/dev/null

echo "Running init_db() on the copy" >&2
docker run -i --rm --network none \
  -e APP_UID="$uid" -e APP_GID="$gid" \
  -e DATABASE_URL=sqlite:///./data/crm.db -e BACKUP_DIR=/app/backups \
  -v "$work/data:/app/data" -v "$work/backups:/app/backups" \
  "$image" python - <<'PY'
import sqlite3, sys
from app.database import SCHEMA_VERSION, init_db

PATH = "/app/data/crm.db"

def counts():
    db = sqlite3.connect(PATH)
    tables = [r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    out = {t: db.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
    db.close()
    return out

db = sqlite3.connect(PATH)
before_ver = db.execute("PRAGMA user_version").fetchone()[0]
db.close()
before = counts()
print(f"live user_version={before_ver}, {len(before)} tables; target SCHEMA_VERSION={SCHEMA_VERSION}")

init_db()

db = sqlite3.connect(PATH)
problems = []
ver = db.execute("PRAGMA user_version").fetchone()[0]
if ver != SCHEMA_VERSION:
    problems.append(f"user_version {ver} != SCHEMA_VERSION {SCHEMA_VERSION}")
ok = [r[0] for r in db.execute("PRAGMA integrity_check")]
if ok != ["ok"]:
    problems.append(f"integrity_check: {ok}")
fk = db.execute("PRAGMA foreign_key_check").fetchall()
if fk:
    problems.append(f"foreign_key_check: {len(fk)} rows, first {fk[:5]}")
db.close()
after = counts()
for t, n in before.items():
    if after.get(t) != n:
        problems.append(f"row count {t}: {n} -> {after.get(t)}")
new = sorted(set(after) - set(before))
if new:
    print(f"new tables: {', '.join(new)}")

if problems:
    print("FAIL")
    for p in problems:
        print(f"  {p}")
    sys.exit(1)
print(f"PASS: v{before_ver} -> v{ver}, row counts unchanged")
PY
