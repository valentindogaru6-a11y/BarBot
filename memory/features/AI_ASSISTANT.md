# AI Assistant Pilot

Scope: bounded role-aware intent, conversation, retrieval, capabilities and proposals
Trust: Confirmed from code and tests
Lifecycle: Fresh
Last verified: 2026-10-12
Evidence: `app/services/assistant/`, `app/handlers/assistant.py`, assistant tests
Depends on: catalog services, centralized access, warehouse draft confirmation
Importance: High

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
- Structured state keeps the current topic, last catalog query/entity,
  candidates, requested action, tool and one pending clarification. High-
  confidence follow-ups are resolved locally; ambiguous references ask one
  question rather than guessing.
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
