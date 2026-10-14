# Decisions Index

Scope: durable BarBot architecture and safety decisions
Trust: Confirmed
Lifecycle: Fresh
Last verified: 2026-10-14
Evidence: `app/`, `tests/`, `docs/production_*.md`, Git history through `c2ce9fd2ae46509b8ed47aa6d44e1cc1b3ccf80f`
Depends on: current application architecture
Importance: Critical

Read [BarBot decisions](BARBOT_DECISIONS.md) for the compact decision set:

1. SQLite with sequential validated migrations.
2. Kyiv business date with a 23:00 cutoff.
3. Integer-scaled quantities; never binary float.
4. No silent fuzzy acceptance.
5. Whole-message atomic intake batches.
6. Warehouse audit separated from admin audit.
7. Logical read-only release inspection.
8. Verified backup before hardening/migration.
9. Personal attendance sessions separate from warehouse shifts.
10. Confirmation before mutations.
11. Preserve production-added products during seed reconciliation.
12. Future AI can call services only, never SQL/shell/systemd directly.
13. Persistent AI context is bounded state, never mutation authority.
14. Preserve typed local-first relation semantics across every AI layer.
15. Measure the owner-approved Bar Jarvis end state on versioned corpora before claiming completion.
