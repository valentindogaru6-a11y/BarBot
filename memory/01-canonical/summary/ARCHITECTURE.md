# Architecture Summary

Scope: application boundaries and runtime control flow
Trust: Confirmed
Lifecycle: Fresh
Last verified: 2026-10-14
Evidence: `app/bot.py`, `app/handlers/`, `app/services/`, `app/storage/`, `app/keyboards/`, tests, commit `c2ce9fd2ae46509b8ed47aa6d44e1cc1b3ccf80f`
Depends on: current Python application structure
Importance: Critical

- `app/bot.py` is the composition root. It loads validated settings, initializes
  the database, builds services/routers, registers commands, starts reminder and
  schedule workers, then owns long polling and orderly worker/session shutdown.
- Handlers translate Telegram messages/callbacks and FSM state into service
  calls. They must not execute SQL directly.
- Services own validation, authorization orchestration, normalization, business
  dates, resolution, reports, delivery, reminders, schedules, and corrections.
- Storage owns SQLite/JSON persistence and transactions; it contains no
  Telegram UI. Mutating repositories accept authorization callbacks where a
  final in-transaction permission check is required.
- Keyboards own inline navigation and compact typed callback payloads. Tests
  enforce Telegram's 64-byte callback limit.
- `JsonCatalogStorage` performs validated and atomic catalog writes. Catalog
  handlers load the current JSON so data updates do not require architectural
  hardcoding.
- `UserDatabase.initialize()` runs sequential transactional migrations and
  validates schema v14. Startup therefore can migrate and must be controlled in
  production.
- Admin audit is an append-only JSONL concern; warehouse mutations have their
  own transactional SQLite audit trail.
- Release inspection uses a separate logical read-only SQLite connection and
  does not call initialization, migrations, checkpoint, journal mutation, or
  chmod. SQLite may still touch WAL/SHM runtime sidecars.
- `app.handlers.assistant` is an opt-in last-resort text router plus a gated
  voice router. Provider adapters return strict intents to
  `AssistantOrchestrator`; schema-v13 conversation storage keeps bounded topic
  and turn context across restart. `AssistantToolRegistry` can only search the
  catalog or translate input into the existing warehouse persistent-draft
  flow, while `AssistantReadOnlyTools` delegates allowlisted attendance and
  shift reads through centralized access checks.
  Schema-v14 action proposals add restart-safe, opaque confirmation state;
  `CapabilityRegistry` filters actions by role, private-chat policy and flags.
  General chat returns escaped text only; the separate grounded-search adapter
  returns text plus metadata-backed links and has no registry access.
- Bar Jarvis catalog requests follow one typed contract across `Local parser →
  AssistantIntent → Orchestrator → Catalog retrieval/tool → Persistent
  context → Renderer/callback`. The contract carries `domain`, action,
  entity/query, `requested_kind`, relation, `relation_target`, stable candidate
  and selected catalog IDs, plus confidence/evidence when applicable. No
  intermediate layer may reconstruct these values only from newly assembled
  text or silently discard fields it does not understand. Callback/context
  continuation uses stable catalog IDs rather than environment-specific
  numeric identifiers.

Exact control flow or data shape must be re-read from source; this summary is a
router, not a substitute for code review.
