# Applying Global KOS To A New Project

## Goal

Create a local project memory without changing the shared methodology.

## Steps

1. Choose a domain template only if it adds useful project-specific discovery guidance.
2. For a new project, copy the repository-root `AGENTS.md` to the project root. For an existing project, preserve its `AGENTS.md` and merge in the KOS protocol.
3. Copy the contents of `global-kos/templates/_shared-local-memory/memory/` into the target's root `memory/` directory. Do not copy the outer folder as `memory/`.
4. Optionally overlay the contents of the selected domain template's `memory/seed/` into the target `memory/`.
5. Merge relevant domain-template `AGENTS.md` notes into project instructions; do not replace an existing root file blindly.
6. Fill in `memory/01-canonical/summary/PROJECT_SUMMARY.md` only with verified scope and facts.
7. Create additional summaries, features, symbols, and references only when they improve routing or reuse.
8. Run `global-kos/Validate-Template.ps1` from this repository and confirm the target has `memory/MEMORY_INDEX.md` (not `memory/memory/MEMORY_INDEX.md`).
9. Keep project facts inside the project's local `memory/` tree; change Global KOS only for reusable methodology improvements.

## Migration Rule

When converting an existing project:

- classify files into universal method, local facts, and reusable seeds
- move only the method into Global KOS
- keep project facts local
- convert repeatable project patterns into template seeds only when they generalize by domain

## Success Criteria

A new project is correctly onboarded when:

- the global methodology is unchanged
- the project has its own local `memory/`
- the local `AGENTS.md` points to that local memory
- new findings flow into local summaries, patterns, lessons, decisions, symbols, and features
