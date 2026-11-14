# Bar Jarvis Conversation Brain

Scope: bounded multi-turn context, confirmed memory and guarded tool loop
Trust: implemented and tested offline; live provider/shadow evidence pending

## Read-only execution contract (P1 regression)

### Process authority (memory hardening from `a2e01b9...`)

The earlier live-DB worker boundary failed native shared-memory probes:
MAP_SHARED_VALIDATE passed a private-bit check, and unconditional mprotect
upgraded a read mapping. Checksums/business hashes did not detect direct SHM
changes. That live-mapping architecture is superseded, not a closed finding.

The parent owns a fresh 0700 temporary root/0600 clone, bounded to 64 MiB. An
exec coordinator supervises a fixed Backup API program; neither runs capability
code. The coordinator supplies only validated RO source DB/WAL/SHM descriptors
via ADDFD, and RW authority only over the owned destination. It is a separate
process because opening/closing a live DB inode in the SQLite-owning parent can
release that parent's POSIX locks. No source sidecar creation/repair is allowed.

Active WAL is read normally with existing readable sidecars. For absent/empty
WAL only, a kernel read lease must first exclude all writable handles/mappings
and remain unbroken until copy completion; only then is immutable reading safe.
Lease break or unsupported RO-WAL/lease state discards the clone. Never apply
immutable blindly to a source that may have committed WAL frames.

The Backup API clone is checked (integrity/FK) and normalized to DELETE journal
before a separate executor is spawned. No live source descriptor/mapping/path
is passed to the executor. Source commits after that point do not alter its
consistent snapshot. Normal/error/timeout/cancellation paths reap processes
before clone removal. Source data never enters diagnostics. This is not an
authorization for production access or a measured live-readiness claim.

Risk-0 execution uses a short-lived exec worker with closed JSON IPC.
The parent never serializes a service, callable, connection, cursor or closure.
Parent writable SQLite handles do not survive exec/close-fds. A shared manifest
is derived from `CAPABILITIES`, schemas and executor source hashes; it rejects
mutations/unknown capabilities and incompatible peers. The worker rechecks
actor/private-chat/pilot/role/flags against trusted facts and read-only DB state.
Only a per-request opaque actor reference crosses IPC, not a Telegram identifier.

Linux seccomp user notifications mediate clone file opens: the broker supplies a
validated fd rather than continuing a syscall against a mutable pathname.
Main DB access is RO; the worker itself opens `mode=ro`, enables query-only and
deny-write authorizer, and leases a single consistent read snapshot to services.
There are no writable file-backed mappings in this process, even before seal.
Exact private anonymous allocations and approved-descriptor private COW remain
allowed; shared writable/unknown sharing flags are rejected. All mprotect and
pkey_mprotect calls, mremap, msync, remap_file_pages and new shared-memory/FD
authority are denied, rather than tracking partial/unknown memory ranges. New
module imports and ctypes/cffi/dynamic loading are denied after seal. Prepared
native pointers are still hostile and constrained by the kernel/RO backing FDs.
No network,
new process/thread, arbitrary file or writable second DB authority is available.
No seed, migration or business schema change is involved.

Results are bounded materialized display DTOs; parent renderers must not re-query
writable services to complete them. Catalog media preserves validated opaque
Telegram file references, never a local path or URL supplied as media authority.
Draft/proposal/confirmation mutations remain
in the parent under existing centralized permission/revision/idempotency checks.
Verification covers DB checksum/metadata, inode/link identity, sibling entries,
business fingerprint, authorizer attempts, integrity and FK on the clone. A live
parent writer retains its locks/authority. A lease conflict invalidates snapshot
creation; a later commit does not change a completed clone. Partial results never
count as successful execution. Source bytes are protected by RO OS authority,
not a claim that arbitrary SHM writes can be identified as SQLite-native.

Worker timeout/cancellation/crash must kill/reap the child and close pipes/listener
fds. Blocking notification receive has a bounded native thread-directed wakeup;
its borrowed unused realtime disposition/timer/mask must always be restored.
Supported runtime requires Linux/seccomp notify+ADDFD support and libseccomp;
unsupported runtime fails closed, never falls back to in-process tool execution.
Pooling/native plugins need separate adapters/review and cannot bypass the broker.

