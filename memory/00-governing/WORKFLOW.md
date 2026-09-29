# BarBot Memory Workflow

Scope: local memory routing, evidence, maintenance, and utility measurement
Trust: Confirmed from adopted KOS protocol and BarBot safety policy
Lifecycle: Fresh
Last verified: 2026-09-29
Evidence: `AGENTS.md`, `global-kos/methodology/`
Depends on: `AGENTS.md`
Importance: Critical

Use the lightest task mode that safely fits. BarBot production-safety rules in
the root `AGENTS.md` always override this workflow.

## Read And Localize

1. Read the project-root `AGENTS.md` and `memory/MEMORY_INDEX.md`.
2. Read only the smallest relevant summary, then follow its links to decisions, patterns, lessons, symbols, or features as needed.
3. For Standard and Full tasks, check `CURRENT_CONTEXT.md`, `DISCOVERY_INBOX.md`, and relevant working notes.
4. Use Graphify, an index, or targeted search to localize source when impact analysis is needed. Graphify is not installed for this pilot; use `rg`, import/file indexes, and targeted tests without installing extra tooling.
5. Read source only when memory and localization do not answer the question.
6. Do not read the archive by default or create notes that do not make future work faster, narrower, or more accurate.

## Evidence And Trust

- Keep facts scoped to the project and link them to evidence and dependencies when practical.
- Use `Confirmed` only for facts verified against current source, runtime, or an authoritative project decision.
- Mark unverified ideas as `Working hypothesis` or `Needs verification`; do not present them as confirmed.
- Recheck notes marked `Possibly stale` or `Stale` before relying on them. Archived notes are historical context, not current truth.
- Review freshness, trust, lifecycle, and compression only for the memory slice used by the task.

## Task Memory And Knowledge Extraction

- Update `CURRENT_CONTEXT.md` during Standard and Full tasks, and during Lightweight tasks that touch files, continue an investigation, or span multiple turns.
- Put potentially reusable discoveries in `DISCOVERY_INBOX.md` before cleanup.
- Before resetting task memory, move each inbox item to exactly one suitable destination: summary, decision, pattern, lesson, symbol, feature, working memory, dead ends, or archive. Update affected indexes and freshness notes.
- Ask whether any stable fact would be lost. If so, store it first; never clear unresolved discoveries.
- After redistribution, clear the inbox and reset `CURRENT_CONTEXT.md` to its empty template.

## Lifecycle And Pruning

- Prefer updating an existing note over creating a duplicate.
- Use `Fresh` for recently verified knowledge and `Stable` for repeatedly confirmed knowledge. Mark contradicted or outdated notes `Stale` or `Superseded`.
- Archive knowledge that is historically useful but no longer belongs in default reasoning. Delete only temporary scratch, exact duplicates, or material fully replaced and no longer useful.
- Keep project facts in this project's `memory/`; promote only reusable cross-project methodology to shared KOS.

## Memory Utility Pilot

When `memory/02-working/MEMORY_UTILITY_LOG.md` has `Status: Active`, append one valid JSON object per eligible Standard/Full task to `memory/02-working/MEMORY_UTILITY_EVENTS.jsonl`. A task is eligible when relevant project memory was used or a relevant existing note was discovered to have been missed. Skip Lightweight tasks and tasks with no relevant project memory. Do not score individual notes.

Use observable, redacted evidence for the outcome. Never include credentials, personal data, or large code excerpts. Do not claim saved time unless it was measured; otherwise use the coarse effort labels or `unknown`.

After appending an event, keep it local. BarBot does not run the optional
PowerShell/Telegram sender during this pilot. Reports are written only to
`memory/02-working/reports/`. After each 10 observations, review helpful,
neutral, misleading, and missed-memory outcomes alongside maintenance effort.
Choose to keep, refine, or stop the pilot. Treat the small self-reported sample
as exploratory, not causal proof.

## Runtime safety gate

Memory marked `Owner-confirmed`, runtime-sensitive, stale, or older than the
current deployment must be revalidated through the separately authorized
production workflow before use. Memory never grants permission to open
`.env`, production SQLite/audit/XLSX, or operate systemd/polling.
