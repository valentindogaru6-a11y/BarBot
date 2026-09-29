# Shared Local Memory Base

Copy the contents of this directory's `memory/` folder into the target project's root `memory/` folder. Do not copy this outer directory as `memory/`, or the result will be nested as `memory/memory/`.

Use the root KOS `AGENTS.md` as the protocol entrypoint. Keep an existing project `AGENTS.md`; merge in the KOS rules instead of replacing project instructions.

This seed contains the complete compact workflow and routing indexes. Add project facts only after onboarding.

The memory utility pilot is opt-in. Its log is seeded as `Not started`; set it to `Active` only when you want to measure memory usefulness in this project.

To enable Telegram reports, copy `global-kos/tools/Send-MemoryUtilityReport.ps1` into the target as `tools/kos/Send-MemoryUtilityReport.ps1`, keep the real `.env` outside automatically synced folders, and add the local secret and generated report paths to `.gitignore`. The sender rejects OneDrive config paths. Use `-TestSend` for a Telegram delivery test that does not change the pilot sample, or combine it with `-DryRun` to test without reading `.env` or using the network.
