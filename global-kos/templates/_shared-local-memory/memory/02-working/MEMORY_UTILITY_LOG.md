# Memory Utility Pilot

Status: Not started
Target sample: 10 eligible Standard/Full tasks
Event source: `MEMORY_UTILITY_EVENTS.jsonl`

Set status to `Active` when intentionally evaluating memory in this project. The pilot is off by default for newly seeded projects.

Append exactly one JSON object per eligible task to the event source. Do not store observations in this guide or score individual notes. A task is eligible when relevant project memory was used or a relevant existing note was discovered to have been missed. Skip Lightweight tasks and tasks with no relevant project memory.

```json
{
  "date": "YYYY-MM-DD",
  "mode": "Standard",
  "signal": "used",
  "outcome": "helpful",
  "effort": "unknown",
  "evidence": "A concise, redacted observable effect"
}
```

## Labels

- Signal: `used` or `relevant note missed`
- Outcome: `helpful` (changed a decision, avoided rereading, prevented an error, or confirmed a consequential fact); `neutral` (no observable effect); `misleading` (stale, wrong, duplicate, or irrelevant memory); `missed` (relevant memory was found only after rediscovery)
- Effort: `saved`, `roughly equal`, `extra`, or `unknown`. Use exact time only when measured; otherwise keep this qualitative.
- Evidence: concise and redacted; never include credentials, personal data, or large code excerpts. Do not count merely opening a note as usefulness.

When enabled, the optional sender groups complete batches into a Markdown report with a short Telegram caption. Without the sender, events remain local.

## Review After 10 Eligible Tasks

- Helpful / neutral / misleading / missed counts:
- Maintenance effort:
- Decision: keep / refine retrieval / refresh or prune memory / stop pilot
- Rationale:

This small self-reported sample is exploratory. Do not present it as causal proof or a universal score.