Static UI/FSM navigation handoffs remain pure parent instructions after the
central gate; they do not fetch business results or invoke a service executor.
All 18 actual bound read capabilities use the broker. A standalone catalog cache
can use the same zero-filesystem worker without DB authority; live handlers pass
the real access context for the worker's repeated policy/role/flag checks.

Historical live-DB-worker measurement (not the new snapshot architecture): startup
median/p95 1326.7/1504.4 ms, execution 37.3/43.0 ms, end-to-end 1736.5/1932.6 ms;
maximum child RSS 66,308 KiB. Two concurrent readers completed in 4527.8 ms.
These are observations on the development host, not production SLAs; four
concurrent children require corresponding memory/headroom. Prefer correctness
over a persistent worker that could retain authority or user state.

Historical pre-hardening test evidence: 2415 AI tests, 125 adjacent config/access/reminder/delivery
tests and 8 additional DB/WAL/SHM replacement/invented-result cases passed.
After the media DTO correction, 126 relevant tests and 6 focused media checks
passed (overlapping suites, not additive unique coverage). Final full pytest:
3253 passed; this includes 74 real-process/authority/cleanup regressions. An
earlier full attempt was interrupted for the media correction, not counted as
a pass. All 18 read bindings also matched the canonical materialized results
on a pinned temporary WAL snapshot. Those tests missed native mapping bypasses.
Current snapshot/memory offline validation is complete. Live provider quality,
unsupported native/plugin adapters and production acceptance remain pending.

Current hardening evidence (offline, synthetic v15): 211 targeted tests passed,
plus 2 stronger mapping error/cancellation checks. All 18 materialized read
results independently matched prewarmed canonical executors. Non-root
snapshot/seccomp/read-lease execution passed. Final full pytest: 3294 passed
on the frozen application tree (one full run for this change).

New snapshot pipeline measurement (7 serial reads, tracked catalog, synthetic
DB approximately 0.5 MiB): median/p95 wall 3618.1/4072.4 ms, worker startup
1864.6/2236.0 ms, execution 47.8/55.9 ms; reported helper-copy phase
308.2/413.3 ms excludes coordinator import time. Observed peak RSS: parent
87,360 KiB, worker 61,960 KiB, coordinator 37,520 KiB, backup helper 37,008 KiB.
Two concurrent reads completed in 8492.0 ms; peak 4 child processes and 20
parent FDs. FD/process/root cleanup and business-write zero checks passed.
These small-sample observations are not a production SLA or proof of maximum
64-MiB clones under four-way load. 1-GiB VPS sizing should begin with the
owner-only single-flight pilot and revalidate free temp space/headroom before
rollout. Each clone is at most 64 MiB; process virtual-memory ceilings are
192 MiB. Native/plugins and live quality remain pending separate review.

### Exception-safe ownership and teardown

From development base `e8482f5...`, cleanup has independent rollback, close,
verification and application-restoration stages. The execution scope owns every
new native SQLite handle observed inside it, including helper-created handles;
duplicate registration/cleanup is idempotent. Multiple handles are supported.
Repositories must consume/close cursors inside the operation: escaping cursors,
external writable connections, pools and native/plugin executors are unsupported.

A rollback error never skips close or cleanup of later connections. A broken
instance close falls back to the captured `sqlite3.Connection.close` descriptor
only for supported native SQLite connections/subclasses. Closed handles are not
effectively closed twice. If base close fails and the handle remains usable,
the normalized diagnostic explicitly marks `connection_leaked`; execution and
evaluation readiness fail. Physical release cannot be promised after native
close failure, but request registry/context/counter/hooks are still released.

An independent outer finally restores contextvars, loop hooks and the atomic
bytecode counter regardless of resource errors. Body errors remain intact;
ordinary simultaneous failures use BaseExceptionGroup. Cancellation/fatal
control exceptions remain primary with cleanup failures chained, preserving
asyncio cancellation state. Handlers produce a safe error without provider
fallback; evaluator treats recoverable and fatal cleanup failures as safety exit
2 and emits only normalized cleanup metadata. Pooling/native/plugin support and
live semantic quality remain pending a separate review.

