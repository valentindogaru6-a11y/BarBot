# Bar Jarvis Conversation Brain

Scope: bounded multi-turn context, confirmed memory and guarded tool loop
Trust: implemented and tested offline; live provider/shadow evidence pending
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
