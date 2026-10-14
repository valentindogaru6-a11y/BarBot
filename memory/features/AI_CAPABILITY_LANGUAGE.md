# AI Capability Language Coverage

Scope: language and regression coverage index for all registered AI capabilities
Trust: Mixed; structural columns are source-confirmed, language coverage is explicit
Lifecycle: Active
Last verified: 2026-10-14
Evidence: `app/services/assistant/capabilities.py`, assistant tests, commit `c2ce9fd2ae46509b8ed47aa6d44e1cc1b3ccf80f`
Depends on: `features/AI_ASSISTANT.md`, `docs/ai_capability_inventory.md`
Importance: High

The authoritative capability count and structural metadata remain `CAPABILITIES`;
this index must not become an independent permission source. Role abbreviations:
`SA` super-admin, `A` admin, `B` bartender, `Acc` accountant. Risks are
`0` read/navigation, `1` safe flow, `2` mutation, `3` high-risk mutation.

Every capability coverage record grows through these fields: capability ID,
role, risk, domain, verified natural commands, verified conversational synonyms,
required parameters, allowed clarifications, confirmation requirement, local
fallback and regression tests. Unverified language is always `pending live
coverage`; never infer examples merely from a title or adapter name.

The owner-approved completion target is 10–15 approved natural formulations
per capability and at least 90% aggregate correct routing/argument extraction
on a versioned corpus, with 100% applicable security-boundary cases. These are
future acceptance thresholds, not current results. Until corpus version, sample
size, scoring rules and reproducible results exist, coverage remains `pending`.

