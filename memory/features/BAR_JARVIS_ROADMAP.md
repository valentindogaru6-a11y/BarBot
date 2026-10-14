# Bar Jarvis Product Goal And Roadmap

Scope: owner-approved end state, measurable acceptance and staged delivery plan
Trust: Owner-approved direction; implementation and live coverage are mixed/pending
Lifecycle: Planned
Last verified: 2026-10-14
Evidence: owner direction; current implementation evidence remains in `AI_ASSISTANT.md`
Depends on: `AI_ASSISTANT.md`, `AI_CAPABILITY_LANGUAGE.md`, production safety policy
Importance: Critical

## End state

Bar Jarvis is a text and voice AI pilot that uses natural language to operate
every capability the current actor is allowed to use. It maintains bounded,
restart-safe context, can execute multi-step work, and combines local BarBot
data with Gemini, grounded web search, images and video when those sources are
appropriate and enabled.

This is a target, not a statement of current capability. Any surface without a
measured evaluation corpus or controlled live evidence is `pending`. Runtime
AI access, roles, pilot allowlists and feature flags remain independent rollout
decisions; this roadmap grants no production authority.

## Definition of Done

1. **Technical foundation:** zero open critical/high security or reliability
   findings; permission, confirmation, idempotency and rollback safety pass
   every applicable automated and controlled-live case.
2. **Catalog and context:** at least 90% correct results on an owner-approved,
   versioned conversational corpus; follow-ups use stable IDs; context survives
   restart; exact, fuzzy, relation, ingredient and category retrieval are
   measured separately. Current aggregate score: `pending corpus`.
3. **Warehouse natural language:** at least 90% correct draft preparation on an
   owner-approved corpus, zero operations before confirmation, and zero unsafe
   false positives in the negative/security corpus. Current aggregate score:
   `pending corpus`.
4. **All 41 capabilities:** each capability has at least 10–15 approved natural
   formulations covering required arguments and clarifications; aggregate
   routing/argument extraction is at least 90%; security-boundary cases pass
   100%. Capability-by-capability coverage remains `pending` unless explicitly
   marked tested in `AI_CAPABILITY_LANGUAGE.md`.
5. **Voice:** at least 90% end-to-end success on an owner-approved real voice
   corpus; transcription enters the same typed pipeline as text; no silent
   mutation is possible. Current live corpus result: `pending`.
6. **Web, images and video:** external answers are grounded in source metadata,
   links are safe, local catalog data has priority, external material never
   mutates the catalog automatically, and image/video results include source
   metadata. Image/video implementation and evaluation are `pending`.
7. **Jarvis experience:** multi-step plans preserve typed context; clarification
   occurs only when materially necessary; errors do not destroy recoverable
   context; every mutation has preview and confirmation; completed actions are
   explained concisely.

Percentages may be reported only with the corpus version, sample size, scoring
rules, failure breakdown and reproducible result. Ad-hoc examples are evidence
for individual cases, never an aggregate percentage.

## Phase A — Natural Language Coverage

- **Scope:** versioned text corpora for catalog/context, warehouse commands and
  all 41 capabilities; typed intent/argument extraction; synonyms, colloquial
  forms, negative cases, ambiguity and recovery; update the language index from
  measured evidence only.
- **Dependencies:** stable capability inventory, typed local parser/intent
  contract, deterministic retrieval, persistent context, fake transports and
  owner-approved corpus governance.
- **Measurable acceptance:** 10–15 approved formulations per capability;
  catalog/context and warehouse draft suites each reach at least 90%; aggregate
  41-capability routing/argument extraction reaches at least 90%; all security
  and unsafe-false-positive cases pass. Status: `pending corpus`.
- **Security boundaries:** permissions, pilot/private-chat gates, local action
  evidence, no automatic fuzzy selection, no operation before confirmation,
  opaque bounded callbacks and server-side rechecks.
- **Live pilot:** owner-only private-chat batches, one command at a time, with
  expected route/arguments recorded before execution; mutations end at preview
  and are cancelled unless separately approved.
- **Rollback criteria:** any unauthorized surface, unsafe false positive,
  operation before confirmation, repeated mutation, context cross-talk or
  critical/high finding disables the affected AI route by existing flags and
  returns users to deterministic BarBot flows.

## Phase B — Multi-step Planner

- **Scope:** bounded plans composed only from runtime-authorized capabilities;
  typed intermediate outputs, stable entity references, explicit dependencies,
  resumable clarification and concise completion summaries.
- **Dependencies:** Phase A corpus baseline, persistent context/revisions,
  capability registry bindings, proposal lifecycle and idempotent services.
- **Measurable acceptance:** an owner-approved multi-step corpus with declared
  expected plans, tool arguments and stop points; at least 90% correct complete
  plans and 100% security/confirmation/idempotency cases. Status: `pending`.
- **Security boundaries:** no invented capability or ID, no provider-selected
  service method, per-step access recheck, transactional services remain the
  mutation authority, and later steps cannot run after a failed prerequisite.
