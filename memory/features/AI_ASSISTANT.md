# AI Assistant Pilot

Scope: bounded owner-only intent, conversation, retrieval, search, and drafts
Trust: Confirmed from code and tests
Lifecycle: Fresh
Last verified: 2026-10-10
Evidence: `app/services/assistant/`, `app/handlers/assistant.py`, assistant tests
Depends on: catalog services, centralized access, warehouse draft confirmation
Importance: High

- The runtime system role is the short versioned builder in
  `app/services/assistant/system_prompt.py`. It is shared by text and voice and
  contains no catalog dump, KOS content, IDs, or secrets.
- `AssistantCatalogRetriever` is the factual boundary. It normalizes commands,
  Unicode, `ё/е`, punctuation, Russian endings and token order; exact/token
  matches precede aliases/searchable fields and bounded fuzzy candidates.
- Gemini receives the request, normalized entity and at most five local
  candidate labels. Its output remains strict schema/Pydantic data and cannot
  supply database IDs, callbacks, SQL, or service methods.
- `AssistantConversationMemory` combines structured clarification fields with
  at most six normalized user/assistant turns, 6000 characters and a 30-minute
  TTL. It is process-local, isolated for at most 100 users, never logged or
  persisted, and its reset phrases do not clear FSM or warehouse drafts.
- Candidate callbacks contain stable local catalog IDs and are revalidated
  against both current context and the current catalog. Active FSM, commands,
  exact warehouse triggers, permissions, single-flight, persistent drafts, and
  explicit warehouse confirmation remain authoritative.
- Four routing modes are represented by strict intents: local `bar_knowledge`,
  allowlisted `bar_action`, text-only `general_chat`, and optional
  `web_search`. The first two retain existing local retrieval/draft boundaries.
- `GeminiGroundedSearchClient` uses only the official `google_search` tool,
  accepts at most two reported queries and builds source links solely from
  `groundingMetadata`. Search text is untrusted and cannot call the registry.
- KOS is developer/Codex memory; it is never copied into the runtime prompt or
  dialog context. The system context is the runtime role, conversation memory
  is ephemeral, local retrieval owns BarBot facts, and grounding is external.
- General chat and web search default off. The production pilot access remains
  private-chat `super_admin`; no broader role is enabled by this phase.
- The committed catalog has no searchable `мохито` card. The safe response is
  an honest absence message; no recipe is synthesized.
