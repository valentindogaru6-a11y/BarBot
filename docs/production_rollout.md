# Phase 10B production rollout runbook

This document is preparation only. Do not execute it without a separate,
explicit Phase 10B authorization and a named operator. Replace every
`<operator-confirmed-...>` placeholder; never guess a service unit or path.

The controlled-rollout exception in `AGENTS.md` is active only when the task
names Phase 10B (or continues a precise Stage/STOP), supplies the confirmed
HEAD, has a clean tree, and separately authorizes the next Stage. Stage 0 is
read-only. Stage 1 stop/backup, Stage 2 migration/import, Stage 3
start/acceptance, and Stage 4 enablement each require a new explicit approval.
Changing policy in a turn never authorizes production access in that same turn.
The previous Stage 0 attempt ended `NO-GO` without production mutations.

## Before the maintenance window

1. Name the operator, reviewer, maintenance start/end, and rollback owner.
2. Confirm the server identity through the approved infrastructure inventory.
3. Confirm repository `/opt/barbot`, branch `main`, approved release HEAD, and
   a clean working tree.
4. Attach the successful full pytest and Phase 10A targeted results.
5. Record free disk space/inodes for the DB, backup, report, and audit filesystems.
6. Record current Git HEAD and SQLite `PRAGMA user_version` without changing DB.
7. Confirm exactly one polling owner. Identify the actual systemd unit from
   owner-controlled operations data; this repository does not define its name.
8. Confirm an admin, accountant, and dedicated test recipient are available.
9. Prepare an empty, mode-`0700`, timestamped backup parent with enough free space.
10. Announce a short maintenance window. Do not enable warehouse functionality yet.

Run config-only preflight with configuration supplied to the process without
echoing it:

```bash
cd /opt/barbot
.venv/bin/python -m app.release_preflight \
  --config-check-only \
  --polling-owner-count 1
```

Any `FAIL` is a stop condition. Resolve warnings explicitly in the change log.
Do not change DB/audit/report permissions during Stage 0. Any required
permission hardening must be named and approved as a separate rollout action.

## Stop point 1 — identity and rollback authorization

Before stopping anything, two people confirm:

- correct server and restaurant instance;
- repository, branch, and exact approved HEAD;
- clean tree and expected Python 3.12 virtualenv;
- actual systemd unit and exactly one polling owner;
- explicit database/report/audit/catalog paths;
- writable backup location outside those source paths;
- the rollback backup procedure and access to the previous Git revision.

If any item is uncertain, stop the rollout.

## Stop service and prove polling stopped

Set shell variables to explicit operator-reviewed values. Do not reuse `HOME`,
do not print environment contents, and do not use repository root as a backup
target.

```bash
BARBOT_SERVICE='<operator-confirmed-systemd-unit>'
BARBOT_REPO='/opt/barbot'
BARBOT_DB='<operator-confirmed-absolute-db-path>'
BARBOT_REPORTS='<operator-confirmed-absolute-report-directory>'
BARBOT_AUDIT='<operator-confirmed-absolute-audit-path>'
BARBOT_BACKUP_ROOT='<operator-confirmed-dedicated-backup-directory>'

sudo systemctl stop "$BARBOT_SERVICE"
sudo systemctl is-active "$BARBOT_SERVICE"
```

`is-active` must report `inactive`. Independently confirm there is no remaining
`python -m app.bot` process for this instance and no other polling owner. Do
not proceed on `activating`, `deactivating`, `failed` without diagnosis, or an
ambiguous process list.

## Consistent backup

Never copy only the live main SQLite file while WAL may contain committed data.
With polling stopped, use the SQLite backup API to a new path. The destination
must not exist.

```bash
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_DIR="$BARBOT_BACKUP_ROOT/$STAMP"
test -n "$BARBOT_BACKUP_ROOT"
test "$BARBOT_BACKUP_ROOT" != "/"
test "$BARBOT_BACKUP_ROOT" != "$BARBOT_REPO"
test ! -e "$BACKUP_DIR"
df -Pk "$BARBOT_BACKUP_ROOT"
install -d -m 0700 "$BACKUP_DIR"

cd "$BARBOT_REPO"
.venv/bin/python - "$BARBOT_DB" "$BACKUP_DIR/barbot.db" <<'PY'
import sqlite3, sys
from pathlib import Path
from urllib.parse import quote
source, target = map(Path, sys.argv[1:])
if not source.is_file() or target.exists():
    raise SystemExit("unsafe backup source or existing target")
src = sqlite3.connect(f"file:{quote(str(source.resolve()), safe='/')}?mode=ro", uri=True)
dst = sqlite3.connect(target)
try:
    src.backup(dst)
finally:
    dst.close(); src.close()
PY
chmod 0600 "$BACKUP_DIR/barbot.db"
```

