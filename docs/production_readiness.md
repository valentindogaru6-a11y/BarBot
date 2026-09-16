# BarBot production readiness (Phase 10A)

Phase 10A is an offline readiness stage. It does not authorize deployment,
production migration, seed import, polling, systemd changes, or Telegram
delivery. Phase 10B requires a separate explicit owner decision.

Default development remains production-safe: the real `.env`, production
SQLite/audit/XLSX, polling, and systemd are out of bounds. The limited
exception in `AGENTS.md` applies only to an explicitly authorized Phase 10B
Stage, with a confirmed HEAD, clean tree, and the preceding stop point passed.
Every later Stage requires a new confirmation; permission never carries
forward automatically.

## Audited runtime facts

- Application command: `.venv/bin/python -m app.bot` from `/opt/barbot`.
- Supported interpreter: Python 3.12.
- Telegram mode: aiogram long polling. `app.bot.main` owns polling, the
  reminder worker, and the schedule worker in one process.
- Shutdown: `finally` cancels and awaits both workers, then closes the bot HTTP
  session. SIGTERM must be allowed enough time to complete this path.
- SQLite target: schema v11. `UserDatabase.initialize()` performs the
  transactional migration/validation and enables WAL.
- Seed entrypoint: `scripts/import_warehouse_products.py`; dry-run is default,
  writes require `--apply`. The production path additionally requires two
  explicit guards documented in the rollout runbook.
- Warehouse timezone/cutoff: persistent `warehouse_settings`, initially
  `Europe/Kyiv` and `23:00`. Shift snapshots preserve the effective values.
- Administrative audit path: repository-fixed
  `logs/admin_audit.jsonl`; it is not an environment setting.
- Final report directory: `WAREHOUSE_REPORTS_PATH`, relative to the repository
  and restricted by application validation.
- There is no repository-owned systemd unit or deployment script. The operator
  must identify the real unit name and must not infer it from this document.

## Configuration keys

Values must never be pasted into tickets, logs, command history, or reports.

| Key | Rule |
| --- | --- |
| `BOT_TOKEN` | Required secret; preflight reports only configured/missing. |
| `SUPER_ADMIN_IDS` | Required non-empty JSON array of positive Telegram IDs. |
| `ADMIN_IDS` | Deprecated fallback only; migrate to `SUPER_ADMIN_IDS`. |
| `CATALOG_PATH` | Optional; default `data/catalog.json`. |
| `DATABASE_PATH` | Optional; default `data/barbot.db`. |
| `WAREHOUSE_ENABLED` | Safe default `false`; change only at the rollout stop point. |
| `WAREHOUSE_REPORTS_PATH` | Optional relative path; default `reports/warehouse`. |
| `VOICE_SEARCH_ENABLED` | Optional boolean. |
| `VOSK_MODEL_PATH` | Must remain inside the repository. |
| `VOSK_LANGUAGE` | Optional; current default `ru`. |
| `VOICE_MAX_DURATION_SECONDS` | Positive integer. |
| `VOICE_MAX_FILE_SIZE_MB` | Positive integer. |
| `VOICE_MAX_CONCURRENT` | Positive integer. |
| `VOICE_DEBUG_TRANSCRIPTS` | Must remain `false` in production unless explicitly approved. |

The real `.env` is operational secret material. The preflight command reads
the process environment only and deliberately does not load `.env` itself.
During an explicitly authorized Stage 0, an operator-side wrapper may validate
only the allowlisted keys programmatically and pass them to preflight without
printing values. This exception is read-only and does not apply to development.

## Read-only release preflight

Config-only mode performs no SQLite open and creates no directories or files:

```bash
cd /opt/barbot
.venv/bin/python -m app.release_preflight \
  --config-check-only \
  --polling-owner-count 1
```

Full read-only mode opens the explicitly configured database with SQLite
`mode=ro` and `query_only=ON`, then checks schema, integrity, and foreign keys:

```bash
cd /opt/barbot
.venv/bin/python -m app.release_preflight \
  --db /absolute/operator-confirmed/barbot.db \
  --report-dir /opt/barbot/<operator-confirmed-report-directory> \
  --audit-log /opt/barbot/logs/admin_audit.jsonl \
  --catalog /absolute/operator-confirmed/catalog.json \
  --polling-owner-count 1
```

