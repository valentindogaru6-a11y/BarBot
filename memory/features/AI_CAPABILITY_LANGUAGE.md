# AI Capability Language Coverage

Scope: language and regression coverage index for all registered AI capabilities
Trust: Mixed; structural columns are source-confirmed, language coverage is explicit
Lifecycle: Active
Last verified: 2026-10-14
Evidence: `app/services/assistant/capabilities.py`, assistant tests, commit `c2ce9fd2ae46509b8ed47aa6d44e1cc1b3ccf80f`
Depends on: `features/AI_ASSISTANT.md`, `docs/ai_capability_inventory.md`
Importance: High

## Planner corpus

Service-word entity coverage from `d515ed0...` strengthens
`warehouse.prepare_writeoff` / `warehouse.prepare_transfer`: one clause action
pass, typed body/entity spans, exact full-name precedence and provider
non-authority over product binding. `tests/test_assistant_service_entities.py`
covers exact/alias/missing products, both foreground modes, clarification/restart
and bounded scoped-fact cleanup. Separate deterministic v1.3 has 360 cases;
v1.1/v1.2 expectations stay frozen. This is offline evidence only; extended live
coverage remains pending.

Offline mutation-role coverage from `a15759c...` adds Unicode/control rejection
and grammar-aware service-word product slots for `warehouse.prepare_writeoff`
and `warehouse.prepare_transfer`. Missing-unit clarification for product Стоп
does not cancel an older draft; standalone cancel remains an owned typed action.
Frozen v1.1 seed 20261109 and separate v1.2 cases are tracked in
`tests/assistant_mutation_fuzz.py` / `tests/test_assistant_unicode_roles.py`.
These are deterministic synthetic regressions, not measured live accuracy;
extended live language coverage remains pending.

`tests/fixtures/assistant_planner_corpus_v1.json` defines 17 offline contract
cases covering corrections, pronouns, topic switch/return, incomplete
mutations, colloquialisms, mixed Russian/Ukrainian typos, attendance,
reports/delivery, administration, false positives, confirmation/cancellation
and provider failure. The owner’s Облепиха correction is `correction` plus
`catalog.manage` with the supplied stable ID, never a literal catalog query.
Decisions are scripted for contract tests; live Gemini coverage remains
`pending`, so this corpus carries no accuracy percentage.

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

Compound cancellation hardening from `27f0b9c...` adds offline-tested families
for both `warehouse.prepare_writeoff` and `warehouse.prepare_transfer`: bounded
clause veto, contradictory/double-negation clarification, one-edit cancellation
typos and the closed positive `не забудь + action` template. Reason/product
words are not fuzzy cancellation targets. Exact reconstructed synthetic review
cases live in `tests/test_assistant_compound_cancellation.py`; handler/FSM and
temporary-v15 ledger tests check no transient draft writes for blocked input.
This is not complete language coverage; extended live evaluation is pending.

Offline hardening from `96f50da...` is covered in
`tests/test_assistant_mutation_language.py`. Writeoff/transfer retain typed
`proceed/cancel/corrected/ambiguous/invalid` disposition, whole Decimal tokens
and explicit missing fields. RU/UA action cancellation blocks preparation;
descriptive comment negation does not. Explicit last-value corrections create
one draft only; ambiguous corrections, ranges and malformed/signed quantities
create none. Multiline numeric invalidity rejects the whole new AI batch.
Pending quantity/unit/destination answers are actor/chat-isolated and restart-
safe; cancellation uses the existing draft/proposal service, never confirmation.
These are tested grammar families, not complete natural-language or live coverage.

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

## Conversation-brain coverage

Offline contracts now cover gender-tolerant pronouns, first/second/last bounded
candidates, exact confirmed-memory commands and a maximum two-step tool loop.
The versioned `bar-jarvis-conversation-v1` fixture contains at least one
three-turn chain for each of the 41 capability IDs. This is structural and
safety coverage, not measured provider routing quality; per-capability natural
language accuracy and live synonym coverage remain `pending live coverage`.
Scripted runner scores always carry provider mode `offline_scripted`; live
Groq/Gemini metrics remain `pending — requires controlled shadow evaluation`.