- **Live pilot:** owner-only read-only plans first, then preview-and-cancel
  mutation plans; expand one domain at a time after logged acceptance.
- **Rollback criteria:** plan escapes its allowlisted domain, skips a required
  confirmation, continues after failure, duplicates a step, or loses stable
  context; disable planner routing while keeping single-step local flows.

## Phase C — Voice

- **Scope:** real owner-approved audio corpus, bounded transcription, the same
  parser/orchestrator/tool pipeline as text, spoken clarification and safe
  rendering of previews/results.
- **Dependencies:** Phase A, stable Groq adapter/runtime health, audio limits,
  cleanup/cancellation, single-flight and privacy-safe diagnostics.
- **Measurable acceptance:** at least 90% end-to-end task success on a versioned
  corpus with noise/accent/quantity/unit cases; 100% mutation confirmation and
  cleanup/security cases. Status: `pending owner-approved voice corpus`.
- **Security boundaries:** transcript is untrusted input, no silent mutation,
  no retained audio beyond bounded processing, no secret/transcript logging,
  and text/voice access policy parity.
- **Live pilot:** owner only, read-only phrases first, then mutation previews
  that are cancelled; enable voice flag only for the controlled window.
- **Rollback criteria:** material transcription ambiguity without clarification,
  leaked/retained audio, access divergence, unexpected provider cost/failure or
  any mutation without explicit confirmation; disable voice flag only.

## Phase D — Grounded Web

- **Scope:** grounded web answers for current external information, with local
  catalog priority, source metadata, bounded queries and clear separation from
  local tools.
- **Dependencies:** stable grounded-search adapter, citation validator, safe-link
  renderer, budgets/timeouts and Phase A routing that distinguishes local facts.
- **Measurable acceptance:** versioned factual/query corpus with source-quality,
  citation-support and local-priority scoring; acceptance threshold defined
  before the pilot and then measured. Current score: `pending corpus`.
- **Security boundaries:** web content cannot issue tool instructions, select
  database IDs or mutate catalog/warehouse; only credential-free HTTPS links
  backed by provider metadata; no blind retry of ambiguous billable calls.
- **Live pilot:** owner-only, read-only questions with manual source review;
  start with web disabled outside a short approved window.
- **Rollback criteria:** ungrounded claim, unsafe link, prompt injection crossing
  into a tool, local-data displacement or uncontrolled cost/latency; disable web
  search flag while local assistant remains available.

## Phase E — Images And Video

- **Scope:** search/presentation of relevant images and videos with source,
  title, media type and safe link metadata; optional understanding/generation
  requires a separate approved design.
- **Dependencies:** Phase D source policy, media provider/adapters, Telegram-safe
  rendering, licensing/retention rules and explicit feature flags.
- **Measurable acceptance:** owner-approved media corpus scored for relevance,
  source metadata, safe URL handling and local-context continuity; thresholds
  fixed before measurement. Status: `pending design and corpus`.
- **Security boundaries:** no automatic catalog mutation, no untrusted file
  execution, bounded downloads/types/sizes, no credential-bearing URLs, and no
  media sent to users or chats outside the active access policy.
- **Live pilot:** metadata/links to owner private chat first; no group delivery
  until source, licensing and recipient controls pass controlled acceptance.
- **Rollback criteria:** missing/unsafe source metadata, unexpected recipient,
  unbounded download, unsupported content type, licensing concern or context
  leakage; disable the media surface independently.

## Phase F — Evaluation Harness And Production Observability

- **Scope:** versioned redacted corpora, deterministic/fake-provider runs,
  scoring by domain/capability, safety suites, latency/error categories and
  privacy-safe production counters without prompts, transcripts or IDs.
- **Dependencies:** phases A–E contracts, stable test fixtures, release runbooks,
  redaction policy and owner-approved acceptance datasets.
- **Measurable acceptance:** reproducible reports include corpus/version/sample,
  route and argument accuracy, unsafe false positives, confirmation/idempotency,
  provider/local split, latency buckets and failure classes; DoD thresholds are
  enforced as release gates. Status: `pending harness`.
- **Security boundaries:** no secrets, Telegram IDs, raw prompts, transcripts or
  provider bodies in metrics; no production mutation probes; least-retention
  diagnostics and explicit operator access.
- **Live pilot:** shadow measurement of already authorized owner interactions,
  then staged release dashboards and alerts; observability never sends Telegram
  updates or expands access.
- **Rollback criteria:** telemetry leaks sensitive content, changes application
  behavior, miscounts safety outcomes or creates unacceptable overhead; disable
  the new instrumentation and retain deterministic offline evaluation.

## Sequencing and evidence rule

Recommended order is A → B → C → D → E, with F started alongside A and made a
release gate before broader rollout. A later phase does not weaken earlier
permission, confirmation, idempotency, rollback or local-first guarantees.
Every phase needs its own development commit, final review, controlled pilot
authorization and rollback decision under `AGENTS.md`.
