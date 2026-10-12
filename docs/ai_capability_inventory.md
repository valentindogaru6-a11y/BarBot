# BarBot AI capability inventory

The authoritative machine-readable inventory is `CAPABILITIES` in
`app/services/assistant/capabilities.py`. Permissions below are role filters;
the application service always performs the final authorization check.

| UI action | AI capability | Roles | Confirmation | Status |
|---|---|---|---|---|
| Main menu/catalog search | `navigation.main`, `catalog.search`, `catalog.get` | active catalog roles | none | local adapter |
| Catalog administration | `catalog.manage` | admin, super-admin | explicit | registry contract; existing catalog service/FSM |
| Users and bartenders | `users.list`, `users.list_bartenders`, `users.get` | admin, super-admin | none | local read adapter |
| Change/block user | `users.change_role` | admin, super-admin | reason + explicit | registry contract; existing user service |
| Own attendance | `attendance.status/open_own/close_own` | bartender, admin, super-admin | open/close explicit | local adapter + proposal |
| Attendance administration | `attendance.list_open/get/force_close` | admin, super-admin | force-close reason + explicit | typed read / proposal adapter |
| Attendance XLSX | `attendance_reports.generate` | accountant, admin, super-admin | period preview | existing XLSX coordinator contract |
| Warehouse overview/records | `warehouse.shift_summary/list_operations` | warehouse-view roles | none | typed local read |
| Writeoff/transfer | `warehouse.prepare_writeoff/prepare_transfer/commit_batch/cancel_batch` | warehouse mutation roles | persistent draft confirmation | existing intake service/FSM |
| Required writeoffs | `warehouse.required_status/start_required_flow` | warehouse roles | flow rules | existing closing service/FSM |
| Edit/delete operation | `warehouse.update_operation/delete_operation` | warehouse mutation roles | reason + explicit | registry contract; existing operation service |
| Close warehouse shift | `warehouse.close_shift` | warehouse mutation roles | reason + explicit | registry contract; existing closing service |
| Corrections | `warehouse.open_correction/commit_correction` | admin, super-admin | reason + explicit | registry contract; existing correction service |
| Reports/history | `reports.generate/status/history` | warehouse-view/mutation roles | generation preview | typed read / existing report service |
| Report delivery | `reports.send` | report-send roles | recipient/file preview + explicit | existing delivery service contract |
| Recipients | `recipients.list/create_or_update/disable` | admin, super-admin | explicit for mutation | typed read / proposal contract |
| Reminders | `reminders.get/update` | admin, super-admin | before/after + explicit | typed read / local proposal adapter |
| Schedules | `schedules.list/create_or_update/disable` | admin, super-admin | explicit for mutation | typed read / proposal contract |
| Capability help | `capabilities.list` | all active roles | none | registry-generated |
| Back/pagination | excluded | — | — | pure navigation; no business effect |
| Deployment/systemd/debug | excluded | — | — | outside Telegram BarBot and prohibited |

Risk levels are: 0 read/navigation, 1 safe flow, 2 business mutation, and 3
high-risk mutation. Level 2 requires explicit confirmation. Level 3 also
requires a reason, target revision where applicable, and the existing service
audit/transaction boundary. Callback payloads contain only an action and an
opaque token, never Telegram IDs, raw input, names or mutation arguments.
