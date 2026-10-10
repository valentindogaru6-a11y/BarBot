# AI Assistant Pilot

Scope: bounded owner-only intent, retrieval, and draft preparation
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
- `AssistantConversationMemory` keeps only structured intent/entity/query,
  local candidate keys, pending slots, and timestamps. It is per-user,
  process-local, bounded to 100 entries, expires after 15 minutes, and clears
  after completion, cancellation, timeout, or provider failure.
- Candidate callbacks contain stable local catalog IDs and are revalidated
  against both current context and the current catalog. Active FSM, commands,
  exact warehouse triggers, permissions, single-flight, persistent drafts, and
  explicit warehouse confirmation remain authoritative.
- KOS is developer/Codex memory; it is never copied into the runtime prompt or
  dialog context. Runtime context is not persisted to SQLite.
- The committed catalog has no searchable `мохито` card. The safe response is
  an honest absence message; no recipe is synthesized.
