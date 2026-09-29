# Patterns Index

Scope: reusable BarBot implementation patterns
Trust: Confirmed
Lifecycle: Fresh
Last verified: 2026-09-29
Evidence: `app/`, regression tests
Depends on: architecture and services
Importance: High

- Handlers own Telegram/FSM orchestration; services own business rules; storage owns persistence; keyboards own UI construction.
- Authorization is recalculated server-side at service/storage mutation boundaries, not inferred from visible buttons.
- Callback payloads use compact typed IDs and revisions; stale/replayed actions fail safely.
- SQLite mutations use explicit transactions, optimistic revisions, idempotent claims, or unique keys according to the workflow.
- User text is normalized at boundaries and escaped before Telegram HTML rendering.
- Catalog data stays data-driven; stable IDs survive rename/import.
- Runtime recovery uses persistent states plus stale-claim recovery rather than in-memory assumptions.
