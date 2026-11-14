# Freshness Manifest

Read isolation slice in development 2026-11-14 from `47a8e869...`: in-process
new-handle guards do not protect pre-opened native writable handles. Owner permits
only exact SQLite WAL/SHM runtime changes. Exec workers/typed manifest IPC/kernel
file authority and pre-executor sealing are implemented in the working tree;
targeted evidence is 2415 AI + 125 adjacent tests and 8 additional adversarial
cases. Seven offline measurements give 1736.5/1932.6 ms median/p95 end-to-end and
66,308 KiB maximum child RSS. Final full pytest: 3253 passed, including 74
process regressions. Post-media targeted checks: 126 relevant tests and 6 media
checks passed (overlapping coverage). All 18 read DTO results matched canonical
execution on a pinned temporary WAL snapshot. Earlier interrupted full output
is not pass evidence. Compile/import/link/JSONL/secret/artifact/Git checks pass.
Schema v15, catalog/seed/migrations and production/runtime are not changed or
revalidated by this task. No live/provider-readiness claim follows.

Cleanup slice revalidated offline 2026-11-13 from `e8482f5...`: independent
rollback/close/verification and unconditional restoration finally; native forced
close only for supported SQLite handles; explicit fatal diagnostics on unreleased
handles. Body exceptions are preserved via grouping/control-exception chaining.
Ownership is connection-per-operation, with idempotent duplicate/nested cleanup;
external handles/pools/native plugins/escaping cursors remain unsupported pending
review. Evidence: temporary-v15 fault matrix and fake evaluation, never live API.
Schema v15/catalog/seed/migrations and production/runtime facts are unchanged or
uninspected; no production-readiness or semantic-quality claim follows.

Read-only capability slice reviewed offline 2026-11-09 from `3f7e6c...`:
`get_required_status_snapshot` computes virtual/materialized effective status
without persistence; ordinary overview delegates to it. Materialization belongs
to explicit start. AI read executors enforce query-only/authorizer and scoped
external-effect denial; evaluator composes authorizers and treats violations as
safety exit 2. Regression evidence is temporary-v15/fake-transport only. Extended
live metrics are partial (117/183, stopped on P1), not release proof. Schema v15,
catalog, seed and migrations unchanged; live semantic quality/runtime pending.

Service-word entity slice revalidated offline 2026-11-09 from `d515ed0...`:
single clause-level action extraction, closed typed spans, no action stripping
inside a body, direct rendering of server-owned facts and exact request-scoped
reuse at legacy interfaces. Full official names precede aliases/candidates;
provider product/target substitution is denied. Frozen v1.1/v1.2 are unchanged;
v1.3 is a separate 360-case deterministic extension with real temporary-v15
handler/service ledger checks. No catalog/seed/schema/migration change or live
quality claim. Extended live coverage and production runtime remain pending.

Unicode/token-role slice revalidated offline 2026-11-09 from `a15759c...`:
default-ignorable/control fail-closed inspection, detection-only representation,
grammar-assigned entity roles with numeric positions and typed FSM cancellation.
Evidence: frozen v1.1 seed 20261109 plus separate v1.2 parser/FSM/temporary-v15
ledger tests. Existing v15 JSON, catalog, seed, schema and migrations remain
unchanged. Live extended quality and all production runtime facts remain pending
separate authorization and revalidation.

Compound-cancellation slice revalidated 2026-11-09 from `27f0b9c...`: classify
bounded typed clauses before comments, fail closed on contradiction/unknown
action tails, recognize only bounded one-edit cancellation typos, preserve
ordinary reasons and positive `не забудь + action`. Evidence is synthetic real
handler/FSM/services plus the temporary-v15 ledger. Optional clause metadata is
v15 JSON-compatible; schema/catalog/seed unchanged. Extended live evaluation
and production facts remain pending separate authorization and verification.

Mutation-language slice revalidated 2026-11-09 from `96f50da...`: typed local
disposition, cancellation-before-evidence, whole-token Decimal grammar,
unambiguous correction and fail-closed multiline numeric invalidity. Typed
missing-field state reuses the existing conversation JSON, without schema or
migration changes. Evidence is synthetic handlers/services/ledger only; live
provider quality and production runtime remain pending. Catalog/seed unchanged.

