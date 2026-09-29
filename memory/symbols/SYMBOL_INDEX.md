# Symbol Index

Scope: high-value symbols for targeted source entry
Trust: Confirmed
Lifecycle: Fresh
Last verified: 2026-09-29
Evidence: symbol definitions in current source
Depends on: current imports/file layout
Importance: High

- `app.bot.main`: composition, initialization, workers, polling and shutdown.
- `app.config.Settings`: validated environment-backed configuration and paths.
- `app.services.access_service.UserContext`: role capability source of truth.
- `app.storage.user_database.UserDatabase.initialize`: database initialization/migration entrypoint.
- `app.storage.migrations.CURRENT_SCHEMA_VERSION`: current schema target (`12`).
- `app.storage.release_inspection.open_release_inspection`: logical read-only release connection.
- `app.storage.json_storage.JsonCatalogStorage`: validated atomic catalog persistence.
- `app.services.warehouse.BusinessDateService`: Kyiv cutoff/business period semantics.
- `app.services.warehouse.parse_quantity`: Decimal-to-scaled quantity boundary.
- `WarehouseOperationsDatabase.commit_batch`: atomic intake-to-operation commit.
- `WarehouseReminderDatabase.claim`: reminder ownership before Telegram send.
- `WarehouseAttendanceService`: personal attendance lifecycle and authorization.
- `AttendanceReportCoordinator`: one-shot XLSX generation token lifecycle.
- `source.import_bar_xls.CatalogImporter`: spreadsheet-to-catalog reconciliation preserving stable/manual data.

Names are routing aids, not API guarantees. Confirm definitions before edits.
