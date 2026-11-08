# Current Context

Metadata: scope `working`; trust `working`; lifecycle `ephemeral`; last cleared
`2026-09-29`.

## Status

Gemini-first resilience development starts at `197a765...`: explicit disabled
Groq fallback, shared physical admission/pacing, bounded logical text/voice
requests and privacy-safe attempt traces. Six-case paced bake-off selected
Gemini fast (6/6 canonical/semantic); Groq 20B/120B remain below contract gates.
That small prior sample is not a production-quality percentage. Production
remains local-first; owner-only Gemini shadow, Groq fallback enablement and
AI-first are pending separate review/authorization. No live/provider/production
actions belong to this implementation task; catalog/seed/schema stay unchanged.

Focused scoring fix from `a1900e19...`: close the invisible-draft oracle gap
with actual DB snapshots/statement ledger and scorer-owned allowed transitions;
attribute accuracy/confusion to expected domains with required metric gates.
Scope is evaluation only; corpus semantics and production behavior unchanged.
Live provider quality remains pending.

Corpus-v2 evaluation development from `bd73be96...`: smoke remains frozen v1;
core and extended add seeded natural-language scenarios with real synthetic
services, per-domain readiness and explicit lifecycle/fault controls. Only
corpus/harness/tests/docs change. Live accuracy, provider health and unsupported
dialogue behaviors remain pending. No production or external API actions.

Live harness hardening starts from `b0c2780...`: independent provider-step
failure aggregation, owned temporary SQLite identity, authorization observation
and explicit partial budget policy. This is development-only, with synthetic
fixtures/fake HTTP; live provider quality and production facts remain pending.

Isolated live evaluation development starts from `c1496bf...`. It composes real
services on fresh synthetic temporary v15, without Telegram or production
startup. The physical HTTP budget spans the entire run. Scripted evaluation is
kept separate; live provider quality remains pending. No provider API call or
production action is part of this development task.

Security-fix development on 2026-10-24 starts at
`2d18075064966d0d54d51ddb73dac6486eb4d62c`. The five review findings concern
memory trust/chat ownership, typed iterative planner dispatch, strict unreleased
v15 validation and evaluation failure semantics. Evidence is offline only;
production, provider health and live/shadow accuracy remain pending. Catalog,
seed and legacy migrations are outside this change.

Checkpoint 2026-10-22: AI-first planner foundation started from
`a5482d033c9a480c544547459a71b39caf69b74f`. Safe default remains
`local_first`; shadow/AI-first live coverage is pending. Offline corpus v1 has
17 scripted contract cases and establishes no provider-accuracy percentage.
Development schema target is v15 for explicit long-term assistant memory.
Catalog and warehouse seed content are unchanged; production remains on its
separately verified version until an authorized staged rollout.

Development follow-up at `0205ec4f...` fixes shadow lifecycle and evaluation
observability. Live planner Cases 2–4 remain unknown; no accuracy percentage is
established. Production rollout is outside this development task.

Development at starting HEAD `d8f0789...` adds separate strict Groq/Gemini
wire contracts, canonical conversion, sequential bounded fallback and a
process-local circuit breaker. Groq is configured as primary and Gemini as
fallback, but live quality, promotion and production rollout remain pending.
Schema stays v14; catalog, seed and migrations are unchanged.

At development HEAD `fbbd6ab...`, controlled Groq Call 1 passed the strict
planner contract; Call 2 returned HTTP 400 before semantic evidence was
available. Offline reconstruction shows identical strict schema hashes across
both turns. Safe provider-error classification is being added without changing
the schema. Exact correction quality and the original provider subcategory are
unknown; one controlled correction re-smoke remains pending.

Development from `7871802...` replaces the provider-generated monolithic
decision with compact Stage 1 routing, server-owned target mapping and local
policy assembly. Stage 2 uses deterministic parsing or bounded
capability-specific contracts. Corpus v2 has nine route and four argument
contract cases; live compact-route quality remains pending. Schema stays v14;
catalog, seed and migrations are unchanged.

Task: none
State: empty
Started: n/a

## Inputs Used

- none

## Findings

- none

## Pending Redistribution

- none