Four-finding provider/evaluation slice revalidated 2026-11-09 from `c298961...`:
typed terminal outcomes, status-bounded diagnostics, active-consumer config and
resolved-DB quantity observation across suites. Evidence is offline; owner quota
figures are documentation, not new API measurement. Ten-second Gemini pacing,
catalog/seed/schema/migrations and production configuration are unchanged.

Provider-policy slice revalidated 2026-11-09 from `197a765...`: Gemini-first,
disabled conditional Groq fallback, process-local admission and physical pacing,
one logical deadline and safe local/clarification termination. Prior six-case
paced results select a candidate only; owner-only shadow and broader live
quality remain pending. This development changes composition/config defaults,
not the real environment, production state, catalog, seed or schema/migrations.

Scoring slice revalidated 2026-11-03 from `a1900e19...`: actual temp-DB structural
snapshots/statement ledger, typed allowed transitions, expected-domain metrics
and confusion matrix. P1/P2 are covered by offline regressions. Corpus counts,
production composition, catalog, seed and migrations are unchanged. Live
provider quality/runtime facts remain pending separate authorized verification.

Relevant evaluation slice reviewed 2026-11-03 from `bd73be96...`: live corpus v2
and selectable/seeded suites are offline development evidence only. Synthetic
target remains v15; production composition, catalog, seed and migrations are
unchanged. Live results/quality remain pending; revalidate suite/version/seed
and physical budget before any separately authorized live run. Runtime facts
and production permissions were not inspected or changed.

Metadata: scope `BarBot`; trust `mixed`; lifecycle `active`; last reviewed
`2026-10-23` during two-stage planner development based on starting HEAD
`7871802965d3fa4700e982551b744f370f44f25b`.

Conversation-brain development was revalidated on 2026-10-23 from base
`b21ec7e69d213e781c3b5188c0e027844a14122d`. Schema target is v15; catalog and
warehouse seed content are unchanged; production rollout/runtime facts remain
pending revalidation.

| Note | Scope | Importance | Trust | Lifecycle | Last verified | Evidence / dependencies | Revalidation trigger |
|---|---|---:|---|---|---|---|---|
| `01-canonical/summary/PROJECT_SUMMARY.md` | whole project | high | mixed | active | 2026-09-29 | repository source; owner runtime statement last verified 2026-09-24 | architecture, release, or live-runtime task |
| `01-canonical/summary/ARCHITECTURE.md` | application | high | verified | active | 2026-10-22 | handlers/services/storage; provider-neutral planner boundary | composition or package-boundary change |
| `01-canonical/summary/WAREHOUSE_DOMAIN.md` | warehouse | high | mixed | active | 2026-09-29 | models/services/storage/tests; owner-confirmed production counts | schema, domain rule, or production inspection |
| `01-canonical/summary/PERMISSIONS.md` | authorization | critical | verified | active | 2026-09-29 | access/auth services and access tests | role or permission change |
| `01-canonical/summary/PRODUCTION_SAFETY.md` | rollout | critical | mixed | active | 2026-09-29 | `AGENTS.md`, rollout docs; owner runtime statement last verified 2026-09-24 | every production task |
| `01-canonical/summary/CATALOG.md` | catalog | high | verified | active | 2026-09-29 | `data/catalog.json`, importer/tests, commit `a57cc389...` | catalog/import change |
| `01-canonical/decisions/BARBOT_DECISIONS.md` | cross-cutting | high | verified | active | 2026-10-22 | implementation and tests through provider-neutral planner development | contradicting implementation or owner decision |
| `01-canonical/lessons/KNOWN_RISKS_AND_LESSONS.md` | reliability | high | mixed | active | 2026-10-22 | source/tests/history and labeled owner observations | regression or new incident |
| `01-canonical/reference/EVIDENCE_MAP.md` | routing | medium | verified | active | 2026-09-29 | repository paths | file move or ownership change |
| `features/FEATURE_INDEX.md` | feature routing | medium | verified | active | 2026-10-14 | repository paths | feature/package change |
| `features/AI_ASSISTANT.md` | AI contract | high | verified | active | 2026-10-22 | assistant source/tests including strict provider wire boundaries | assistant routing, context, capability or schema change |
| `features/BAR_JARVIS_ROADMAP.md` | AI product goal/roadmap | critical | owner-approved | planned | 2026-10-14 | owner-approved Definition of Done; implementation evidence is separate | owner changes goal, threshold, phase order or safety boundary |
| `features/AI_CAPABILITY_LANGUAGE.md` | AI language coverage | high | mixed | active | 2026-10-14 | `CAPABILITIES`; verified tests; explicit pending cells | capability inventory or verified-language change |
| `symbols/SYMBOL_INDEX.md` | symbol routing | medium | verified | active | 2026-09-29 | repository symbols | rename or package change |
| `02-working/MEMORY_UTILITY_LOG.md` | utility pilot | medium | verified | active | 2026-09-29 | local event contract | after 10 eligible tasks |

