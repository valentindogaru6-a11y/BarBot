# Memory Policy

## Purpose

This policy controls reuse, lifecycle, importance, pruning, archive behavior, and knowledge-loss prevention for project memory.

## Reuse Before Re-read

Reuse memory instead of source reads when all conditions hold:

- the summary or symbol/feature note matches the task scope
- trust is `Confirmed` or `High confidence`
- lifecycle state is `Fresh` or `Stable`
- Graphify does not expose a broader unresolved dependency chain

## Escalation Policy

Follow this order for Standard and Full work. Lightweight work may stop after the smallest relevant memory note when no broader source or durability risk exists.

1. Summary layer
2. Symbol memory
3. Feature memory
4. Current context
5. Working memory
6. Graphify
7. Source code
8. Archive only if historical explanation is explicitly needed

## Knowledge Extraction Protocol

Knowledge Extraction is mandatory for Full Mode, for Standard Mode when durable knowledge was created or changed, and for Lightweight Mode only when the task creates a reusable fact, decision, pattern, lesson, symbol, feature, or workflow change. It remains mandatory before clearing a non-empty `CURRENT_CONTEXT.md`.

For each new finding, ask:

1. Is this a stable architectural fact?
   - update Canonical or Summary
2. Is this a repeated project pattern?
   - update Pattern Memory
3. Is this a useful acceleration insight?
   - update Lessons Learned
4. Does this improve a symbol card?
   - update Symbol Memory
5. Does this improve feature routing or feature knowledge?
   - update Feature Memory
6. Is this an architectural or workflow decision?
   - update Decision Memory
7. Is this a temporary but useful hypothesis?
   - update Working Memory
8. Is this a rejected hypothesis?
   - update DEAD_ENDS
9. Does this invalidate older knowledge?
   - update freshness, trust, lifecycle, and mark stale or superseded items

No stable finding may disappear without either being stored or consciously discarded.

## Update Before Create Rule

Before creating a new note, try in this order:

1. update an existing summary
2. update an existing pattern
3. update an existing feature note
4. update an existing symbol card
5. update an existing lesson
6. update an existing decision or working-memory note

Create a new document only if the existing structure truly does not fit.

## Duplication Policy

If the new knowledge partially exists already:

- do not create a duplicate
- expand the existing note
- prefer strengthening the current pattern/summary/feature/symbol over creating a sibling note

## Lifecycle States

- `Fresh`: newly verified against current evidence
- `Stable`: repeatedly reliable and unchanged across tasks
- `Possibly stale`: a relevant dependency may have changed, but conflict is not yet confirmed
- `Stale`: evidence or dependency changes make the item unsafe as a fact
- `Superseded`: replaced by a newer summary, decision, or canonical note
- `Archived`: historical or retired; not part of default working context

## Lifecycle Transitions

`Fresh -> Stable`
- the item is reused successfully across multiple tasks without contradiction
- no relevant dependency change is detected

`Fresh/Stable -> Possibly stale`
- a related file changes
- Graphify shows a changed dependency chain
- a nearby runtime or canonical note changes and may affect the item

`Possibly stale -> Stale`
- conflicting runtime evidence appears
- code or graph validation disproves the previous understanding
- the item fails revalidation

`Stale -> Superseded`
- a newer canonical summary or decision replaces the old item explicitly

`Superseded -> Archived`
- the item is no longer needed for default reasoning but remains historically useful

`Any active state -> Archived`
- the item is historical-only and no longer part of active reasoning
- working context was redistributed and the scratch note is no longer needed

## Importance Levels

- `Critical`: core architecture, key workflow, or essential operating knowledge; cannot be auto-archived
- `High`: important subsystem or stable process knowledge; archive only after explicit replacement
- `Medium`: useful but replaceable knowledge; may be archived when stale or superseded
- `Low`: narrow or temporary knowledge; may be compressed or removed from working memory when stale
- `Historical`: keep only in archive; never use by default

## Importance Rules

- `Critical` items cannot be archived automatically.
- `High` items can be archived only after a newer canonical replacement exists.
- `Medium` items can move to archive when stale or superseded.
- `Low` items can be compressed or removed from working memory when stale, duplicated, or fully redistributed.
- `Historical` items belong in `memory/archive/` only.

## Re-read Triggers

Read the underlying source only when at least one trigger applies:

- the relevant note has lifecycle `Possibly stale` or `Stale`
- the note trust is `Needs verification` or `Working hypothesis`
- related files or graph nodes changed
- the task requires exact control flow or exact data shape
- there is conflicting memory
- Graphify narrows the problem but leaves a semantic gap

## Current Context And Inbox Policy

`CURRENT_CONTEXT.md` is the only per-task scratch memory.
`DISCOVERY_INBOX.md` is the temporary holding area for newly found observations.

- Update current context during Standard and Full tasks, or during Lightweight tasks that touch files, continue an investigation, or span multiple turns.
- Store new findings in the discovery inbox when they may become reusable memory.
- Do not let either file become permanent history.
- Before cleanup, every discovery inbox entry must be redistributed to exactly one destination.
- `CURRENT_CONTEXT.md` cannot be cleared until all stable findings are redistributed.
- If the discovery inbox still contains unresolved stable findings, cleanup is forbidden.

## Pruning Policy

After every Full task, and after any Standard or Lightweight task that creates durable memory, the agent must:

1. run Knowledge Extraction
2. redistribute every discovery inbox item
3. move confirmed stable knowledge into canonical, summary, pattern, lesson, symbol, feature, or decision memory as appropriate
4. move temporary hypotheses into working memory
5. move rejected hypotheses into `DEAD_ENDS.md`
6. mark outdated items `Stale` or `Superseded`
7. compress low-significance temporary notes
8. move historical-only data into archive
9. avoid deleting anything important without explicit reason
10. clear `CURRENT_CONTEXT.md` only after the no-loss check passes

## No-Loss Check

Before clearing `CURRENT_CONTEXT.md`, answer:

`Have all stable findings been moved into permanent memory or consciously discarded?`

If the answer is `no`, cleanup is forbidden.

## Deletion Rules

Deletion is allowed only for:

- temporary scratch notes
- exact duplicates
- `Low` plus `Stale` working-memory items
- items fully replaced by a new canonical summary and not historically useful

Everything else should be archived before deletion.

## Dead-End Policy

Before exploring a new path in this integrated repository, check the project's
[DEAD_ENDS.md](../../memory/02-working/DEAD_ENDS.md).

Revisit a dead end only if:

- related files changed
- Graphify now exposes a new route
- new runtime evidence invalidates the old rejection
