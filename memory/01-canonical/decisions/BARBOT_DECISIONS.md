# BarBot Decisions

Scope: compact durable decision record
Trust: Confirmed, except the future-AI boundary which is owner-approved direction
Lifecycle: Fresh
Last verified: 2026-09-29
Evidence: application code, tests, rollout documents, Git history, owner direction
Depends on: current architecture and production policy
Importance: Critical

## D1 — SQLite and sequential migrations

Use local SQLite without ORM. Every version advances through an explicit,
transactional step and finishes with structural validation, integrity, and FK
expectations. Never use hand-written production downgrade SQL.

## D2 — Business date and cutoff

Warehouse time uses `Europe/Kyiv` and a `23:00` cutoff. Operations after cutoff
belong to the next business date; shifts snapshot timezone and cutoff.

## D3 — Exact quantities

Parse with `Decimal`, reject float input, and persist integer-scaled quantities
with explicit base units. This prevents binary-float drift.

## D4 — No silent fuzzy acceptance

Fuzzy matching may rank candidates but cannot silently select a product,
destination, or unit. Missing/unknown fields remain separate user-visible
resolution states.

## D5 — Whole-message atomic intake

Multi-line input is one persistent batch. Resolve every line and ask per-item
comments before one final atomic commit; cancellation creates no partial
operations.

## D6 — Separate audit domains

Catalog/user administration uses the restrictive JSONL admin audit. Warehouse
business mutations use transactional SQLite audit records tied to shifts and
operations. Do not merge them casually.

## D7 — Logical read-only release inspection

Release inspection uses its own `mode=ro`, `query_only=ON` connection and never
calls initialization, migrations, journal-mode changes, checkpoint, or chmod.
WAL/SHM filesystem activity remains theoretically possible.

## D8 — Backup before hardening or migration

During staged rollout, stop the verified single service, create and verify a
new SQLite API backup, then harden permissions and migrate. Migration never
precedes the verified backup.

## D9 — Personal attendance is not the warehouse shift

The warehouse shift aggregates business operations per source/date. Attendance
sessions record individual employee intervals and link to that shift without
sharing its lifecycle.

## D10 — Confirm mutations

Destructive or consequential Telegram actions require preview/confirmation,
revision/token checks, server-side authorization, and idempotent replay
behavior. Visible buttons are not authorization.

## D11 — Preserve production-added products

The 414-product seed is not the whole production nomenclature. Reconcile seed
records without deleting or overwriting deliberate owner additions; current
owner-confirmed production baseline is 416 active products.

## D12 — Future AI service boundary

Proposed Groq Whisper/Gemini functionality may interpret input and request
existing service operations. It must not receive direct SQL, filesystem shell,
systemd, polling, migration, deployment, or unrestricted mutation access.
Mutations require preview and explicit confirmation. This layer is not yet
implemented.
