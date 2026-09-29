# Warehouse Domain Summary

Scope: warehouse semantics and feature relationships
Trust: Confirmed from code/tests; production counts are Owner-confirmed
Lifecycle: Fresh
Last verified: code 2026-09-29; runtime 2026-09-24
Evidence: warehouse services/storage/models/tests, owner rollout confirmation
Depends on: schema v12 and warehouse feature flag
Importance: Critical

Core locations are `Бар`, `Кухня`, and `Кондитерка`. Default business timezone
is `Europe/Kyiv`; cutoff is `23:00`. A local instant at or after cutoff belongs
to the next business date. Shifts snapshot timezone/cutoff so historical state
does not change when settings later change.

Quantities are parsed with `Decimal`, converted to integer values scaled by
1,000,000, and stored with base units `kg`, `l`, or `pcs`. Binary floats are
forbidden at the boundary.

Operations:

- writeoff: product, quantity, source and optional per-item reason/comment;
- transfer: source and explicit destination, which must differ;
- development: separate title/ingredient workflow and comment semantics.

Multi-line writeoff/transfer input becomes one intake batch. Product,
quantity/unit, destination, and comments are resolved per line; the whole batch
is committed atomically only after confirmation. Unknown products and missing
units are separate states. Fuzzy candidates are suggestions, never silent
acceptance. Duplicate messages/callbacks are protected by unique keys,
revisions, and idempotent state transitions.

The broader warehouse shift groups operations for one source/business date.
Required rules and closing sessions resolve mandatory writeoffs before close.
Immutable versioned XLSX reports feed recipients and persistent delivery rows;
reminders and schedules use atomic claims plus stale recovery. Corrections of
closed shifts produce linked report versions and remain isolated from automatic
schedule selection.

Personal attendance sessions in schema v12 record each employee's open/close
interval and are distinct from the shared warehouse shift. They reference the
warehouse shift but have independent lifecycle, forced-close reason, reminder
recipient selection, and attendance XLSX export.

Owner-confirmed runtime baseline: 416 active products, including 2 manual
production additions that seed reconciliation must preserve. Revalidate live
before any production mutation.
