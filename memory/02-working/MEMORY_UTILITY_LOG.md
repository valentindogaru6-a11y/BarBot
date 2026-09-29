# Memory Utility Pilot

Status: Active
Target sample: 10 eligible Standard/Full tasks
Event source: `MEMORY_UTILITY_EVENTS.jsonl`
Delivery: local files only; no network or Telegram sender

## Event contract

Append one JSON object after each eligible Standard/Full task. Lightweight tasks
and tasks for which project memory was irrelevant do not count. The bootstrap
event is marked `counts_toward_sample: false` and is not one of the ten tasks.

Record these fields without inventing measurements:

- `task_mode`;
- `memory_files_read` and `source_files_read`;
- useful memory hits;
- stale or conflicting notes;
- memory updates;
- source reads demonstrably avoided;
- incorrect or outdated memory incidents;
- estimated usefulness and overhead.

Use `unknown` where an effect was not measured. Never store secrets, Telegram
identifiers, personal data, production values, or message contents.

## Review after 10 eligible tasks

Summarize helpful, neutral, misleading, and missed outcomes; maintenance effort;
and whether to keep, refine, prune, or stop the pilot. This is a small qualitative
sample, not causal proof or a universal score.
