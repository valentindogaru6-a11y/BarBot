# BarBot Memory Index

Scope: routing entrypoint for BarBot project memory
Trust: Confirmed
Lifecycle: Fresh
Last verified: 2026-10-14
Evidence: repository at `c2ce9fd2ae46509b8ed47aa6d44e1cc1b3ccf80f`
Depends on: `AGENTS.md`, `memory/00-governing/WORKFLOW.md`
Importance: Critical

Read this file first, then open only the smallest route needed by the task.

## Fast routes

| Task | Minimum memory slice |
|---|---|
| Direct project/status question | `01-canonical/summary/PROJECT_SUMMARY.md` |
| Catalog/import | `01-canonical/summary/CATALOG.md`, feature index |
| Warehouse behavior | `01-canonical/summary/WAREHOUSE_DOMAIN.md`, decisions index |
| Roles/authorization | `01-canonical/summary/PERMISSIONS.md` |
| Production/release | `01-canonical/summary/PRODUCTION_SAFETY.md`; live revalidation is mandatory |
| Architecture/impact | `01-canonical/summary/ARCHITECTURE.md`, symbol index, then targeted source |
| Risks/regressions | lessons index |
| Bar Jarvis/AI | `features/AI_ASSISTANT.md`, [product goal and roadmap](features/BAR_JARVIS_ROADMAP.md), [capability language coverage](features/AI_CAPABILITY_LANGUAGE.md) |
| Active Standard/Full task | relevant summary plus `02-working/CURRENT_CONTEXT.md` |

## Indexes

- [Summary index](01-canonical/summary/SUMMARY_INDEX.md)
- [Decisions index](01-canonical/decisions/DECISIONS_INDEX.md)
- [Patterns index](01-canonical/patterns/PATTERNS_INDEX.md)
- [Lessons index](01-canonical/lessons/LESSONS_INDEX.md)
- [Feature index](features/FEATURE_INDEX.md)
- [Symbol index](symbols/SYMBOL_INDEX.md)
- [Working-memory index](02-working/WORKING_MEMORY_INDEX.md)
- [Freshness manifest](03-graphify/FRESHNESS_MANIFEST.md)

## Mode rule

- Lightweight reads one relevant slice and source only when needed.
- Standard adds current context, targeted localization, and knowledge extraction.
- Full adds trust/freshness/lifecycle review for broad or risky work.

Templates in `global-kos/templates/`, archive, and heavy references are never
part of the default read path. Memory cannot authorize production actions.
