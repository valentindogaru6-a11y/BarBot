# Known Risks And Lessons

Scope: regressions and operational lessons worth checking before related work
Trust: Confirmed unless marked Owner-confirmed
Lifecycle: Fresh
Last verified: 2026-10-14
Evidence: regression tests, Git history through `c2ce9fd2ae46509b8ed47aa6d44e1cc1b3ccf80f`, rollout docs, owner observations
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
  Reject answers without usable grounding metadata/support, display only
  credential-free metadata-backed links, never route search output into local
  mutation, and never automatically retry an ambiguously completed billable
  search call. The prompt's two-query request is soft, not an enforceable API
  limit.
- **Provider intent is not mutation authority:** writeoff/transfer drafts require
  explicit local command evidence or a live pending context created from that
  evidence. Keep both the dynamic provider enum and local post-validation;
  prompt instructions alone are not a security boundary.
- **Relation metadata can be lost between AI layers:** dropping
  `requested_kind` or relation between the local parser, `AssistantIntent` and
  orchestration degraded a related-card lookup into ordinary fuzzy search.
  Commit `c2ce9fd2ae46509b8ed47aa6d44e1cc1b3ccf80f` protects the route with strict
  enums, forbidden unknown intent fields, end-to-end contract regressions and
  backward-compatible persistent context. Re-test parser → intent → tool →
  callback/context whenever one of these boundaries changes.

Revalidate runtime-dependent lessons before production action. Do not turn an
owner observation into a measured infrastructure claim without evidence.
