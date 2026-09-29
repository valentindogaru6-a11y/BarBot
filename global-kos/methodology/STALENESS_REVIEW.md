# Staleness Review

## Purpose

Review only the relevant slice of memory for each task. Do not perform full-memory reviews by default.

## Slice-Only Rule

At task start, review only the memory items directly involved in the task scope:

- relevant summary notes
- relevant symbol cards
- relevant feature notes
- working-memory entries in scope
- freshness entries tied to those notes

## Review Triggers

Check for staleness when:

- related files changed
- Graphify reveals a new dependency chain
- runtime evidence conflicts with current memory
- a newer canonical summary overlaps the same scope
- a task repeatedly bypasses a memory item because it is no longer useful

## Candidate-For-Archive Rules

Mark an item as candidate-for-archive when:

- it is not `Critical` or `High`
- it has not been used for a long time
- it is replaced by a better summary or decision
- it is historical context rather than active reasoning material

## Supersession Rules

Mark an item `Superseded` when:

- a fresher canonical summary covers the same scope better
- a decision record replaces the old rationale
- a working-memory note has been promoted into canonical form

## Historical Rules

Move an item to `archive/historical` when:

- it still explains past project phases usefully
- it should not appear in default reasoning anymore

## Conflict Rule

If an older note conflicts with a fresh summary:

1. trust the fresh summary for active reasoning
2. mark the older item `Possibly stale`, `Stale`, or `Superseded`
3. note the conflict in the relevant memory update or freshness entry
