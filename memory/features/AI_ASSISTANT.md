# AI Assistant Pilot

Scope: bounded role-aware intent, conversation, retrieval, capabilities and proposals
Trust: Confirmed from code and tests
Lifecycle: Fresh
Last verified: 2026-10-14
Evidence: `app/services/assistant/`, `app/handlers/assistant.py`, assistant tests, follow-up/local-action regressions
Depends on: catalog services, centralized access, warehouse draft confirmation
Importance: High

## AI-first planner foundation (development)

The development planner boundary is provider-neutral. `GroqPlannerProvider`
(`openai/gpt-oss-20b`) is the configured fast primary and
`GeminiPlannerProvider` the sequential fallback, while `PlannerDecision`
remains the only internal plan contract. Each adapter validates its own closed,
flat wire envelope, converts allowlisted string arguments, validates stable IDs
and then derives read/mutation risk from the runtime `PlannerCapability`.
Provider data cannot assert confirmation, actor, role, permission, revision or
context updates. Provider-generated payloads never reach an executor.

The Groq strict schema is versioned, has no `$defs`, `$ref`, defaults or open
objects, and makes nullable fields explicitly required. It receives no tools.
Fallback is sequential under one bounded deadline: shadow does not double-call
by default; AI-first may fall back only for normalized transient/schema errors;
auth/config failures do not retry. A process-local closed/open/half-open circuit
breaker suppresses repeatedly unhealthy providers without touching business
data. This composition is development evidence only: production promotion,
provider quality and live accuracy remain `pending`.

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