Evidence: `tests/test_assistant_read_only_cleanup.py` covers injected rollback/
close/control errors, native forced-close success/failure, DELETE/WAL lock probes,
multiple/duplicate/nested ownership, registration errors, two threads/event loops,
query/close cancellation, safe diagnostics and real fake-transport evaluation.

`warehouse.required_status` must never materialize state. Its immutable
`RequiredStatusSnapshot` is computed from a single SQLite read snapshot of the
current shift, applicable active rules, frozen requirement rows and operations.
Unmaterialized requirements are virtual pending entries. Existing rows and
timestamps are not refreshed by viewing; `overview()` is now read rendering too.
Only the explicit required/closing start workflow materializes the snapshot;
AI `warehouse.start_required_flow` still hands off to the authorized UI/FSM.

Every planned risk-0 dispatch, deterministic controller read branch and direct
AI read tool runs under `read_only_execution`. Connection-per-operation SQLite
handles are query-only, write opcodes/state-changing PRAGMAs denied, then closed.
Evaluation authorizers compose this policy and retain denied-write ledger events
even when SQLite changes zero rows. File/audit creation, network/subprocess,
new threads/pool submissions and asyncio task/callback scheduling are denied.
Violations unwind as a dedicated control exception, bypass provider fallback,
and produce a safe owner error or evaluation safety exit 2. Conversation updates
and owner reply rendering are outside the synchronous tool scope.

The legacy in-process guard alone does NOT protect arbitrary pre-opened writable
handles. It remains worker defense in depth, not the authority boundary. Earlier
tests using evaluation-owned connections could mask this gap through their
test-only authorizer; external native handles must be tested independently.

Evidence: `tests/test_assistant_read_only_boundary.py` enumerates all bound,
owner-available risk-0 capabilities and supplies real temporary-v15 fixtures.
It checks P1 repeat/reopen/materialization, business/ledger/filesystem equality,
denied SQL/external effects, concurrent readers/start/closing, and guard cleanup.

The extended v2 seed-0 live run at `3f7e6c...` was partial: 117/183 cases,
84 physical attempts, stopped on P1's two unexpected requirement rows; 66 cases
were skipped. These are NOT readiness or full capability-coverage evidence.
Live semantic/authority quality remains pending a separate improvement phase;
no new live evaluation or production action belongs to this fix.
Lifecycle: Development
Last verified: 2026-10-24

`message → ContextSnapshot → Stage 1 route → local validation → optional Stage
2 extraction → server capability → result/preview → atomic context update`.

The provider never owns permissions, stable IDs, risk, revisions,
confirmation, execution, SQL, shell, filesystem, systemd or secrets. A snapshot
contains at most 12 turns/12,000 recent characters, a 2,000-character rolling
summary, five candidates, eight confirmed memories, 500 tool-result characters
and 24,000 serialized bytes. Actor/chat numeric identifiers, raw DB rows, audit
data, full catalog and capabilities outside the runtime surface are excluded.

## Server-owned mutation language (from `96f50da...`)

`ParsedMutation` and `MutationLine` are closed, immutable local models, not
provider schemas. They retain action, entity, Decimal quantity, explicit unit,
destination/comment, imperative evidence, cancellation, a closed error and the
selected correction segment. Disposition is `proceed`, `cancel`, `corrected`,
`ambiguous` or `invalid`. Cancellation is evaluated before action evidence,
provider planning and tool dispatch. An imperative prefix cannot override a
later explicit RU/UA refusal; descriptive negation alone is not cancellation.

Only unambiguous correction grammar replaces a quantity/product/destination.
The old value never becomes an extra intake line. Different unit dimensions,
ranges, alternatives and unclear corrections require clarification without a
new draft. Whole numeric tokens are validated: signed, non-positive, non-finite,
scientific, incomplete and damaged numbers are never reduced to a valid prefix
or absolute value. Warehouse precision/maximum checks remain authoritative.
Leading `.5`/`,5` retain existing quantity UX; conventional three-digit space
grouping is supported. Sentence punctuation after a known unit is distinct from
an unfinished decimal. A spelled singular measure retains the existing explicit
one-measure UX; a base unit is never silently selected.

