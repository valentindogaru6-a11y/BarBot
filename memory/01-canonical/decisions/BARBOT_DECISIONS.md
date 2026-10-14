# BarBot Decisions

Scope: compact durable decision record
Trust: Confirmed, except the future-AI boundary which is owner-approved direction
Lifecycle: Fresh
Last verified: 2026-10-14
Evidence: application code, tests, rollout documents, Git history, owner direction, commit `c2ce9fd2ae46509b8ed47aa6d44e1cc1b3ccf80f`
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
owner-confirmed production baseline is 417 active products.

## D12 — Bounded AI service boundary

The Phase 1 Groq Whisper/Gemini pilot may interpret input and request only
catalog lookup or existing warehouse draft preparation. Strict local intent
models and a tool allowlist own that boundary; provider output cannot choose
database IDs or callbacks. The pilot is private-`super_admin` only and disabled
by default. It has no direct SQL, filesystem shell, systemd, polling, migration,
deployment, catalog-editing, or unrestricted mutation access. Optional general
chat and Google Search grounding are separate text-only outputs behind disabled
flags; web content cannot select or invoke a local tool, and source links come
only from provider grounding metadata.
Warehouse mutations retain preview, authorization recheck, idempotency, and
explicit confirmation.

## D13 — Persistent AI context is bounded and not authority

Bar Jarvis dialog state lives in schema v13, isolated by user/private-chat and
bounded to 24 hours, 12 turns and 12000 characters with oldest-first eviction.
Structured topic/entity/tool fields may resolve follow-ups but never authorize
warehouse mutations. Read-only operational tools use fixed Pydantic contracts,
centralized server-side role checks and existing read services; Gemini cannot
choose SQL, tables, database identifiers or mutation methods.

## D14 — Preserve typed local-first relation semantics

Confident read-only catalog requests execute locally without Gemini. A local
result cannot be replaced by a generic provider error, and a fuzzy candidate
is never selected automatically. Parser, intent, orchestration, retrieval,
persistent context and renderer/callback must preserve the typed request
contract end to end; no layer may infer discarded fields from rebuilt text.
Provider planning receives only the capabilities of the selected domain.
Consequential actions remain inside the existing persistent
draft/proposal/explicit-confirmation and server-side authorization boundaries.

## D15 — Measure the Bar Jarvis end state before claiming it

The owner-approved target is a text and voice natural-language pilot over all
actor-authorized BarBot capabilities, with persistent multi-step context and
bounded local, Gemini, web, image and video sources. Completion claims require
versioned owner-approved corpora and reproducible scoring. Target thresholds
are at least 90% for catalog/context retrieval, warehouse draft preparation,
all-capability routing/arguments and voice end-to-end success; permission,
confirmation, idempotency, rollback and unsafe-false-positive security cases
must pass 100%. Unmeasured capabilities stay `pending`. This direction never
authorizes production access or weakens local-first, preview/confirmation,
stable-ID, server-side authorization or no-silent-mutation rules.
