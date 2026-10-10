# BarBot Project Summary

Scope: first-read project purpose, stack, and current state
Trust: Mixed — code facts Confirmed; production facts Owner-confirmed
Lifecycle: Fresh
Last verified: code 2026-09-29; runtime 2026-09-24
Evidence: `README.md`, `app/bot.py`, `app/config.py`, Git history, owner rollout confirmation
Depends on: current HEAD and production deployment
Importance: Critical

BarBot is an internal Telegram assistant for bar staff. It provides a
data-driven technological-card catalog and a warehouse workflow through
aiogram 3 inline UI and long polling. Python/Pydantic services implement
business rules; JSON stores the catalog; versioned SQLite stores users,
permissions, warehouse state, reports, schedules, corrections, and attendance.

Code-confirmed state:

- Python 3.12, aiogram 3, Pydantic/pydantic-settings, pytest.
- SQLite target schema is v12; application startup calls initialization and
  validation before polling.
- Roles are `super_admin`, `admin`, `bartender`, `accountant`, `user`, and
  `blocked`; authorization is centralized and repeated server-side.
- Warehouse supports nomenclature, writeoff, transfer, development, atomic
  intake, closing rules, XLSX reports, recipients/delivery, reminders,
  schedules, corrections, and personal attendance sessions.
- Catalog at commit `a57cc389c4aafcf5e840b67b0481a168c032fe3b`
  has 10 categories and 513 cards, including 6 cards in `Новые лимонады`.

Owner-confirmed production state — last verified 2026-09-24; requires live
revalidation before production actions:

- schema v12; 416 total/active products (414 seed plus 2 deliberately added);
- aliases 8; required rules 2; `WAREHOUSE_ENABLED=true`;
- service active with one polling owner;
- verified backup recorded at the approved production backup root;
- GitHub remote configured and the v12 rollout completed.

Current development state: an opt-in Phase 1 AI pilot uses Groq Whisper for
voice and Gemini for strict intent classification, only for `super_admin` in a
private chat. It delegates catalog lookup and warehouse draft preparation to
existing services; mutations retain preview/confirmation. All AI flags default
to false, and the AI layer has no direct SQLite, shell, systemd, deployment,
catalog-editing, or internet-search tools. Production deployment is unverified.