One missing-field request is persisted as typed `pending_mutation` inside the
existing conversation JSON, isolated by actor/private chat and revision/TTL.
Restart preserves it; invalid quantity answers do not overwrite valid prior
fields. Cancellation clears AI context and uses only the owned source-chat
draft/proposal cancellation service. Reset clears AI pending context, not an
independent warehouse draft or confirmed long-term memory. Active warehouse
FSM has priority and handles natural cancellation through its own service.

AI multiline policy is fail-closed for the whole new batch when any line is
cancelled, ambiguous or numerically invalid. No valid prefix is silently kept.
Duplicate product lines remain separate and correction affects only its line.
Unknown/fuzzy products/destinations retain the ordinary explicit resolution
preview: all lines stay visible and the batch cannot commit until resolved.
Known-product unit incompatibility is rejected read-only before draft creation.
Exact unique grammatical forms may map to existing nomenclature names; exact
names/aliases win, collisions and fuzzy matches still require user selection.

Regressions `ADV_NEGATION_READY_01`, `ADV_NEGATIVE_01`, `ADV_DECIMAL_01` are
covered through real handlers/services and the temporary-v15 observer/ledger.
No schema/catalog/seed change, live quality claim or rollout is implied.

### Single-pass service-word entities (from `d515ed0...`)

`mutation_syntax()` performs one action extraction per original clause and
returns a closed, bounded `MutationSyntax`/`MutationCommand` AST: action/body
spans, entity/quantity/unit/destination/source spans, correction/comment spans,
cancellation clauses, disposition and validation category. Numeric/entity word
spans preserve order and repetitions. The body is never fed back into `_action`.
New explicit clauses retain the old multi-action clarification/fail-closed
policy; a bare noun inside a batch row is not another command header.

Service-like words are valid product data, including full/repeated action names.
Exact normalized full official name wins, then exact alias, then the unchanged
explicit candidate policy. A missing full name cannot silently become a shorter
known product. Stable IDs are bound by the warehouse resolver, not by a planner.
Provider product/target substitutions are rejected before draft creation.

Validated immutable mutation facts are passed directly to registry rendering.
Legacy parser/evidence interfaces reuse facts only for the exact normalized
rendering within an isolated request scope, cleared in `finally` on every exit.
Unicode inspection precedes every scoped lookup, including leading/trailing
controls that ordinary whitespace stripping would otherwise hide.
Changed text is gated normally; different tasks do not share bindings. The
isolated evaluation path uses the same binding. No spans or extra input copies
are persisted in v15 conversation JSON; restart keeps the existing typed facts.

The blocking short/full collision is now an explicit real handler/service/ledger
regression in both foreground modes. Frozen v1.1/v1.2 remain unchanged; separate
v1.3 adds 360 deterministic service-word/entity cases (12 families), plus vertical
exact/alias/missing/compound/restart variants. Its SHA256 is
`99a82aeb28c58a0a23883c5546a9c9a2a5037f912ccbc5c7a6fd546497f84d5a`.
Evidence is offline only. Extended live evaluation remains pending.

### Unicode and positional roles (from `a15759c...`)

`mutation_tokens.py` owns bounded typed Unicode inspection before providers or
tools. NFKC/casefold/ё→е/і→и normalization preserves ordinary/NBSP/thin spaces.
Format/control characters, joiners, bidi/default-ignorable characters, variation
selectors, residual combining marks and service-word homoglyph/boundary attacks
fail closed. Detection-only text is never executable text; suspicious entity or
comment text is not silently repaired. Raw text/token values are excluded from
diagnostic repr/serialization.

Ephemeral tokens retain kind, clause index and bounded spans, including quantities
and units. Grammar assigns entity slots before interpreting service lexemes:
product Стоп before a quantity is an entity, while a separate terminal `стоп`
is cancellation. Exact product names/aliases can resolve a postfix entity;
fuzzy matching cannot suppress a cancel clause. Only typed parsed fields persist,
not a second copy of input/tokens; existing v15 JSON constraints are unchanged.
Inherited typed fields are re-inspected on missing-field resumption: old accepted
control-bearing entity/comment/destination data cannot be revived after restart.

Active warehouse FSM consumes typed cancellation, checks actor/source chat,
optimistic content snapshot and current business-period scope. Role is checked
inside the transaction; duplicate cancellation is idempotent. New incomplete
commands cannot become an older draft's comment: a separate clarification state
persists typed pending fields and preserves the older draft until explicitly
resolved. Restart retains the actor/chat-scoped pending fields.