| Capability ID | Roles | Risk | Domain | Parameters | Confirmation | Language / fallback / regression coverage |
|---|---|---:|---|---|---|---|
| `navigation.main` | SA,A,B,Acc | 0 | navigation | none | none | pending live coverage |
| `catalog.search` | SA,A,B | 0 | catalog | query/relation/selected stable entity | none | tested direct/relation lookup and similarity follow-up below; deterministic local retrieval; fuzzy results remain candidates |
| `catalog.get` | SA,A,B | 0 | catalog | stable entity | none | tested pronoun/card-content follow-ups below; stable-ID callback/context tests |
| `catalog.manage` | SA,A | 2 | catalog | mutation fields | explicit | pending live coverage |
| `users.list` | SA,A | 0 | employees | none | none | pending live coverage |
| `users.list_bartenders` | SA,A | 0 | employees | none | none | pending live coverage |
| `users.get` | SA,A | 0 | employees | query | none | pending live coverage |
| `users.change_role` | SA,A | 3 | employees | mutation fields | reason + explicit | pending live coverage |
| `attendance.status` | SA,A,B | 0 | attendance | none | none | pending live coverage |
| `attendance.list_open` | SA,A | 0 | attendance | none | none | pending live coverage |
| `attendance.get` | SA,A | 0 | attendance | stable entity | none | pending live coverage |
| `attendance.open_own` | SA,A,B | 2 | attendance | none | explicit | pending live coverage |
| `attendance.close_own` | SA,A,B | 2 | attendance | none | explicit | pending live coverage |
| `attendance.force_close` | SA,A | 3 | attendance | mutation fields | reason + explicit | pending live coverage |
| `attendance_reports.generate` | SA,A | 1 | reports | period | none | pending live coverage |
| `warehouse.shift_summary` | SA,A,B,Acc | 0 | warehouse | none | none | pending live coverage |
| `warehouse.list_operations` | SA,A,B,Acc | 0 | warehouse | none | none | pending live coverage |
| `warehouse.required_status` | SA,A,B,Acc | 0 | warehouse | none | none | pending live coverage |
| `warehouse.prepare_writeoff` | SA,A,B | 1 | warehouse | product, Decimal quantity, unit; optional comment | existing draft confirmation | tested imperative forms below; local draft fallback; missing quantity/unit uses clarification; remaining colloquial forms pending live coverage |
| `warehouse.prepare_transfer` | SA,A,B | 1 | warehouse | product, Decimal quantity, unit, destination; optional comment | existing draft confirmation | tested imperative forms below; local draft fallback; missing destination uses clarification; remaining colloquial forms pending live coverage |
| `warehouse.commit_batch` | SA,A,B | 2 | warehouse | stable entity | explicit | pending live coverage |
| `warehouse.cancel_batch` | SA,A,B | 2 | warehouse | stable entity | explicit | pending live coverage |
| `warehouse.start_required_flow` | SA,A,B | 1 | warehouse | none | existing flow rules | pending live coverage |
| `warehouse.update_operation` | SA,A,B | 3 | warehouse | mutation fields | reason + explicit | pending live coverage |
| `warehouse.delete_operation` | SA,A,B | 3 | warehouse | mutation fields | reason + explicit | pending live coverage |
| `warehouse.close_shift` | SA,A,B | 3 | warehouse | mutation fields | reason + explicit | pending live coverage |
| `warehouse.open_correction` | SA,A | 3 | warehouse | mutation fields | reason + explicit | pending live coverage |
| `warehouse.commit_correction` | SA,A | 3 | warehouse | mutation fields | reason + explicit | pending live coverage |
| `reports.generate` | SA,A,B | 1 | reports | period | none before existing report flow | pending live coverage |
| `reports.status` | SA,A,B,Acc | 0 | reports | stable entity | none | pending live coverage |
| `reports.history` | SA,A,B,Acc | 0 | reports | none | none | pending live coverage |
| `reports.send` | SA,A,B | 3 | reports | mutation fields | explicit | pending live coverage |
| `recipients.list` | SA,A | 0 | recipients | none | none | pending live coverage |
| `recipients.create_or_update` | SA,A | 2 | recipients | mutation fields | explicit | pending live coverage |
| `recipients.disable` | SA,A | 2 | recipients | mutation fields | explicit | pending live coverage |
| `reminders.get` | SA,A | 0 | reminders | none | none | pending live coverage |
| `reminders.update` | SA,A | 2 | reminders | mutation fields | explicit | pending live coverage |
| `schedules.list` | SA,A | 0 | schedules | none | none | pending live coverage |
| `schedules.create_or_update` | SA,A | 2 | schedules | mutation fields | explicit | pending live coverage |
| `schedules.disable` | SA,A | 2 | schedules | mutation fields | explicit | pending live coverage |
| `capabilities.list` | SA,A,B,Acc | 0 | help | query | none | deterministic matcher covered; runtime-filtered registry; capability-controller tests |

## Verified catalog language

- `найди пф для Berry radiance` → `preparation_for` → `Ягодное пюре`.
- `найди мне пфку для ягодного чая` → `Ягодная основа для чая`.
- `покажи что входит в ягодный чай` → `ingredients_of` →
  `Ягодная основа для чая`.
- A selected card followed by a pronoun-based ingredients question resolves
  against the same stable entity even when grammatical gender differs.
- A selected card followed by a request for something similar uses
  `similar_to`, excludes the source and returns local candidates without a
  provider call.

## Verified warehouse language

- Writeoff imperatives equivalent to `спиши`, `списать`, `сделай/оформи
  списание`, `нужно списать` and `убери в списание`, with product, quantity and
  unit, create only the existing persistent draft/preview.
- Transfer imperatives equivalent to `перемести`, `перенеси` and `сделай
  перемещение`, with product, quantity, unit and destination, create only the
  existing persistent draft/preview.
- Missing quantity or destination produces a local clarification; informational
  questions about writeoffs/transfers do not create drafts.
- Confirmation, cancellation, duplicate-confirm and role-change behavior stays
  owned by the existing warehouse flow. Additional natural synonyms remain
  `pending live coverage` until tests or controlled acceptance confirm them.

All other natural-command, synonym, clarification and fallback cells remain
pending live coverage until a test or controlled owner observation confirms
them. Runtime access still comes from `CapabilityRegistry`, `AccessService`,
the pilot gate, private-chat policy and feature flags.
