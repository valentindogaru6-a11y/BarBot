# Global KOS

Global KOS is a reusable memory protocol for AI-assisted work across codebases and investigation domains.

It separates:

- shared methodology in `./AGENTS.md` and `./global-kos/methodology/`
- local project facts in that project's own `memory/`
- reusable agent guidance and starter templates in `./global-kos/agents/` and `./global-kos/templates/`

## Repository Layout

- `methodology/`: memory architecture, classification, adoption, trust,
  freshness, evolution, and compression guidance
- `agents/`: concise adapters for supported AI agents
- `templates/`: shared memory skeleton and optional domain-specific seeds

## Start A Project

1. For a new project, copy this repository's root `AGENTS.md` to the project root. For an existing project, preserve its `AGENTS.md` and merge in the KOS rules.
2. Copy the contents of `./global-kos/templates/_shared-local-memory/memory/` into the target's root `memory/` directory. Do not copy the outer template directory, which would create `memory/memory/`.
3. Optionally overlay the contents of one domain template's `memory/seed/` into `memory/`.
4. Merge relevant domain-template `AGENTS.md` guidance into project instructions; never replace existing project instructions blindly.
5. Fill in `memory/01-canonical/summary/PROJECT_SUMMARY.md` from verified project facts.
6. Run `./global-kos/Validate-Template.ps1` from this repository to verify a clean bootstrap.

Keep links portable and project-specific facts inside that project's local memory. The shared seed's `memory/00-governing/WORKFLOW.md` contains the complete operative protocol; the separate governing documents in this repository are deeper references, not required seed files.

Use the lightest protocol mode that safely fits the task. Keep the shared methodology domain-neutral; do not put a sample project's facts into it.

## Optional Telegram Pilot Reports

When a project explicitly activates the memory utility pilot, copy `./global-kos/tools/Send-MemoryUtilityReport.ps1` into the target as `tools/kos/Send-MemoryUtilityReport.ps1`. Copy `.env.example` to a local `.env` outside automatically synced folders, set `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and optionally `KOS_UTILITY_REPORT_EVERY`, and ensure `.env` is ignored by Git. The sender rejects config paths under OneDrive. It checks complete JSONL batches, saves a Markdown report locally, and sends it as a Telegram document with a short caption. Run it with `-DryRun` to create a sample report without reading `.env` or using the network.

For an explicitly labelled delivery test, run `powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\kos\Send-MemoryUtilityReport.ps1 -TestSend -ConfigPath "C:\path\outside-OneDrive\.env"`. This test is not added to the pilot sample. Add `-DryRun` to create the test file without reading the config or contacting Telegram.

The bot token stays in the local `.env` file and is never printed or written to reports. Telegram's Bot API necessarily receives the token over HTTPS when a report is sent. The sender contacts no other service.

For a private destination, `TELEGRAM_CHAT_ID` must be the user's `chat.id` from a message sent to the bot (for example, `/start`), not the bot's own ID. Telegram rejects bot-to-bot messages.
