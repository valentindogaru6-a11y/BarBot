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
from app.services.access_service import AccessDeniedError, AccessService
from app.services.assistant.retrieval import AssistantCatalogRetriever
from app.services.warehouse.attendance_service import WarehouseAttendanceService
from app.services.warehouse.operations_service import WarehouseOperationsService


class AssistantReadOnlyTools:
    """Allowlisted local reads; arguments never select SQL, tables or fields."""

    def __init__(
        self,
        catalog: Catalog,
        access: AccessService,
        *,
        attendance: WarehouseAttendanceService | None = None,
        operations: WarehouseOperationsService | None = None,
    ) -> None:
        self.retriever = AssistantCatalogRetriever(catalog)
        self.access = access
        self.attendance = attendance
        self.operations = operations

    def _require_pilot(self, actor_user_id: int) -> None:
        if self.access.get_context(actor_user_id).role is not Role.SUPER_ADMIN:
            raise AccessDeniedError("AI read-only tools are limited to super_admin")

    def search_tech_cards(self, actor_user_id: int, payload: SearchTechCardsInput) -> tuple[TechCardToolResult, ...]:
        self._require_pilot(actor_user_id)
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
        self._require_pilot(actor_user_id)
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
        self._require_pilot(actor_user_id)
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
        self._require_pilot(actor_user_id)
        if self.operations is None:
            raise RuntimeError("Shift tool is unavailable")
        row = self.operations.current_shift(actor_user_id=actor_user_id)
        return CurrentShiftSummaryResult(
            exists=row.get("id") is not None,
            location=str(row["source_location_name"]),
            business_date=str(row["business_date"]),
            status=str(row["status"]),
        )
