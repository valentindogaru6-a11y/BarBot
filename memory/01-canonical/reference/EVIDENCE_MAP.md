# Evidence Map

Scope: provenance map for canonical memory
Trust: Confirmed routing; evidence must still be re-read when exactness matters
Lifecycle: Fresh
Last verified: 2026-09-29
Evidence: repository files listed below
Depends on: current HEAD
Importance: Medium

- Project/runtime composition: `README.md`, `app/bot.py`, `app/config.py`.
- Permissions: `app/models/user.py`, `app/services/access_service.py`, access tests.
- Schema/migrations: `app/storage/migrations.py`, migration v4-v12 tests.
- Warehouse dates/quantities: business-date and quantity services/tests.
- Transaction/recovery: warehouse databases plus operations, reminder, delivery,
  schedule, correction, and attendance tests.
- Production safety: root `AGENTS.md` and `docs/production_*.md`.
- Catalog: `data/catalog.json`, `source/import_bar_xls.py`, catalog tests, commit
  `a57cc389c4aafcf5e840b67b0481a168c032fe3b`.
- Runtime baseline: owner rollout confirmation only; requires live revalidation.