Frozen `barbot-clause-grammar-v1.1` uses seed 20261109, 1410 cases/1372 unique
strings and SHA256 `61625f501c4f6387bbf0c5ab5b1ec51597ffef53a6f3224526d24488a6572625`.
Expected outcomes are unchanged. Separate v1.2 adds 210 control, 100 entity and
100 FSM/context fixtures; evidence is offline parser plus real temporary-v15
handler/FSM/service and mutation ledger, not live language quality. Extended live
evaluation remains pending; no production permissions or rollout are implied.

### Compound cancellation hardening (from `27f0b9c...`)

`mutation_clauses.py` classifies at most 64 clauses/4,000 characters before any
tail can become a comment. Closed `MutationClause` values carry only kind,
bounded source span and a server-owned superseded flag, never raw text. Kinds:
action, arguments, comment/reason, correction, cancellation, contradiction,
unknown. Decimal punctuation is not a clause delimiter. Existing v15 JSON
without this optional metadata remains readable; an unvalidated legacy comment
cannot be revived by an amount-only answer and requires fresh clear input.
No schema migration is needed.

Final direct cancellation vetoes the whole new batch. Contradictory cancellation,
double negation, cancellation-of-cancellation and unresolved action-like tails
require clarification, with no provider/tool/draft. A later complete explicit
action may supersede cancellation only after full product/quantity/unit/destination
syntax validation; a quantity fragment cannot revive the cancelled action.
Ambiguity clears actionable AI pending state, preserving successful catalog
context. Active warehouse FSM intercepts ambiguous cancellation without changing
an existing draft or consuming the text as its comment; clear cancellation alone
uses the existing owned cancellation service.

Cancellation typos use a closed RU/UA lexicon, whole tokens of length >=5 and
at most one Damerau edit. Only a final isolated clause head/current-action
reference is eligible. Ties clarify. Two-edit damage is not repaired: an unknown
isolated/action-related tail clarifies instead. Short words are never fuzzy
matched. Product names and ordinary reason prose are not scanned for typos;
reason/comment markers cannot hide a direct command to cancel this action.
Supply-cancellation reasons and descriptive negation remain comments. Only the
explicit `не забудь + списать/переместить` template is a positive imperative.
An explicit dialogue correction (`нет, я имел в виду ...`) is not a cancellation;
the existing contextual planner/authority boundary still handles it.

Exact synthetic `CANCEL_COMPOUND` and `CANCEL_TYPO` inputs are reconstructed and
tracked in `tests/test_assistant_compound_cancellation.py`, not copied from
production. Vertical regressions use real handler/FSM/services and the temporary
v15 ledger, including zero transient business inserts for blocked input. Broad
natural-language quality and extended live Gemini evaluation remain pending.

Eviction is deterministic oldest-first. Summary uses only server-confirmed
topic/entity/task/capability state; provider prose never becomes long-term
memory. Summary/session updates share the v13 optimistic revision. Failed
lookup/provider calls do not clear the last successful stable entity. Reset
deletes only the AI conversation session, not warehouse FSM/drafts.

Schema v15 adds `assistant_long_term_memories`. DDL has no transaction control;
the migration coordinator owns atomic v14→v15 creation, validation and
`user_version`. Records are owner-isolated, opaque-tokened, revisioned, scoped,
typed and optionally expiring. Even explicit “запомни …” creates a persistent,
expiring preview; only its owner can confirm it after access recheck. Recall is
read-only. Deletion has a separate preview/confirmation and ambiguous deletion
requires exact selection. Duplicate confirmation is idempotent.

Unreleased v15 binds every memory and deletion preview to both owner and source
private chat. Save/cancel/delete callbacks contain only opaque tokens/revisions;
expiry and ownership are checked transactionally, with a fresh runtime access
check inside confirmation/deletion transactions. Deletion uses its own expiring
preview token. Confirmation replay is idempotent within the original preview
deadline; a replay cannot activate a cancelled record or cancel an active one.

