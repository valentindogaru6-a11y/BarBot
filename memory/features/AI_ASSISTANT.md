# AI Assistant Pilot

Scope: bounded role-aware intent, conversation, retrieval, capabilities and proposals
Trust: Confirmed from code and tests
Lifecycle: Fresh
Last verified: 2026-10-14
Evidence: `app/services/assistant/`, `app/handlers/assistant.py`, assistant tests, follow-up/local-action regressions
Depends on: catalog services, centralized access, warehouse draft confirmation
Importance: High

The multi-turn context, confirmed-memory and guarded tool-loop contract is in
[BAR_JARVIS_CONVERSATION.md](BAR_JARVIS_CONVERSATION.md).

## AI-first planner foundation (development)

Mutation-language hardening from `96f50da...` uses the server-owned typed
disposition described in [conversation contract](BAR_JARVIS_CONVERSATION.md#server-owned-mutation-language-from-96f50da).
Cancel/ambiguous/invalid input cannot reach a provider or prepare a new draft;
the same analysis gates local-first, planned dispatch and the isolated harness.
Full numeric tokens, exact correction segments and a typed persistent missing-
field context replace substring quantity extraction. This is offline evidence;
natural-language breadth and provider quality remain pending live coverage.

The provider boundary is now explicitly two-stage. Stage 1 has seven required
scalar fields and contains no nullable value, array, free object, stable ID,
argument, risk, permission, confirmation or revision. Providers see bounded
display labels and indices; the server resolves the index and locally builds
the canonical `PlannerDecision`. Stage 2 prefers deterministic BarLexicon and
warehouse Decimal parsing; remaining catalog ingredient, writeoff, transfer
and attendance force-close schemas are closed and capability-specific. No
extractor can execute a tool or mutation.

Evaluation separates Stage 1 dialogue/domain/capability/target/action/
clarification accuracy from Stage 2 argument, missing-field and unit/quantity
validation. Corpus `bar-jarvis-two-stage-v2` contains nine route and four
argument cases and makes no live accuracy claim. The compact route remains
pending a separately authorized live smoke.

The development planner boundary is provider-neutral. `GeminiPlannerProvider`
is the default primary; `GroqPlannerProvider` is a conditional availability
fallback candidate, disabled by default, while `PlannerDecision`
remains the only internal plan contract. Each adapter validates its own compact
route envelope; local assembly resolves target indices and derives
read/mutation risk from the runtime `PlannerCapability`.
Provider data cannot assert confirmation, actor, role, permission, revision or
context updates. Provider-generated payloads never reach an executor.

The Groq strict route schema is versioned and has no nullable fields, arrays,
`$defs`, `$ref`, defaults or open objects. It receives no tools.
Fallback is sequential under one bounded deadline: shadow does not double-call
by default; AI-first may fall back only for normalized availability errors;
auth/config failures do not retry. A process-local closed/open/half-open circuit
breaker suppresses repeatedly unhealthy providers without touching business
data. This composition is development evidence only: production promotion,
broader provider quality and production promotion remain `pending`.

### Gemini-first policy (from `197a765...`)

Owner-approved paced six-case bake-off at that HEAD selected Gemini fast:
canonical/semantic 6/6, target/reference 3/3, no 429/timeouts, median 904 ms,
p95 1703 ms. This is six diagnostic cases, not a >=90% production-corpus claim.
Groq 20B stopped after two contract failures (1/3 canonical); 120B had 5/6
canonical (83.3%, below the 90% contract threshold). Pacing removed observed
429s in this sample but did not remove `json_validate_failed`; exact historical
RPM/TPM cause is not established. These results are prior live evidence, not a
new live run in the policy implementation task.

`AI_PLANNER_FALLBACK_ENABLED=false` is the safe default. Missing Groq key/model
does not block a disabled planner fallback; voice/STT has independent Groq
requirements. Fallback is at most one availability attempt, never auth/config,
schema/semantic/authority errors, overload, or after a tool/proposal boundary.
Both converters validate before execution; local imperative evidence remains
necessary for mutations. Failure can clarify or return a deterministic local
result, never re-enter an unbounded legacy provider chain.

Application admission, not provider fan-out, handles spam: common text/voice
claim, bounded logical concurrency, per-user cooldown, accepted-update TTL
dedupe and immediate busy (no queue). Shared physical concurrency is one;
Gemini starts are >=10 seconds apart, Groq >=15; 429 cooldown >=90 seconds,
bounded Retry-After <=900. A logical scope has <=2 physical attempts, <=1 per
provider across the two-step loop, with one deadline. Attempt telemetry has no
actor identifiers or request content. Completed local reads survive exhaustion.

Production remains `local_first`; next phase is separately authorized owner-only
Gemini shadow. `ai_first`, broad staff access, Groq fallback enablement and larger
live evaluation remain pending. No config or runtime change is authorized by memory.

### Four contract corrections (from `c298961...`)

Terminal safety/model refusals and authority/capability/target/domain violations
remain typed through adapters, chain and handler. They cannot become availability
or trigger local mutation preparation; explicit imperative evidence does not
override refusal. Only an already completed read may survive. Unknown internal
errors likewise do not authorize local mutation. HTTP status is authoritative:
400 metadata may only refine a terminal category, never grant retry/fallback or
availability-circuit authority. 401/403, 429 and 5xx stay in their status classes.

Disabled fallback is absent from dependency validation and composition, including
legacy same-provider configuration. Active primary/fallback consumers validate
their own key/model. Legacy Gemini intent/general, Gemini web and Groq Whisper
are explicit separate consumers, never hidden planner fallback dependencies.

All evaluation suites project canonical positive quantity/base-unit pairs from
resolved temporary intake lines through a typed contract. Decimal scale/unit
equivalence reaches the real runner, metrics and readiness, without raw-text
re-parsing. A correct 200 g line is canonical 0.2 kg; missing/invalid data and
cross-dimensional comparisons fail. These are offline regression claims only.

Owner-confirmed dashboard limits for documentation, 2026-11-09: Gemini 15 RPM,
250K TPM, 500 RPD; six bake-off requests, peaks 4 RPM / 8.39K TPM. Keep pilot
pacing at ten seconds. Limits are not newly queried or a claim of daily-quota
enforcement. Owner-only shadow/live validation and production changes remain pending.

The first controlled Groq planner smoke established a strict-schema PASS for
the initial catalog lookup. Its second correction call returned HTTP 400, but
the old adapter discarded provider `type`/`code`/safe `param`, so the precise
cause is not recoverable and correction quality remains `pending`. Offline
reconstruction proves the two turns use the same strict schema hash; stable
entity IDs stay in bounded context and local conversion, never a schema enum.
The adapter now retains only allowlisted error metadata, response size and
one-way request/schema hashes. It separates invalid schema, generated-output
mismatch, unsupported parameters, refusals, auth, quota, rate limit, server,
timeout, network and unknown bad requests without retaining provider prose.
Deterministic request/schema failures neither retry nor count as transport
outages for circuit health; shadow still has no fallback, while AI-first uses
only its explicit fallback categories.

`AI_PLANNER_MODE` is `local_first` (safe default), `shadow` (comparison only,
never execution), or `ai_first` (free text planned first). Commands, callbacks,
active FSM and exact warehouse triggers retain priority. The strict planner
sees only role, runtime-filtered capability contracts, domain, supplied stable
entity/candidate IDs, pending state, a safe tool summary, six bounded turns and
revision/expiry. It receives no actor/chat ID, secret, database dump or
authority field. BarBot rejects unavailable capabilities, invented IDs and
risk mismatches, then rechecks all normal access/proposal boundaries.

Warehouse plans still need deterministic imperative evidence and use the
existing draft/preview/confirmation path. Catalog management remains an admin
FSM, never model access to JSON. Provider failure preserves the last successful
entity and falls back to deterministic read-only behavior or a safe
clarification. Shadow and AI-first live accuracy is `pending`; the incomplete
live smoke established no score and Cases 2–4 remain unknown.

Shadow evaluation is detached from the primary request lifetime.
`ShadowPlannerSupervisor` keeps strong task references, allows at most one
active attempt per actor key, bounds global concurrency and provider time,
drops duplicate/busy work, consumes terminal exceptions, and cancels/drains
tasks on shutdown. The primary response and per-user coordinator claim never
await shadow. Shadow receives only the redacted snapshot and runtime-filtered
capability contracts; it has no executor and cannot update context, FSM,
drafts, proposals, operations or storage.

Each accepted, dropped, failed or cancelled attempt emits one bounded,
versioned comparison event. It records local/planner dialogue act, domain,
capability and allowed stable target ID, risk, clarification, validation,
latency/payload buckets and a closed error category. It never records raw text,
transcripts, Telegram identifiers, usernames, secrets, headers, provider
payloads, system prompts or warehouse comments. Telemetry failure is isolated.

The diagnostic smoke harness uses the application adapter and strict Pydantic
decision, counts attempted/completed calls, retains each bounded result before
the next case, uses `model_copy(update=...)`, executes no tools, and refuses
live calls without an explicit one-time flag. It is not a production path.

The owner-approved end state and phased measurable plan are maintained in
`BAR_JARVIS_ROADMAP.md`. It is a target contract, not evidence that voice,
multi-step planning, full 41-capability language coverage, web, image or video
acceptance thresholds have already been met. Unmeasured surfaces remain
`pending`.

- The runtime system role is the short versioned builder in
  `app/services/assistant/system_prompt.py`. It is shared by text and voice and
  contains no catalog dump, KOS content, IDs, or secrets.
- `AssistantCatalogRetriever` is the factual boundary. It normalizes commands,
  Unicode, `ё/е`, punctuation, Russian endings and token order; exact/token
  matches precede aliases/searchable fields and bounded fuzzy candidates.
- `BarLexicon` and its structured local parser normalize everyday bar terms
  (`пф`, `п/ф`, `пфка`, `полуфабрикат`, `заготовка`) into an action, domain,
  entity and relation before retrieval. Confident read-only catalog routes keep
  local results without provider refinement; stable candidate/entity IDs back
  ordinal and card-content follow-ups. Known-domain provider planning receives
  only the actor-authorized capabilities for that domain.
- Catalog relation semantics (`requested_kind`, relation, target, parent,
  related and selected stable IDs) use strict enums from parsing through local
  retrieval and are retained across process restart in the existing bounded
  conversation JSON field. Old list-only context rows remain readable; no
  schema migration or catalog-specific recipe alias is required.
- Supported relations are `preparation_for`, `ingredients_of`, `similar_to`,
  `direct_lookup` and `unspecified`. Supported requested kinds are
  `preparation`, `tech_card`, `ingredient`, `category` and `unspecified`.
  The end-to-end fields are `domain`, action, entity/query, requested kind,
  relation, relation target, stable candidate IDs, stable selected ID and,
  where applicable, confidence/evidence. No intermediate layer may rebuild
  them only from synthesized text or silently discard fields it does not
  understand.
- Confirmed catalog examples:
  - `найди пф для Berry radiance` resolves parent `Berry radiance
    (ягідний)`, relation `preparation_for`, child `Ягодное пюре`;
  - `найди мне пфку для ягодного чая` resolves `Ягодная основа для чая`;
  - `покажи что входит в ягодный чай` uses `ingredients_of` and
    resolves `Ягодная основа для чая`.
- Gemini receives the request, normalized entity and only bounded local
  candidate labels. Its output remains strict schema/Pydantic data and cannot
  supply database IDs, callbacks, SQL, or service methods.
- Warehouse intent classification is not authority. A normalized local action
  gate must find an explicit writeoff/transfer command, or a previously gated
  pending action must still be live. The provider enum is narrowed per request,
  and local post-validation blocks escalation before persistent-draft creation.
- `PersistentAssistantConversationMemory` stores schema-v13 sessions and turns
  by user/private-chat. Retention is 24 hours, 12 turns and 12000 characters,
  with oldest-first eviction, bounded summaries and opportunistic expiry.
  Reset deletes only AI state; FSM and warehouse drafts remain authoritative.
- Schema-v13 conversation DDL is a sequence of individual statements with no
  transaction control of its own. The migration coordinator owns the enclosing
  transaction, so DDL, `user_version=13` and validation roll back together.
- Phase 3 adds an immutable `CapabilityRegistry` and schema-v14 persistent
  action proposals. Capability discovery is filtered by the current role,
  private-chat policy and feature flags; prompt text is never authority.
- `AssistantAccessPolicy` is the single AI entry gate, separate from capability
  authorization. Its safe default is `pilot_allowlist`: AI must be enabled and
  the actor must be an active, non-blocked, explicitly allowlisted
  `super_admin` in a private chat. Enabled pilot mode fails configuration when
  the list is empty or invalid. The implemented future `role_based` mode still
  delegates the concrete surface to `CapabilityRegistry` and `AccessService`.
  Handlers, callbacks, controller dispatch and persistent proposal lifecycle
  all recheck the policy; proposal execution rechecks again inside its atomic
  transaction. Regular non-AI commands, FSM and warehouse routes remain
  independent of this gate.
  Mutations use opaque ten-minute tokens, one active proposal per actor,
  explicit confirmation and service-owned revision/authorization checks.
- Runtime discovery and the planner schema require a concrete binding from
  capability to adapter, application service or existing Telegram FSM route,
  renderer and (for mutations) callable executor. `AccessService` context
  properties are the permission source; `user` and `blocked` receive no AI
  capability surface. Existing typed callback/FSM flows remain authoritative
  for reports, required writeoffs, corrections, catalog, users, schedules,
  recipients and attendance XLSX.
- Proposal-owned SQLite mutations use one `BEGIN IMMEDIATE` unit of work for
  ready→confirmed CAS, permission/revision recheck, business write,
  transactional audit and executed result. Any exception rolls all of these
  back before the proposal is separately marked failed/conflicted. A new
  mutation cannot silently cancel an unfinished proposal. External Telegram
  delivery continues through its durable claim/send/sent-or-failed lifecycle.
- Schema-v14 proposal DDL also contains no transaction control. Migration owns
  creation, `user_version=14`, validation and rollback as one atomic step.
- Structured state separates `last_successful_entity`, `current_candidates`,
  `pending_clarification` and `last_attempt`. A failed lookup, provider error or
  malformed response may update the attempt but must not erase the last
  successfully selected stable entity. A successful selection replaces that
  entity; reset clears all AI context. State remains isolated by user and
  private chat and survives process restart through persistent memory.
- The local reference resolver recognizes pronouns and descriptive references
  without requiring grammatical-gender agreement. With one selected entity it
  resolves `ingredients_of` and `similar_to` by stable ID; with multiple viable
  candidates it asks for clarification. A pronoun is never sent as a literal
  catalog query. Similarity is deterministic, excludes the source card and
  never auto-selects a fuzzy candidate.
- Explicit natural-language writeoff and transfer commands are parsed locally
  into action, product, decimal quantity, unit, destination and optional
  comment before provider fallback. Complete commands enter the existing
  warehouse draft/preview/confirmation flow with zero immediate operations;
  incomplete commands request only missing fields. Informational questions are
  not mutation evidence, and catalog context cannot replace an explicitly
  named warehouse product.
- `AssistantReadOnlyTools` exposes only typed, bounded catalog, employee,
  attendance, shift, report, recipient, reminder, schedule and capability
  views. Every call rechecks centralized authority server-side and delegates
  to existing services; there is no arbitrary SQL or mutation surface.
- Candidate callbacks contain stable local catalog IDs and are revalidated
  against both current context and the current catalog. Active FSM, commands,
  exact warehouse triggers, permissions, single-flight, persistent drafts, and
  explicit warehouse confirmation remain authoritative.
- Four routing modes are represented by strict intents: local `bar_knowledge`,
  allowlisted `bar_action`, text-only `general_chat`, and optional
  `web_search`. The first two retain existing local retrieval/draft boundaries.
- `GeminiGroundedSearchClient` uses only the official `google_search` tool and
  builds links solely from metadata-backed, credential-free HTTP(S) sources.
  It requires grounding metadata, a valid source and support linkage. One user
  update makes one provider call with no automatic retry; the request suggests
  at most two internal queries, but this is not a technical Google-query cap.
  Only the numeric reported query count is logged.
- General chat or web search requires the master flag, at least one enabled
  input channel, and Gemini. Web composition remains separate and creates no
  search client while its flag is disabled.
- KOS is developer/Codex memory; it is never copied into the runtime prompt or
  dialog context. The system context is the runtime role, conversation memory
  is ephemeral, local retrieval owns BarBot facts, and grounding is external.
- General chat and web search default off. Phase 3 may route private-chat
  requests for active roles, but each capability has an explicit role set and
  every underlying service remains authoritative.
- The committed catalog has no searchable `мохито` card. The safe response is
  an honest absence message; no recipe is synthesized.
