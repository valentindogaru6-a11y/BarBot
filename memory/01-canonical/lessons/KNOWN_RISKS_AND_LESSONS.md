# Known Risks And Lessons

Scope: regressions and operational lessons worth checking before related work
Trust: Confirmed unless marked Owner-confirmed
Lifecycle: Fresh
Last verified: 2026-10-14
Evidence: regression tests, Git history, rollout docs, owner observations
Depends on: callbacks, SQLite/WAL, workers, XLSX, imports, VPS environment
Importance: High

- **Terminal is not unavailable:** flattening safety/model/authority failures
  into generic availability enabled a local draft after refusal. Preserve typed
  terminal outcomes through the handler; local imperative evidence cannot erase
  them. HTTP status bounds body diagnostics: a body rate-limit code on 400 must
  not authorize failover or an availability circuit.
- **Dependency and observation contracts need vertical tests:** disabled
  fallback must not require/create its provider. Scorer unit tests alone missed
  absent actual quantity/unit fields; derive typed canonical values from resolved
  temporary DB rows and verify readiness through the real runner.

- **Provider failover is not backpressure:** busy/cooldown requests must not fan
  out to another provider. Count at the physical HTTP boundary and share limits
  across users; keep claims/cancellation/shutdown bounded. A tool/proposal
  boundary forbids fallback. Groq schema failures still occurred in paced tests;
  it remains a conditional candidate, not a healthy default reserve.
- **Scoring equivalence is dimensional:** gram/грамм/g are equivalent mass
  aliases, not a reason to accept volume/count. Informational warehouse-domain
  answers can be read-only; actual effects and runtime risk, not domain alone,
  decide safety. Six diagnostic cases do not prove broad live accuracy.

- **Conversation state separation:** successful entity/task facts must remain
  separate from failed attempts, candidates and active proposals. Provider
  failure must not erase the last stable selection.
- **Evaluation claims:** scripted planner outputs are not conversational
  accuracy. Percentages require a versioned corpus, frozen runner, recorded
  predictions and methodology.
- **Long-term memory is a mutation:** only explicit owner instruction or a
  confirmed preview may write; ambiguous deletion/update must stop for exact
  selection.
- **Confirmed memory is still untrusted data:** normalize and reject known
  multilingual authority/secret/SQL/shell instructions before preview; carry
  typed quoted records separately from runtime policy. No prompt or remembered
  preference can create a capability, permission, target or confirmation.
- **Planner dispatch must consume the canonical plan:** re-running regex on
  raw text after planning loses the selected capability/arguments. Bound the real
  iterative path, feed only typed inert results back, and stop at any preview.
- **Schema-validator false positives block release:** column names and a few
  SQL substrings do not prove security constraints. Compare PRAGMA metadata,
  indexes/FK actions and parsed CHECKs; test real weakened schemas individually.
- **Quoted-identifier CHECK spoofing:** regex extraction accepted an absent
  revision constraint when a FOREIGN KEY was named `"CHECK(revision>0)"`.
  Tokenize SQLite DDL before extracting balanced real CHECK expressions; never
  interpret strings, quoted identifiers or comments as constraints. Preserve
  literal values and token boundaries during normalization. Parameterized
  regressions cover revision, status/kind, token/content length and time checks;
  the malformed reproduction accepts revision zero but must fail validation.
- **CHECK normalization must stay linear:** repeatedly scanning/slicing one
  outer parenthesis pair at a time was quadratic. Build matching-close indices
  once with an iterative stack, move left/right boundaries, then slice once.
  Test token-position visits and copied positions (including through slices),
  not fragile timing ratios; deep nesting must not use recursion. The five
  security findings were already closed before this narrow complexity fix;
  live provider quality remains pending.
- **Evaluation success must include semantic contracts:** a safety-only PASS
  hid route accuracy 0.0. Freeze completeness/coverage/metric thresholds, reject
  missing/non-finite values, expose failed metric names and test CLI exit codes.
