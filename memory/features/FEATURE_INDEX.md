# Feature Index

Scope: feature-to-source routing
Trust: Confirmed
Lifecycle: Fresh
Last verified: 2026-10-14
Evidence: `app/`, tests, README, commit `c2ce9fd2ae46509b8ed47aa6d44e1cc1b3ccf80f`
Depends on: current file layout
Importance: High

| Feature | Primary anchors | Supporting tests |
|---|---|---|
| Catalog/navigation/search | `app/handlers/catalog.py`, `app/services/catalog_service.py`, `app/services/search_service.py`, `app/storage/json_storage.py` | catalog/search/storage tests |
| Catalog administration | `app/handlers/admin/`, `app/services/admin_service.py`, admin keyboards | admin/FSM/photo tests |
| Roles/access | `app/services/access_service.py`, `app/models/user.py`, access middleware | `tests/test_access_service.py` |
| Warehouse intake/operations | `app/handlers/warehouse.py`, operations/resolution services and database | intake, resolution, operation, development tests |
| Closing/required rules | closing handler/service/database | closing and closing-UI tests |
| XLSX/report delivery | report/XLSX/delivery services and databases | report, XLSX, delivery tests |
| Reminders/schedules | reminder/schedule services and databases | reminder/schedule tests |
| Corrections | correction handlers/service/database | correction and release-UI tests |
| Attendance | attendance handler/service/database/XLSX coordinator | attendance safeguard/regression/migration tests |
| Release safety | `app/release_preflight.py`, `app/storage/release_inspection.py`, production docs | release inspection/preflight/readiness tests |
| Spreadsheet import | `source/import_bar_xls.py`, catalog JSON | imported-catalog/storage tests |
| [AI assistant pilot](AI_ASSISTANT.md) | `app/handlers/assistant.py`, `app/services/assistant/`, `app/storage/assistant_conversation_*`, strict assistant/tool models | assistant retrieval/context/provider/orchestrator/routing/tool/migration tests |
| [AI capability language coverage](AI_CAPABILITY_LANGUAGE.md) | authoritative `CAPABILITIES`, verified phrases and pending coverage | capability/controller and per-domain regression tests |

Use this table to localize; read exact code when changing behavior.
