"""Server-authorized, deterministic and read-only BarBot assistant tools."""

from __future__ import annotations

from app.models.assistant_tools import (
    CurrentShiftSummaryInput,
    CurrentShiftSummaryResult,
    GetTechCardInput,
    OpenAttendanceSessionResult,
    OpenAttendanceSessionsInput,
    SearchTechCardsInput,
    TechCardToolResult,
)
from app.models.catalog import Catalog
from app.models.user import Role
from app.models.assistant_capabilities import (
    CapabilityResult,
    EntityCapabilityInput,
    NoCapabilityInput,
    QueryCapabilityInput,
)
from app.services.access_service import AccessDeniedError, AccessService
from app.services.assistant.capabilities import CapabilityRegistry
from app.services.assistant.retrieval import AssistantCatalogRetriever
from app.services.warehouse.attendance_service import WarehouseAttendanceService
from app.services.warehouse.operations_service import WarehouseOperationsService
from app.services.user_service import UserService
from app.services.warehouse.closing_service import WarehouseClosingService
from app.services.warehouse.recipient_service import WarehouseRecipientService
from app.services.warehouse.reminder_service import WarehouseReminderSettingsService
from app.services.warehouse.report_service import WarehouseReportService
from app.services.warehouse.schedule_service import WarehouseScheduleService


