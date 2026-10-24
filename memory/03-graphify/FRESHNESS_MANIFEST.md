# Freshness Manifest

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
