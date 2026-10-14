# Freshness Manifest

Metadata: scope `BarBot`; trust `mixed`; lifecycle `active`; last reviewed
`2026-10-14` during the follow-up/local-action development phase based on
HEAD `f66e402976e6e037ab483744536a52cadb21ded8`.

| Note | Scope | Importance | Trust | Lifecycle | Last verified | Evidence / dependencies | Revalidation trigger |
|---|---|---:|---|---|---|---|---|
| `01-canonical/summary/PROJECT_SUMMARY.md` | whole project | high | mixed | active | 2026-09-29 | repository source; owner runtime statement last verified 2026-09-24 | architecture, release, or live-runtime task |
| `01-canonical/summary/ARCHITECTURE.md` | application | high | verified | active | 2026-10-14 | handlers/services/storage; relation contract at `c2ce9fd2...` | composition or package-boundary change |
| `01-canonical/summary/WAREHOUSE_DOMAIN.md` | warehouse | high | mixed | active | 2026-09-29 | models/services/storage/tests; owner-confirmed production counts | schema, domain rule, or production inspection |
| `01-canonical/summary/PERMISSIONS.md` | authorization | critical | verified | active | 2026-09-29 | access/auth services and access tests | role or permission change |
| `01-canonical/summary/PRODUCTION_SAFETY.md` | rollout | critical | mixed | active | 2026-09-29 | `AGENTS.md`, rollout docs; owner runtime statement last verified 2026-09-24 | every production task |
| `01-canonical/summary/CATALOG.md` | catalog | high | verified | active | 2026-09-29 | `data/catalog.json`, importer/tests, commit `a57cc389...` | catalog/import change |
| `01-canonical/decisions/BARBOT_DECISIONS.md` | cross-cutting | high | verified | active | 2026-10-14 | implementation, tests, Git history through `c2ce9fd2...` | contradicting implementation or owner decision |
| `01-canonical/lessons/KNOWN_RISKS_AND_LESSONS.md` | reliability | high | mixed | active | 2026-10-14 | source/tests/history and labeled owner observations | regression or new incident |
| `01-canonical/reference/EVIDENCE_MAP.md` | routing | medium | verified | active | 2026-09-29 | repository paths | file move or ownership change |
| `features/FEATURE_INDEX.md` | feature routing | medium | verified | active | 2026-10-14 | repository paths | feature/package change |
| `features/AI_ASSISTANT.md` | AI contract | high | verified | active | 2026-10-14 | assistant source/tests including follow-up/local-action regressions | assistant routing, context, capability or schema change |
| `features/BAR_JARVIS_ROADMAP.md` | AI product goal/roadmap | critical | owner-approved | planned | 2026-10-14 | owner-approved Definition of Done; implementation evidence is separate | owner changes goal, threshold, phase order or safety boundary |
| `features/AI_CAPABILITY_LANGUAGE.md` | AI language coverage | high | mixed | active | 2026-10-14 | `CAPABILITIES`; verified tests; explicit pending cells | capability inventory or verified-language change |
| `symbols/SYMBOL_INDEX.md` | symbol routing | medium | verified | active | 2026-09-29 | repository symbols | rename or package change |
| `02-working/MEMORY_UTILITY_LOG.md` | utility pilot | medium | verified | active | 2026-09-29 | local event contract | after 10 eligible tasks |

Owner-confirmed runtime facts are not promoted to live-verified facts by this
manifest. They must be revalidated under the applicable production stage before
being used for an operational decision.

## Current development checkpoint

- Schema target remains v14. The follow-up/local-action development changes do
  not change catalog, warehouse seed or migrations.
- Production rollout of the development changes has not been performed.
- Service state, deployed HEAD, schema, counts, flags and provider health remain
  runtime facts and must be revalidated under the controlled rollout policy
  before deployment.
