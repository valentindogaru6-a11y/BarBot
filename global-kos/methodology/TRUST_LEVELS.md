# Trust Levels

## Levels

| Level | Meaning | Use |
|---|---|---|
| Confirmed | Verified against code, graph, or runtime evidence and still current for the covered scope | Safe default for reuse |
| High confidence | Strongly supported but not fully revalidated against the latest source/runtime state | Reuse allowed with caution |
| Working hypothesis | Plausible explanation used to drive current investigation | Do not treat as truth |
| Needs verification | Useful note with incomplete validation | Re-check before relying on it |
| Stale | Likely outdated because dependencies or runtime state changed | Do not use without revalidation |
| Archived | Historical only | Read only for context |

## Promotion Rules

- Promote to `Confirmed` only when the scope, evidence, and dependent files or graph nodes are explicit.
- Promote to `High confidence` when the structure is clear but a direct re-check is missing.
- Keep unresolved ideas in `Working hypothesis`.

## Demotion Rules

A note drops to `Needs verification` or `Stale` when:

- dependent files changed
- Graphify shows a changed dependency path
- a newer working-memory note conflicts with it
- runtime behavior contradicts it

## Required Metadata

Every new memory note should track:

- `scope`
- `trust`
- `last_verified`
- `evidence`
- `depends_on`
