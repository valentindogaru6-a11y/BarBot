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