- **Memory callbacks need chat ownership:** actor/token alone is insufficient.
  Save and separate delete previews bind owner plus source private chat,
  revision and expiry, with access rechecked within the write transaction.

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
- **Failed follow-ups must not erase successful context:** literal pronoun
  extraction, clearing structured state after `not_found`, and rebuilding a
  similarity query from display text can turn stable relation follow-ups into
  fuzzy lookup or needless clarification. Keep successful entity, candidates,
  clarification and last attempt distinct; resolve references by stable ID and
  test provider failure, restart, reset and actor/chat isolation.
- **Explicit warehouse commands need a deterministic pre-provider route:** a
  fully parseable imperative that falls through to provider planning can end in
  a provider error instead of the existing safe draft flow. Require imperative
  evidence, typed Decimal quantity/unit/destination parsing and the existing
  draft/preview/confirmation boundary; keep informational language negative
  tests alongside mutation tests.
- **Planner output is not execution authority:** filter capability contracts
  before the request, reject invented IDs and risk mismatches afterward, and
  recheck access/proposal state at execution. Start with shadow. Provider
  failure must retain successful context and cannot create a mutation.
- **Shadow must not extend primary latency or claims:** directly creating and
  awaiting comparison work made provider timeout part of the Telegram response
  and kept the actor single-flight claim busy. Route all shadow work through a
  bounded executor-free supervisor, emit privacy-safe terminal telemetry, and
  cancel/drain on shutdown. Prove the primary claim is reusable while shadow
  remains pending.
- **Diagnostic evidence must survive later-case failures:** a validated live
  result was lost when a harness applied `dataclasses.replace()` to a Pydantic
  model and subsequent output was truncated. Retain each bounded result before
  the next call, use Pydantic `model_copy`, and stop when call counters diverge.
  Cases 2–4 remain unknown and establish no accuracy claim.
- **Canonical Pydantic schemas are not automatically provider-compatible:** a
  nested schema with defaults, `$ref` or open dictionaries can be rejected even
  when provider transport is healthy. Keep a versioned closed wire schema per
  provider, validate it first, convert explicitly to the canonical decision,
  and derive risk/stable references locally. Never relax the canonical model or
  pass provider output directly to an executor to accommodate one API.
- **Fallback can multiply latency, cost and ambiguous work:** do not race
  providers. Use one shared deadline, no shadow fallback by default, no retry
  for auth/config failures, and no automatic retry of mutation execution.
  Circuit state is process-local health only and must not become authorization
  or persistent business state.
- **A generic HTTP-400 label destroys the evidence needed for safe provider
  work:** the first Groq planner lookup passed its strict contract, while the
  following correction returned 400 and the old adapter retained only
  `schema_incompatible`. Do not retain provider prose to compensate. Keep a
  closed error envelope with allowlisted type/code/parameter and hashes; prove
  schema invariance offline, and distinguish invalid request schema from
  generated-output mismatch, unsupported parameters and refusals. Correction
  semantics remain pending until a separately authorized live re-smoke.
- **A valid schema may still be too ambitious for constrained generation:**
  the same 11-field monolithic correction contract returned
  `json_validate_failed` on Groq 20B and 120B. A larger model did not change
  the boundary. Keep routing compact, map targets by server-owned index and
  validate arguments independently. The compact route still requires a
  separately authorized live smoke; offline tests are not accuracy evidence.

Evaluation regression: UI preview flags are not a DB mutation oracle. Read-only
cases must detect unrequested drafts/proposals and transient writes using safe
before/after state plus a statement ledger. Attribute route failures to expected
domain; actual-domain denominators can hide a broken capability group. Domain
gates require the metrics actually tested by the suite and explicit zero safety
counters. Offline evidence does not establish live provider quality.

Revalidate runtime-dependent lessons before production action. Do not turn an
owner observation into a measured infrastructure claim without evidence.