class AssistantReadOnlyTools:
    """Allowlisted local reads; arguments never select SQL, tables or fields."""

    def __init__(
        self,
        catalog: Catalog,
        access: AccessService,
        *,
        attendance: WarehouseAttendanceService | None = None,
        operations: WarehouseOperationsService | None = None,
        users: UserService | None = None,
        closing: WarehouseClosingService | None = None,
        reports: WarehouseReportService | None = None,
        recipients: WarehouseRecipientService | None = None,
        reminders: WarehouseReminderSettingsService | None = None,
        schedules: WarehouseScheduleService | None = None,
        capabilities: CapabilityRegistry | None = None,
        flags: dict[str, bool] | None = None,
    ) -> None:
        self.retriever = AssistantCatalogRetriever(catalog)
        self.access = access
        self.attendance = attendance
        self.operations = operations
        self.users = users
        self.closing = closing
        self.reports = reports
        self.recipients = recipients
        self.reminders = reminders
        self.schedules = schedules
        self.capabilities = capabilities or CapabilityRegistry()
        self.flags = flags or {}

    def _require_catalog_view(self, actor_user_id: int) -> None:
        if not self.access.get_context(actor_user_id).can_view_catalog:
            raise AccessDeniedError("Недостаточно прав для просмотра каталога")

    def _require_warehouse_view(self, actor_user_id: int) -> None:
        if not self.access.get_context(actor_user_id).can_view_warehouse:
            raise AccessDeniedError("Недостаточно прав для просмотра склада")

    def _require_attendance_management(self, actor_user_id: int) -> None:
        if not self.access.get_context(actor_user_id).can_manage_warehouse_attendance:
            raise AccessDeniedError("Недостаточно прав для просмотра персональных смен")

    def search_tech_cards(self, actor_user_id: int, payload: SearchTechCardsInput) -> tuple[TechCardToolResult, ...]:
        self._require_catalog_view(actor_user_id)
        retrieval = self.retriever.retrieve(payload.query, limit=10)
        results = retrieval.exact or tuple(candidate.result for candidate in retrieval.candidates)
        return tuple(
            TechCardToolResult(
                card_id=self.retriever.key_for(result), name=result.item.name,
                category=result.category.name,
                subcategory=result.subcategory.name if result.subcategory else None,
            )
            for result in results
        )

    def get_tech_card(self, actor_user_id: int, payload: GetTechCardInput) -> TechCardToolResult | None:
        self._require_catalog_view(actor_user_id)
        result = self.retriever.by_key(payload.card_id)
        if result is None:
            return None
        return TechCardToolResult(
            card_id=payload.card_id, name=result.item.name,
            category=result.category.name,
            subcategory=result.subcategory.name if result.subcategory else None,
        )

    def list_open_attendance_sessions(self, actor_user_id: int, payload: OpenAttendanceSessionsInput | None = None) -> tuple[OpenAttendanceSessionResult, ...]:
        del payload
        self._require_attendance_management(actor_user_id)
        if self.attendance is None:
            raise RuntimeError("Attendance tool is unavailable")
        rows = self.attendance.open_sessions(actor_user_id)
        return tuple(
            OpenAttendanceSessionResult(
                employee=str(row.get("display_name_snapshot") or "Сотрудник"),
                username=str(row["username_snapshot"]) if row.get("username_snapshot") else None,
                location=str(row["source_location_name"]),
                business_date=str(row["business_date"]),
                opened_at_utc=str(row["opened_at_utc"]),
                duration_seconds=self.attendance.duration_seconds(row),
                status=str(row["status"]),
            )
            for row in rows
        )

    def get_current_shift_summary(self, actor_user_id: int, payload: CurrentShiftSummaryInput | None = None) -> CurrentShiftSummaryResult:
        del payload
        self._require_warehouse_view(actor_user_id)
        if self.operations is None:
            raise RuntimeError("Shift tool is unavailable")
        row = self.operations.current_shift(actor_user_id=actor_user_id)
        return CurrentShiftSummaryResult(
            exists=row.get("id") is not None,
            location=str(row["source_location_name"]),
            business_date=str(row["business_date"]),
            status=str(row["status"]),
        )

    def list_registered_users(self, actor_user_id: int, payload: NoCapabilityInput | None = None) -> CapabilityResult:
        del payload
        context = self.access.get_context(actor_user_id)
        if not context.can_manage_users or self.users is None:
            raise AccessDeniedError("Недостаточно прав для списка сотрудников")
        rows, _, _ = self.users.list_users(page_size=100)
        return CapabilityResult(
            kind="users",
            message=f"Зарегистрировано сотрудников: {len(rows)}.",
            data={"users": [{"name": row.display_name, "role": row.role.value, "active": row.is_active} for row in rows]},
        )

    def list_registered_bartenders(self, actor_user_id: int, payload: NoCapabilityInput | None = None) -> CapabilityResult:
        del payload
        context = self.access.get_context(actor_user_id)
        if not context.can_manage_users or self.users is None:
            raise AccessDeniedError("Недостаточно прав для списка барменов")
        rows, _, _ = self.users.list_users(role=Role.BARTENDER, page_size=100)
        return CapabilityResult(
            kind="bartenders",
            message=f"Зарегистрировано барменов: {len(rows)}.",
            data={"users": [{"name": row.display_name, "active": row.is_active} for row in rows]},
        )

    def get_user_summary(self, actor_user_id: int, payload: QueryCapabilityInput) -> CapabilityResult:
        context = self.access.get_context(actor_user_id)
        if not context.can_manage_users or self.users is None:
            raise AccessDeniedError("Недостаточно прав для просмотра сотрудника")
        rows = self.users.search_users(payload.query)[:10]
        return CapabilityResult(
            kind="user_candidates",
            message=f"Найдено сотрудников: {len(rows)}.",
            data={"users": [{"name": row.display_name, "role": row.role.value, "active": row.is_active} for row in rows]},
        )

    def get_attendance_session(self, actor_user_id: int, payload: EntityCapabilityInput) -> CapabilityResult:
        if self.attendance is None:
            raise RuntimeError("Attendance tool is unavailable")
        rows = self.attendance.open_sessions(actor_user_id)
        row = next((value for value in rows if str(value["id"]) == payload.entity_ref), None)
        if row is None:
            return CapabilityResult(kind="not_found", message="Открытая смена не найдена.")
        return CapabilityResult(
            kind="attendance_session",
            message="Открытая смена найдена.",
            data={
                "employee": str(row.get("display_name_snapshot") or "Сотрудник"),
                "location": str(row["source_location_name"]),
                "business_date": str(row["business_date"]),
                "status": str(row["status"]),
            },
        )

    def list_shift_operations(self, actor_user_id: int, payload: NoCapabilityInput | None = None) -> CapabilityResult:
        del payload
        if self.operations is None:
            raise RuntimeError("Operations tool is unavailable")
        shift = self.operations.current_shift(actor_user_id=actor_user_id)
        rows = [] if shift["id"] is None else self.operations.list_operations(int(shift["id"]), actor_user_id=actor_user_id)
        return CapabilityResult(
            kind="shift_operations",
            message=f"Записей в текущей смене: {len(rows)}.",
            data={"operations": [{"type": str(row["operation_type"]), "product": str(row.get("product_name_snapshot") or row.get("development_title") or "Позиция"), "quantity": str(row.get("input_quantity_text") or "")} for row in rows[:50]]},
        )

    def get_required_writeoff_status(self, actor_user_id: int, payload: NoCapabilityInput | None = None) -> CapabilityResult:
        del payload
        if self.closing is None:
            raise RuntimeError("Closing tool is unavailable")
        overview = self.closing.overview(actor_user_id=actor_user_id)
        return CapabilityResult(
            kind="required_writeoffs",
            message="Статус обязательных списаний получен.",
            data={"total": int(overview["requirements_total"]), "resolved": int(overview["requirements_resolved"])},
        )

    def get_report_status(self, actor_user_id: int, payload: EntityCapabilityInput) -> CapabilityResult:
        if self.reports is None:
            raise RuntimeError("Reports tool is unavailable")
        row = self.reports.report_card(int(payload.entity_ref), actor_user_id=actor_user_id)
        return CapabilityResult(kind="report", message="Отчёт готов.", data={"business_date": str(row["business_date"]), "version": int(row["version"]), "status": str(row["status"])})

    def list_report_history(self, actor_user_id: int, payload: NoCapabilityInput | None = None) -> CapabilityResult:
        del payload
        if self.reports is None:
            raise RuntimeError("Reports tool is unavailable")
        rows, total = self.reports.history(actor_user_id=actor_user_id, page=0)
        return CapabilityResult(kind="reports", message=f"Готовых отчётов: {total}.", data={"reports": [{"business_date": str(row["business_date"]), "version": int(row["version"]), "status": str(row["status"])} for row in rows]})

    def list_report_recipients(self, actor_user_id: int, payload: NoCapabilityInput | None = None) -> CapabilityResult:
        del payload
        if self.recipients is None:
            raise RuntimeError("Recipients tool is unavailable")
        rows = self.recipients.list_recipients(actor_user_id=actor_user_id)
        return CapabilityResult(kind="recipients", message=f"Получателей: {len(rows)}.", data={"recipients": [{"title": str(row["title"]), "active": bool(row["is_active"])} for row in rows]})

    def get_reminder_settings(self, actor_user_id: int, payload: NoCapabilityInput | None = None) -> CapabilityResult:
        del payload
        if self.reminders is None or self.operations is None:
            raise RuntimeError("Reminder tool is unavailable")
        shift = self.operations.current_shift(actor_user_id=actor_user_id)
        row = self.reminders.get(int(shift["source_location_id"]), actor_user_id)
        return CapabilityResult(kind="reminders", message="Настройки напоминаний получены.", data={"enabled": bool(row["enabled"]), "first_minutes": int(row["first_reminder_minutes_before_close"]), "repeat_minutes": int(row["repeat_interval_minutes"]), "maximum": int(row["max_reminders"])})

    def list_schedules(self, actor_user_id: int, payload: NoCapabilityInput | None = None) -> CapabilityResult:
        del payload
        if self.schedules is None:
            raise RuntimeError("Schedules tool is unavailable")
        rows = self.schedules.list(actor_user_id)
        return CapabilityResult(kind="schedules", message=f"Расписаний: {len(rows)}.", data={"schedules": [{"title": str(row.get("title") or "Расписание"), "active": bool(row["is_active"]), "type": str(row["schedule_type"])} for row in rows[:50]]})

    def get_capabilities(self, actor_user_id: int, payload: QueryCapabilityInput) -> CapabilityResult:
        role = self.access.get_context(actor_user_id).role
        groups = self.capabilities.describe(role=role, flags=self.flags)
        return CapabilityResult(kind="capabilities", message="Доступные возможности получены.", data={"groups": {category: [item.title for item in values] for category, values in groups.items()}})