Memory is untrusted user-confirmed data. NFKC/casefold/Unicode-separator and
punctuation normalization precedes multilingual authority/secret/instruction
checks. SQL, shell, policy/role/confirmation changes and provider-supplied tool
instructions are rejected without echoing content in diagnostics. Only an
explicit user remember command starts the runtime preview path. Provider
snapshots carry closed `PlannerMemoryData` records with a fixed untrusted-data
marker; runtime policy/capabilities/targets are separate, server-owned fields.
These input checks are defense in depth: server permissions and confirmation
remain authoritative even when external or catalog data contains instructions.

The v15 validator compares canonical PRAGMA metadata (types, nullability,
defaults, PK/FK columns and actions, index columns/collation/uniqueness) and all
parsed normalized CHECKs. It rejects extra memory tables/triggers and missing
token/content/ownership/time/state invariants. No v1–v14 migration is rewritten.

CHECK extraction uses a deterministic SQLite DDL lexer, not substring matching.
Only unquoted CHECK keywords followed by an opening parenthesis count; literals,
quoted identifiers (including doubled delimiters), line/block comments and nested
parentheses are handled separately. Token-aware normalization preserves literal
values and token boundaries while ignoring ASCII identifier case/quoting, whitespace
and redundant outer expression parentheses. Quoted constraint names cannot supply
missing revision, status/kind, token/content length or timestamp invariants.
Canonical whole-DDL comparison is deliberately not used: harmless formatting and
constraint naming may vary. PRAGMA metadata plus real normalized expressions
remain authoritative. Missing-invariant diagnostics use only a server-owned
table name and invariant ordinal. Validation stays read-only with no row probes.

References resolve only to server-owned stable IDs. Pronouns do not require
grammatical gender; first/second/last use bounded candidates; “туда же” requires
a prior confirmed destination. Ambiguity produces one clarification, never a
fuzzy mutation selection.

At most two runtime-allowlisted tool steps are permitted. Reads may run
immediately. A mutation produces an opaque preview/proposal then stops; existing
confirmation, permission/revision recheck, idempotent execution and audit remain
authoritative.

`ai_first` dispatches the validated capability/domain/locally resolved target
and capability-specific input model directly through the controller, without
reclassifying raw text. The real loop makes at most two planner/tool calls;
read results return as bounded typed untrusted data. A read result cannot
escalate to a mutation, repeat a tool, or alter the runtime allowlist. A proposal
or warehouse intake preview ends the loop immediately. Later provider failure
retains successful local results/context. `local_first` and detached shadow
remain separate. Short process-local update deduplication complements persistent
proposal/intake idempotency; it is not a cross-process delivery guarantee.

Corpus `bar-jarvis-conversation-v1` contains 60 dialogues and 180 user turns,
including every 41 registered capabilities. The frozen runner labels its network-
free deterministic result `offline_scripted`, reports expected/actual contracts
and safety side-effect counts, and writes bounded JSON only to an explicit path.
This validates the runner and application contracts, not provider accuracy.
The frozen scripted contract requires exactly 60 dialogues/180 unique turns,
all 41 capabilities, each of seven semantic/safety metrics at 1.0 and zero false
mutations, unauthorized executions or side effects. Missing/non-finite/out-of-
range metrics fail closed. JSON reports list failed metrics and the CLI exits
with code 1 on contract failure; success exits 0.
Live/shadow route, entity and reference quality remains `pending — requires
controlled shadow evaluation`. Voice, web, images and video also remain pending.

## Isolated live evaluation (development)

Resolved quantity observation is typed end-to-end: temporary intake line scaled
quantity plus product base unit → canonical Decimal text/unit → actual report →
scorer/domain metrics/readiness. It is present in smoke as well as v2 suites;
aliases and scale conversions are dimensional, never inferred from raw prompts.
Terminal refusal/authority errors preserve only earlier successful reads and
cannot start a draft, proposal, tool or dangerous context update.

