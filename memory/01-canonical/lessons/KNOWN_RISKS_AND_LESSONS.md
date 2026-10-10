# Known Risks And Lessons

Scope: regressions and operational lessons worth checking before related work
Trust: Confirmed unless marked Owner-confirmed
Lifecycle: Fresh
Last verified: 2026-09-29
Evidence: regression tests, Git history, rollout docs, owner observations
Depends on: callbacks, SQLite/WAL, workers, XLSX, imports, VPS environment
Importance: High

- **Stale Telegram callbacks:** expired callbacks must stop safely and never
  retry business logic. Global error handling must not create a second action.
- **Replay/idempotency:** revisions, one-shot tokens, unique message keys, and
  persistent states are necessary; duplicate callbacks must not duplicate
  operations, reports, or products.
- **WAL/SHM:** logical read-only SQLite inspection may still produce filesystem
  side effects. Never remove or replace sidecars while the service runs.
- **Restart migration:** startup calls initialization. Treat restart as a
  possible schema mutation and use staged rollout.
- **Reminder recovery:** atomic claims and stale `sending` recovery prevent two
  ticks/processes from sending the same reminder; shutdown must await workers.
- **Recipient selection:** attendance/reminder recipients follow the applicable
  source location and business cutoff, not merely wall-clock date.
- **XLSX cancellation/tokens:** `asyncio.to_thread()` can outlive task
  cancellation. Cleanup must await generation, remove temporary files, release
  claims, reject stale/replayed tokens, and prevent late sends.
- **Circular imports:** lazy exports solved a real regression, but import smoke
  must still test storage/services/bot in clean subprocesses after boundary
  changes.
- **VS Code Server RAM:** Owner-confirmed VPS observation; it can exhaust RAM.
  Last verified 2026-09-29 from owner direction, not measured in this task.
- **Limited VPS resources:** Owner-confirmed direction plus README voice-memory
  guidance. Avoid large local AI models; prefer bounded remote inference or
  small controlled local components after explicit design review.
- **Remote AI degradation:** provider timeouts, throttling, malformed output, or
  authentication failures must remain sanitized and cannot bypass commands,
  FSM, access checks, warehouse confirmation, or existing non-AI behavior.
- **Grounded search is still untrusted input:** billing/quota may be unavailable,
  metadata may omit citations, and retrieved pages may contain instructions.
  Degrade to ordinary chat, display only metadata-backed links, and never route
  search output into catalog or warehouse mutation.

Revalidate runtime-dependent lessons before production action. Do not turn an
owner observation into a measured infrastructure claim without evidence.