Record metadata and validate the copy, never the working DB:

```bash
git rev-parse HEAD > "$BACKUP_DIR/git-head.txt"
.venv/bin/python - "$BACKUP_DIR/barbot.db" > "$BACKUP_DIR/sqlite-check.txt" <<'PY'
import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
try:
    print("schema_version=" + str(db.execute("PRAGMA user_version").fetchone()[0]))
    print("integrity=" + str(db.execute("PRAGMA integrity_check").fetchone()[0]))
    print("foreign_key_violations=" + str(len(db.execute("PRAGMA foreign_key_check").fetchall())))
finally:
    db.close()
PY
sha256sum "$BACKUP_DIR/barbot.db" > "$BACKUP_DIR/SHA256SUMS"
stat -c 'bytes=%s mode=%a' "$BACKUP_DIR/barbot.db" > "$BACKUP_DIR/db-stat.txt"
```

`integrity=ok` and zero FK violations are mandatory. Save reports and audit as
separate, non-overwriting artifacts with restrictive permissions. Include
their checksums in `SHA256SUMS`; do not archive `.env` or print secrets. Record
whether `-wal`/`-shm` existed, but the SQLite backup API output is the restore
source.

## Migration and seed

1. Checkout the exact approved release commit and re-confirm clean tree.
2. Run full read-only preflight against the stopped instance. Before migration,
   a schema older than v11 is expected to fail only the schema check; every
   other blocking failure must be resolved.
3. Apply migration exactly once through the application migration runner:

```bash
cd "$BARBOT_REPO"
.venv/bin/python - "$BARBOT_DB" <<'PY'
import sys
from app.storage.user_database import UserDatabase
UserDatabase(sys.argv[1]).initialize()
print("migration initialization completed")
PY
```

4. Run full preflight again. It must report schema v11, integrity ok, and no FK violations.
5. Seed dry-run first:

```bash
.venv/bin/python scripts/import_warehouse_products.py \
  --database "$BARBOT_DB" \
  --input source/warehouse_products_seed.json \
  --allow-production
```

The command is read-only without `--apply`. Compare the expected plan and obtain
reviewer approval before continuing.
6. Apply once with all guards:

```bash
.venv/bin/python scripts/import_warehouse_products.py \
  --database "$BARBOT_DB" \
  --input source/warehouse_products_seed.json \
  --apply \
  --allow-production \
  --confirm-production-import APPLY-WAREHOUSE-SEED
```

7. Repeat the same guarded command. It must report 414 unchanged products and
   two unchanged required rules, with no creates/conflicts/errors.
8. Confirm `data/catalog.json` has not changed.

## Start exactly one instance

```bash
sudo systemctl start "$BARBOT_SERVICE"
sudo systemctl is-active "$BARBOT_SERVICE"
```

Confirm one process only. Review startup logs without dumping environment or
tokens. Check there is no migration loop, scheduler/reminder workers recovered,
and report/delivery/correction recovery has no unhandled failures.

## Controlled production acceptance

Use only a dedicated test recipient first; do not send the first smoke report
to all real recipients.

1. `/start` and role visibility.
2. Admin, bartender, accountant, user, and blocked permissions.
3. Test writeoff, transfer, and development.
4. Edit an operation; then test delete/restore where applicable.
5. Complete required checklist and close only the designated test shift.
6. Verify three-sheet XLSX and immutable report history.
7. Deliver only to the dedicated test recipient and inspect delivery records.
8. Open a correction, change an operation, issue `v002`, and manually deliver it.
9. Confirm schedules did not select the correction report.
10. Confirm accountant can read history/status but cannot mutate/send/retry.

## Stop point 2 — expand or rollback

The operator and reviewer record acceptance results. Real recipients/users are
enabled only after an explicit approval at this stop point. Any unexplained
error, duplicate polling owner, migration/import inconsistency, corrupt report,
permission failure, or unintended Telegram recipient means stop and follow
the rollback runbook.
