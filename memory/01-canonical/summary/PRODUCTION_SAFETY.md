# Production Safety Summary

Scope: constraints for production inspection and rollout
Trust: Confirmed policy; runtime state Owner-confirmed
Lifecycle: Fresh
Last verified: policy 2026-09-29; runtime 2026-09-24
Evidence: `AGENTS.md`, `docs/production_readiness.md`, `docs/production_rollout.md`, `docs/production_rollback.md`
Depends on: explicit owner authorization and live preflight
Importance: Critical

- Do not read or change real `.env`, production SQLite/WAL/SHM, audit log,
  reports, XLS/XLSX, backups, systemd, or polling during normal development.
- Never output tokens, secrets, Telegram IDs, credentials, or message content.
- Production work requires the explicit staged Phase 10B workflow, exact HEAD,
  clean tree, the preceding stop point, and separate authorization for the next
  stage. Memory is not authorization.
- Stop one verified polling owner before a consistent SQLite backup. Verify the
  backup before permission hardening or migration; never improvise SQL
  downgrade or automatic rollback.
- Application startup invokes database initialization. An uncontrolled restart
  may therefore run a migration.
- Logical `mode=ro` plus `query_only=ON` does not promise zero filesystem side
  effects: SQLite can create/update WAL/SHM while resolving a live WAL database.
- Seed reconciliation must not delete or overwrite owner-created production
  products. The accepted runtime baseline is 416 active products, not the 414
  product seed count.

Owner-confirmed, last verified 2026-09-24, live revalidation required:
schema v12, warehouse enabled, one running polling owner, 416 active products,
8 aliases, 2 required rules, and a verified pre-rollout backup. Do not infer
current truth from this note.