Required secrets/IDs must already exist in the process environment; preflight
never prints their values. It reports `PASS`, `WARN`, `FAIL`, or `SKIP` and
returns non-zero when a blocking check fails. `--allow-temporary-paths` exists
only for offline automated tests and must not be used in production.

Checks cover Python/imports, key presence, timezone/cutoff, normalized paths,
safe path types and separation, non-temporary report location, access based on
existing permissions without probe writes, declared polling ownership,
entrypoint/router/callback imports, schema target v11, the 414-product seed,
the application-fixed audit path, and the real Telegram delivery adapter. It
also requires the report directory to resolve inside the repository, matching
runtime configuration validation. It never imports seed, migrates SQLite,
starts polling, creates a report, or calls Telegram.

## Filesystem ownership and modes

The single service account must be able to traverse the repository and read:

- application code and `.venv`;
- the operator-confirmed configuration source;
- `data/catalog.json`;
- Vosk model files when voice search is enabled.

It needs read/write access only to the configured SQLite parent, report
directory, `logs/`, and `backups/`. Recommended starting modes are directories
`0750` (or stricter) and SQLite/report/audit files `0640` (or stricter), owned
by the service account and its dedicated group. DB, WAL/SHM, reports, audit,
and backups must not be world-readable.

## Automated rehearsals

All Phase 10A rehearsals use pytest temporary directories:

- fresh SQLite → v11 and repeated v11 initialization;
- legacy v1/full migration suite and v10 → v11;
- future-version rejection and atomic rollback on injected failure;
- schema validation, `integrity_check`, and `foreign_key_check`;
- seed dry-run/apply/repeat with 414 active products, 8 aliases, two owner
  rules, and `Бар`/`Кухня`/`Кондитерка`;
- offline writeoff, transfer, development, edit, required checklist, closing,
  three-sheet XLSX, fake delivery, daily/one-off schedules, correction `v002`,
  manual correction delivery, schedule isolation, accountant read-only, audit,
  and idempotent recovery.

Generated SQLite and XLSX artifacts live under `tmp_path`; fake senders never
open a network connection. Quantity storage remains integer-scaled, so the
rehearsal does not introduce float drift.

## Recovery behavior audited in code

- Report build: snapshot/version are persistent; XLSX is built outside the
  long SQLite transaction; retry/recovery reuses the same logical version.
- Delivery: persistent per-recipient records, atomic claim, stale `sending`
  recovery, idempotent delivered rows, and retry limited to failed/pending.
- Schedules: persistent jobs and stale claim recovery; correction reports are
  excluded from automatic selection and rejected by scheduled delivery.
- Corrections: persistent session/report linkage; `building`, `failed`, ready
  but incomplete, and repeated recovery do not create an extra version.

## Daily operational checklist

1. Confirm exactly one long-polling process owns the bot token.
2. Check reminder/scheduler heartbeat and recent recovery logs.
3. Check free space and inode availability.
4. Record SQLite, `-wal`, and `-shm` sizes without copying them ad hoc.
5. Review failed/partial delivery jobs and stale jobs.
6. Review `building`/`failed` reports.
7. Review open/building/failed correction sessions.
8. Verify report directory accessibility and non-world-readable permissions.
9. Verify backup age, checksum manifest, and last restore test.
10. Review errors for operational impact and ensure no token/secret was logged.

No external monitoring dependency is introduced by Phase 10A.

The first attempted Stage 0 stopped with `NO-GO` and made no production
mutation. Production permission hardening is an operational change and must be
performed only as a separately authorized rollout action, never inferred from
read-only inspection.

## Security review outcome

- Secrets remain represented with `SecretStr`; preflight never renders values.
- Telegram user content is HTML-escaped in correction/report delivery UI.
- Compact callback payloads remain covered by the 64-byte regression tests.
- Mutation/send authorization is repeated at service/storage boundaries.
- Report publication verifies normalized repository-contained paths and XLSX
  checksum/size; preflight resolves symlinks before path policy checks.
- Seed importer defaults to dry-run and requires three independent signals for
  a production apply.
- SQLite/WAL/SHM, generated XLSX files, and JSONL audit records are forced to
  owner-only mode `0600`; parent-directory ownership remains an operator check.
- `.gitignore` excludes `.env`, logs, backups, reports, SQLite, bytecode, and
  local models.
- No generated DB/SQLite/XLSX or real `.env` belongs in Git.

The repository has no cross-process polling lock. The deployment platform is
therefore responsible for running exactly one unit/process. More than one
declared owner is a blocking preflight failure.