Current provider policy is documented in [AI assistant](AI_ASSISTANT.md#gemini-first-policy-from-197a765):
Gemini primary, conditional Groq fallback disabled by default, application
admission before network and shared physical pacing. The prior six-case paced
comparison does not establish corpus-wide accuracy. Owner-only shadow is the
next separate live stage; AI-first promotion remains pending. Evaluation treats
gram aliases as equivalent only within the mass dimension; informational
writeoff explanations may use general/warehouse domains, but never a mutation
capability, preview or business side effect. Expectations remain scorer-only.

`offline_scripted` remains runner-contract evidence, not provider accuracy.
`live_isolated` has a separate real-service composition and versioned synthetic
`bar-jarvis-live-conversation-v1` corpus: four dialogues, two safety cases, 28
steps (20 texts plus eight explicit lifecycle actions). Expected fields reach
only post-actual scoring. No Telegram Update/transport or production container
is used. Shadow is a different, executor-free comparison path.

Each run owns a fresh temporary v15 DB, catalog and audit paths, validates
containment/no-symlink before use and cleans up on success/error/cancellation.
Read tools run normally; mutations stop at draft/proposal/UI preview. Synthetic
confirm/cancel uses actual registered service transitions. One shared atomic
HTTP-attempt budget covers Stage 1/2, fallback and second tool-loop steps;
retry is zero. Budget exhaustion skips further network work without counting
skipped turns as semantic failures. Output is opt-in, bounded and privacy-safe.
Live provider health/quality remains pending: fake HTTP tests do not establish
live accuracy. See [evaluation run contract](../../docs/assistant_live_evaluation.md).

### Live harness validity hardening

From base `b0c2780...`, every reached provider step contributes independent typed
validation/transport/contract counters. Preserving a successful read never
erases a later wire/canonical/authority violation: readiness is false, exit 2.
Transport failures require infrastructure review; semantic mismatch stays exit 1.
Authorization observations separate blocked attempts from executor invocations
without server-side authorization; unsafe invocation is a fatal safety event.

The harness exclusively creates its own fixed DB in an unpredictable 0700 root,
pins inode/device and checks regular/0600/nlink=1 before and after SQLite open
and before restart. No caller path, hardlink or URI is accepted. All connections
are scoped and closed; cleanup refuses a replaced root. Same-UID hostile races
are outside the remote-provider model, not claimed impossible.

Corpus estimates remain static: 17 minimum, about 25 normal, 34 conservative
without fallback/68 with it. CLI rejects below-minimum budgets before keys;
17–24 marks partial coverage risk. Budget is never raised automatically and
skipped mandatory cases cannot pass. Live provider quality remains pending.

### Versioned live corpus v2

Development from `bd73be96...` adds `--suite smoke|core|extended` and seeded
paraphrase rotation. Smoke retains the original v1 texts/expectations exactly;
core has 9 dialogues/51 messages/18 lifecycle actions; extended has 19/146/37.
E–L measures lookup/reference/typo/topic/correction/intake/attendance/memory;
M–V adds long context, negation, reports, required flow, roles, administration,
general chat, controlled fake failures, isolation and injection probes.

Seed chooses one alternate semantic case, never extra calls for every paraphrase.
Expected reaches only post-actual scorer. Core requires assessed route/reference/
entity/arguments ≥0.90, zero unsafe mutations/executions; extended cannot average
away a failing critical domain. Smoke is infrastructure/contract evidence only.
Static budget estimates at seed 0: smoke 17/25/34/68, core 48/55/96/192,
extended 135/151/263/519 (minimum/normal/no-fallback/fallback). No automatic
budget increase. Synthetic T faults are marked separately from provider quality;
V is capped at one physical call, fallback off. Fixtures and reports exist only
inside the owned temporary root; no production paths or Telegram transport.

Live results and unimplemented conversational behavior remain **pending**.
The harness measures real outcomes and does not improve production NLU to pass.
See [coverage/readiness/budgets](../../docs/assistant_live_evaluation.md).

### Live scoring side-effect/domain correction

From `a1900e19...`, readiness observes actual safe structural snapshots of the
owned temporary DB plus SQLite statement mutation metadata. A create/delete or
write/revert cannot disappear behind a successful response or unchanged final
snapshot. Scorer-owned typed policies permit only the declared read/context,
proposal, confirm/cancel or memory transition; unexpected writes are fatal
exit 2. Raw rows, statements, comments, memory content and actor IDs are not
exported. The ledger conservatively retains writes even if rolled back.

Accuracy denominators use expected domain/capability, never actual routing.
Confusion expected→actual is separate. Required domain metrics come from actual
suite expectations; missing/NaN route cannot be hidden by proposal=1.0. Safety
counters are mandatory zero. Live quality remains pending; this is offline
measurement-integrity evidence only.
