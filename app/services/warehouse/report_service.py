"""Authorized orchestration for draft and final warehouse XLSX reports."""

from __future__ import annotations

import threading
from datetime import UTC
from pathlib import Path
from typing import Any

from app.services.access_service import AccessService
from app.services.warehouse.business_date_service import (
    BusinessDateService,
    Clock,
    utc_rfc3339,
)
from app.services.warehouse.operations_service import WarehouseOperationsService
from app.services.warehouse.xlsx_report_service import (
    WarehouseReportFileConflict,
    WarehouseXlsxReportService,
)
from app.storage.warehouse_reports_database import (
    EmptyWarehouseReportError,
    WarehouseReportsDatabase,
    WarehouseReportsError,
)

_LOCKS_GUARD = threading.Lock()
_SHIFT_LOCKS: dict[int, threading.Lock] = {}


def _shift_lock(shift_id: int) -> threading.Lock:
    with _LOCKS_GUARD:
        return _SHIFT_LOCKS.setdefault(shift_id, threading.Lock())


class WarehouseReportService:
    def __init__(
        self,
        repository: WarehouseReportsDatabase,
        operations: WarehouseOperationsService,
        access: AccessService,
        reports_path: str | Path,
        *,
        clock: Clock | None = None,
        xlsx_service: WarehouseXlsxReportService | None = None,
    ) -> None:
        self.repository = repository
        self.operations = operations
        self.access = access
        self.xlsx = xlsx_service or WarehouseXlsxReportService(reports_path)
        self.clock = clock

    def _now(self) -> str:
        service = BusinessDateService(clock=self.clock)
        return utc_rfc3339(service.clock.now().astimezone(UTC))

    def _source_location_id(self) -> int:
        return int(self.operations.repository.runtime_settings()["source_location_id"])

    def _authorize_report(self, report: dict[str, Any], actor_user_id: int) -> None:
        self.access.require_warehouse_view(actor_user_id)
        if int(report["source_location_id"]) != self._source_location_id():
            raise WarehouseReportsError("Report not found")

    def _authorize_report_mutation(
        self,
        report: dict[str, Any],
        actor_user_id: int,
    ) -> None:
        self.access.require_warehouse_mutation(actor_user_id)
        self._authorize_report(report, actor_user_id)

    def build_final(
        self,
        shift_id: int,
        *,
        actor_user_id: int,
        allow_empty: bool = False,
    ) -> dict[str, Any]:
        self.access.require_warehouse_mutation(actor_user_id)
        shift = self.operations.repository.shift(shift_id)
        if shift is None or int(shift["source_location_id"]) != self._source_location_id():
            raise WarehouseReportsError("Shift not found")
        with _shift_lock(shift_id):
            report = self.repository.prepare_initial_report(
                shift_id,
                actor_user_id=actor_user_id,
                now=self._now(),
                allow_empty=allow_empty,
                authorize=lambda: self.access.require_warehouse_mutation(
                    actor_user_id
                ),
            )
            self._authorize_report(report, actor_user_id)
            if report["status"] == "ready":
                artifact = self.xlsx.verify_ready(report)
                report = self.repository.complete_report(
                    int(report["id"]),
                    relative_file_path=artifact.relative_file_path,
                    file_sha256=artifact.file_sha256,
                    file_size_bytes=artifact.file_size_bytes,
                    actor_user_id=actor_user_id,
                    now=self._now(),
                    authorize=lambda: self._authorize_report_mutation(
                        report, actor_user_id
                    ),
                )
                return {"report": report, "artifact": artifact}
            try:
                artifact = self.xlsx.build_final(report)
            except Exception as exc:
                try:
                    self.repository.mark_failed(
                        int(report["id"]),
                        failure_code=(
                            "file_conflict"
                            if isinstance(exc, WarehouseReportFileConflict)
                            else "xlsx_generation_failed"
                        ),
                    )
                except Exception:
                    pass
                raise
            report = self.repository.complete_report(
                int(report["id"]),
                relative_file_path=artifact.relative_file_path,
                file_sha256=artifact.file_sha256,
                file_size_bytes=artifact.file_size_bytes,
                actor_user_id=actor_user_id,
                now=self._now(),
                authorize=lambda: self._authorize_report_mutation(
                    report, actor_user_id
                ),
            )
            return {"report": report, "artifact": artifact}

    def draft(
        self,
        *,
        actor_user_id: int,
        directory: str | Path,
    ) -> dict[str, Any]:
        self.access.require_warehouse_view(actor_user_id)
        shift = self.operations.current_shift(actor_user_id=actor_user_id)
        if shift["status"] in {"closed", "correction_open"}:
            raise WarehouseReportsError("Current shift is already closed")
        if shift["id"] is None:
            lines: list[dict[str, Any]] = []
        else:
            lines = self.repository.live_aggregated_lines(
                int(shift["id"]),
                business_date=str(shift["business_date"]),
            )
        path = self.xlsx.build_draft(
            lines,
            business_date=str(shift["business_date"]),
            directory=directory,
        )
        return {
            "path": path,
            "business_date": str(shift["business_date"]),
            "is_empty": not lines,
            "writeoff_count": sum(
                line["sheet_type"] == "writeoff" for line in lines
            ),
            "transfer_count": sum(
                line["sheet_type"] == "transfer" for line in lines
            ),
        }

    def history(
        self,
        *,
        actor_user_id: int,
        page: int,
        page_size: int = 8,
    ) -> tuple[list[dict[str, Any]], int]:
        self.access.require_warehouse_view(actor_user_id)
        return self.repository.ready_history(
            self._source_location_id(),
            page=max(page, 0),
            page_size=page_size,
        )

    def report_card(
        self,
        report_id: int,
        *,
        actor_user_id: int,
    ) -> dict[str, Any]:
        report = self.repository.get_report(report_id)
        if report is None:
            raise WarehouseReportsError("Report not found")
        self._authorize_report(report, actor_user_id)
        if report["status"] != "ready":
            raise WarehouseReportsError("Report is not ready")
        return report

    def download(
        self,
        report_id: int,
        *,
        actor_user_id: int,
    ) -> dict[str, Any]:
        report = self.report_card(report_id, actor_user_id=actor_user_id)
        artifact = self.xlsx.verify_ready(report)
        return {"report": report, "artifact": artifact}


__all__ = [
    "EmptyWarehouseReportError",
    "WarehouseReportService",
    "WarehouseReportsError",
]