Owner-confirmed runtime facts are not promoted to live-verified facts by this
manifest. They must be revalidated under the applicable production stage before
being used for an operational decision.

## Current development checkpoint

- 2026-10-28: Development from `b0c2780...` hardens only isolated evaluation:
  independent all-step readiness aggregation, structured authorization attempt
  versus execution observations, and exclusive owned temporary DB identity
  checks before/after opens. Regression risks: second-step failure masked by
  successful read, alias/hardlink inode escape, and counters inferred from error
  strings. Static budget estimates 17/25/34/68 are not live measurements.
  Production composition/catalog/seed/migrations are unchanged; live quality
  remains pending, and same-UID hostile filesystem races are not a remote sandbox.

- 2026-10-27: Isolated evaluation development starts from `c1496bf...`.
  Distinct scripted/live-isolated/shadow modes, run-wide physical HTTP budget,
  owned temporary DB/services, explicit synthetic lifecycle actions and bounded
  reporting are covered offline. Production composition, catalog/seed and
  migrations remain unchanged. No live evaluation has been run; provider
  quality, completion and latency remain pending owner-authorized measurement.

- 2026-10-27: Based on `18e94c4...`, only CHECK-expression normalization is
  changed from quadratic to O(n) time/O(n) auxiliary memory: one matching-stack
  pass, indexed boundaries, one final slice, no recursion. Correctness, counted
  token visits and deep nesting supplement existing spoof/migration tests.
  The five security findings remain closed, not re-audited here. Schema DDL,
  target v15, catalog/seed and migrations stay unchanged; live provider quality
  and all runtime facts remain pending separately authorized verification.

- 2026-10-24: Narrow v15 CHECK-validator hardening starts at `cef9a79...`.
  Lexical extraction replaces spoofable SQL regex matching; schema DDL, target
  v15, catalog/seed and migrations are unchanged. Evidence is temporary SQLite
  and offline tests only; production/live facts remain pending revalidation.

- 2026-10-24: Conversation Brain security fixes are based on `2d180750...`.
  The relevant contract is `features/BAR_JARVIS_CONVERSATION.md`: untrusted
  typed memory/tool data, canonical planner dispatch, chat-owned expiring
  callbacks, full v15 invariants and fail-closed scripted evaluation.
  Development target is v15; prior v14 checkpoints below are historical.
  Production/runtime facts and live/shadow accuracy still require separate
  authorized verification. No rollout is implied by offline security tests.

- Schema target remains v14. The planner foundation changes do
  not change catalog, warehouse seed or migrations.
- Planner modes and the 17-case offline corpus are development evidence only;
  shadow/AI-first live coverage and accuracy remain pending.
- Shadow supervision and privacy-safe telemetry are development evidence only;
  live Cases 2–4 remain unknown and require a separately authorized re-smoke.
- Groq-primary/Gemini-fallback now uses compact Stage 1 routing, local policy
  assembly and separate bounded Stage 2 argument contracts. Live route quality
  and production rollout remain pending.
- The former monolithic correction schema failed constrained generation on
  Groq 20B and 120B. The compact replacement has offline contract evidence
  only and requires a separately authorized live route smoke.
- Production rollout of the development changes has not been performed.
- Service state, deployed HEAD, schema, counts, flags and provider health remain
  runtime facts and must be revalidated under the controlled rollout policy
  before deployment.
