# Localization Workflow

Scope: source localization between memory and broad code reads
Trust: Confirmed protocol; Graphify availability is Needs verification
Lifecycle: Fresh
Last verified: 2026-09-29
Evidence: repository tools available during bootstrap
Depends on: `memory/MEMORY_INDEX.md`
Importance: High

Graphify is not installed and is not required for BarBot work. Do not install
it merely to satisfy the protocol. Use this fallback:

1. choose a feature or symbol anchor from the indexes;
2. use `rg`/`rg --files` and Python import boundaries to identify the minimal dependency slice;
3. open only files needed for exact control flow/data shape;
4. use targeted tests for behavioral evidence;
5. record impacted memory notes before changing code.

Expected output: anchor, minimal dependency slice, likely read set, impacted
memory notes, and unresolved gaps. Escalate only when this cannot answer the task.
