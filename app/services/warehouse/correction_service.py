"""Admin-only orchestration for immutable closed-shift corrections."""

from __future__ import annotations

import asyncio
from datetime import UTC
from html import escape
from pathlib import Path
from typing import Any

from app.models.warehouse.nomenclature import BaseUnit
from app.models.warehouse.operations import WarehouseOperationType
from app.services.access_service import AccessService
from app.services.warehouse.business_date_service import BusinessDateService, Clock, utc_rfc3339
from app.services.warehouse.operations_service import WarehouseOperationsService
from app.services.warehouse.quantity_service import parse_quantity
from app.services.warehouse.report_service import _shift_lock
from app.services.warehouse.xlsx_report_service import WarehouseXlsxReportService
from app.storage.warehouse_correction_database import (
    WarehouseCorrectionConflict,
    WarehouseCorrectionDatabase,
    WarehouseCorrectionError,
)
from app.storage.warehouse_delivery_database import WarehouseDeliveryError


class WarehouseCorrectionService:
    def __init__(
        self,
        repository: WarehouseCorrectionDatabase,
        operations: WarehouseOperationsService,
        access: AccessService,
        xlsx: WarehouseXlsxReportService,
        *,
        clock: Clock | None = None,
    ) -> None:
        self.repository = repository
        self.operations = operations
        self.access = access
        self.xlsx = xlsx
        self.clock = clock

    def _now(self) -> str:
        return utc_rfc3339(
            BusinessDateService(clock=self.clock).clock.now().astimezone(UTC)
        )

    def _manage(self, actor_user_id: int) -> None:
        self.access.require_warehouse_correction_management(actor_user_id)

    def open_correction(
        self,
        shift_id: int,
        reason: str,
        *,
        expected_shift_revision: int,
        actor_user_id: int,
    ) -> dict[str, Any]:
        self._manage(actor_user_id)
        cleaned = reason.strip()
        if not 1 <= len(cleaned) <= 500:
            raise WarehouseCorrectionError("Correction reason must contain 1-500 characters")
        return self.repository.open(
            shift_id, cleaned, expected_shift_revision=expected_shift_revision,
            actor_user_id=actor_user_id, now=self._now(),
            authorize=lambda: self._manage(actor_user_id),
        )

    def session(self, session_id: int, *, actor_user_id: int) -> dict[str, Any]:
        self.access.require_warehouse_correction_view(actor_user_id)
        result = self.repository.session(session_id)
        if result is None:
            raise WarehouseCorrectionError("Correction session not found")
        return result

    def report_entry(self, report_id: int, *, actor_user_id: int) -> dict[str, Any]:
        context = self.access.require_warehouse_correction_view(actor_user_id)
        result = self.repository.report_entry(report_id)
        if result is None:
            raise WarehouseCorrectionError("Report not found")
        source = int(self.operations.repository.runtime_settings()["source_location_id"])
        if int(result["source_location_id"]) != source:
            raise WarehouseCorrectionError("Report not found")
        result["can_manage"] = context.can_manage_warehouse_corrections
        result["can_open"] = bool(
            context.can_manage_warehouse_corrections
            and result["report_status"] == "ready"
            and result["shift_status"] == "closed"
            and int(result["latest_report_id"]) == int(result["report_id"])
            and result["active_session_id"] is None
        )
        return result

    def dashboard(self, session_id: int, *, actor_user_id: int) -> dict[str, Any]:
        self.access.require_warehouse_correction_view(actor_user_id)
        result = self.repository.dashboard(session_id)
        if result is None:
            raise WarehouseCorrectionError("Correction session not found")
        source = int(self.operations.repository.runtime_settings()["source_location_id"])
        if int(result["source_location_id"]) != source:
            raise WarehouseCorrectionError("Correction session not found")
        result["can_manage"] = self.access.get_context(
            actor_user_id
        ).can_manage_warehouse_corrections
        return result

    def operation_page(
        self, session_id: int, *, page: int, actor_user_id: int
    ) -> tuple[list[dict[str, Any]], int]:
        self.dashboard(session_id, actor_user_id=actor_user_id)
        return self.repository.operation_page(session_id, page=max(page, 0))

    def operation(
        self, session_id: int, operation_id: int, *, actor_user_id: int
    ) -> dict[str, Any]:
        dashboard = self.dashboard(session_id, actor_user_id=actor_user_id)
        result = self.repository.operation(session_id, operation_id)
        if result is None or int(result["shift_id"]) != int(dashboard["shift_id"]):
            raise WarehouseCorrectionError("Operation not found")
        result["can_manage"] = bool(dashboard["can_manage"])
        return result

    def active_operation(
        self, operation_id: int, *, actor_user_id: int
    ) -> dict[str, Any]:
        self.access.require_warehouse_correction_view(actor_user_id)
        result = self.repository.active_operation(operation_id)
        if result is None:
            raise WarehouseCorrectionError("Operation not found")
        dashboard = self.dashboard(int(result["session_id"]), actor_user_id=actor_user_id)
        result["can_manage"] = bool(dashboard["can_manage"])
        return result

    def changes(self, session_id: int, *, actor_user_id: int) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        page = 0
        while True:
            rows, total = self.operation_page(
                session_id, page=page, actor_user_id=actor_user_id
            )
            result.extend(rows)
            if len(result) >= total:
                break
            page += 1
        return [row for row in result if row["change_state"] != "unchanged"]

    def finalize_preview(
        self, session_id: int, *, actor_user_id: int
    ) -> dict[str, Any]:
        self._manage(actor_user_id)
        dashboard = self.dashboard(session_id, actor_user_id=actor_user_id)
        if dashboard["status"] != "open" or dashboard["shift_status"] != "correction_open":
            raise WarehouseCorrectionError("Correction is not open")
        if not dashboard["has_mutations"]:
            raise WarehouseCorrectionError("Correction has no mutations")
        preview = self.repository.finalize_preview(session_id)
        if int(preview["latest_report_id"] or 0) != int(preview["base_report_id"]):
            raise WarehouseCorrectionError("Correction base report changed")
        preview.update(
            source_location_name=dashboard["source_location_name"],
            change_counts=dashboard["change_counts"],
            changes=self.changes(session_id, actor_user_id=actor_user_id),
            can_manage=True,
        )
        return preview

    def result(
        self, session_id: int, *, actor_user_id: int
    ) -> dict[str, Any]:
        dashboard = self.dashboard(session_id, actor_user_id=actor_user_id)
        if dashboard["status"] != "completed" or dashboard["result_report_id"] is None:
            raise WarehouseCorrectionError("Correction report is not ready")
        report = self.repository.report(int(dashboard["result_report_id"]))
        if (
            report is None
            or report["status"] != "ready"
            or int(report.get("correction_session_id") or 0) != session_id
        ):
            raise WarehouseCorrectionError("Correction result is inconsistent")
        return {
            "session": dashboard,
            "report": report,
            "file_name": Path(str(report["relative_file_path"])).name,
            "can_manage": bool(dashboard["can_manage"]),
        }

    def recovery_preview(
        self, session_id: int, *, actor_user_id: int
    ) -> dict[str, Any]:
        self._manage(actor_user_id)
        dashboard = self.dashboard(session_id, actor_user_id=actor_user_id)
        if dashboard["status"] not in {"building", "failed", "completed"}:
            raise WarehouseCorrectionError("Correction is not recoverable")
        return dashboard

    def download_result(
        self, session_id: int, *, actor_user_id: int
    ) -> dict[str, Any]:
        result = self.result(session_id, actor_user_id=actor_user_id)
        result["artifact"] = self.xlsx.verify_ready(result["report"])
        return result

    def version_history(
        self, session_id: int, *, actor_user_id: int
    ) -> dict[str, Any]:
        dashboard = self.dashboard(session_id, actor_user_id=actor_user_id)
        return {
            "session": dashboard,
            "versions": self.repository.version_history(int(dashboard["shift_id"])),
        }

    def history(self, shift_id: int, *, actor_user_id: int) -> list[dict[str, Any]]:
        self.access.require_warehouse_correction_view(actor_user_id)
        return self.repository.history(shift_id)

    @staticmethod
    def _text(value: str | None, field: str) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if len(cleaned) > 500:
            raise WarehouseCorrectionError(f"{field} is too long")
        return cleaned or None

    def create_operation(
        self,
        session_id: int,
        *,
        operation_type: WarehouseOperationType | str,
        product_id: int,
        quantity_text: str,
        unit_text: str | None = None,
        destination_location_id: int | None = None,
        reason_text: str | None = None,
        comment_text: str | None = None,
        development_title: str | None = None,
        expected_session_revision: int | None = None,
        actor_user_id: int,
    ) -> dict[str, Any]:
        self._manage(actor_user_id)
        kind = WarehouseOperationType(operation_type)
        product = self.operations.repository.product(product_id)
        if product is None:
            raise WarehouseCorrectionError("Active product not found")
        required = "product_writeoff" if kind is WarehouseOperationType.WRITEOFF else kind.value
        if required not in product.operation_types:
            raise WarehouseCorrectionError("Product does not support operation")
        quantity = parse_quantity(quantity_text, unit_text, product.base_unit)
        title = development_title.strip() if development_title else None
        raw = f"{kind.value}: {product.official_name} {quantity.input_quantity_text}"
        if quantity.input_unit_text:
            raw += f" {quantity.input_unit_text}"
        return self.repository.create_operation(
            session_id,
            {
                "operation_type": kind.value,
                "product_id": product.id,
                "quantity_scaled": quantity.quantity_scaled,
                "base_unit": quantity.base_unit.value,
                "input_quantity_text": quantity.input_quantity_text,
                "input_unit_text": quantity.input_unit_text,
                "destination_location_id": destination_location_id,
                "reason_text": self._text(reason_text, "Reason"),
                "comment_text": self._text(comment_text, "Comment"),
                "development_title": title,
                "raw_text": raw,
            },
            actor_user_id=actor_user_id,
            now=self._now(),
            authorize=lambda: self._manage(actor_user_id),
            expected_session_revision=expected_session_revision,
        )

    def update_product_and_quantity(
        self,
        session_id: int,
        operation_id: int,
        product_id: int,
        quantity_text: str,
        unit_text: str | None,
        *,
        expected_revision: int,
        expected_session_revision: int | None = None,
        actor_user_id: int,
    ) -> dict[str, Any]:
        self._manage(actor_user_id)
        operation = self.operations.repository.get_operation(operation_id)
        product = self.operations.repository.product(product_id)
        if operation is None or product is None:
            raise WarehouseCorrectionError("Operation or product not found")
        required = "product_writeoff" if operation["operation_type"] == "writeoff" else str(operation["operation_type"])
        if required not in product.operation_types:
            raise WarehouseCorrectionError("Product does not support operation")
        quantity = parse_quantity(quantity_text, unit_text, product.base_unit)
        return self.repository.update_operation(
            session_id, operation_id,
            {
                "product_id": product.id,
                "product_name_snapshot": product.official_name,
                "quantity_scaled": quantity.quantity_scaled,
                "base_unit": quantity.base_unit.value,
                "input_quantity_text": quantity.input_quantity_text,
                "input_unit_text": quantity.input_unit_text,
            },
            expected_revision=expected_revision,
            actor_user_id=actor_user_id,
            now=self._now(),
            authorize=lambda: self._manage(actor_user_id),
            expected_session_revision=expected_session_revision,
        )

    def update_quantity(
        self, session_id: int, operation_id: int, quantity_text: str,
        unit_text: str | None, *, expected_revision: int,
        expected_session_revision: int | None = None, actor_user_id: int,
    ) -> dict[str, Any]:
        self._manage(actor_user_id)
        operation = self.operations.repository.get_operation(operation_id)
        if operation is None:
            raise WarehouseCorrectionError("Operation not found")
        quantity = parse_quantity(
            quantity_text, unit_text, BaseUnit(str(operation["base_unit"]))
        )
        return self.repository.update_operation(
            session_id, operation_id,
            {
                "quantity_scaled": quantity.quantity_scaled,
                "input_quantity_text": quantity.input_quantity_text,
                "input_unit_text": quantity.input_unit_text,
            },
            expected_revision=expected_revision,
            actor_user_id=actor_user_id,
            now=self._now(),
            authorize=lambda: self._manage(actor_user_id),
            expected_session_revision=expected_session_revision,
        )

    def update_note(
        self, session_id: int, operation_id: int, *,
        reason_text: str | None, comment_text: str | None,
        expected_revision: int, expected_session_revision: int | None = None,
        actor_user_id: int,
    ) -> dict[str, Any]:
        self._manage(actor_user_id)
        return self.repository.update_operation(
            session_id, operation_id,
            {"reason_text": self._text(reason_text, "Reason"),
             "comment_text": self._text(comment_text, "Comment")},
            expected_revision=expected_revision,
            actor_user_id=actor_user_id,
            now=self._now(),
            authorize=lambda: self._manage(actor_user_id),
            expected_session_revision=expected_session_revision,
        )

    def update_destination(
        self, session_id: int, operation_id: int, destination_location_id: int,
        *, expected_revision: int, expected_session_revision: int | None = None,
        actor_user_id: int,
    ) -> dict[str, Any]:
        self._manage(actor_user_id)
        return self.repository.update_operation(
            session_id, operation_id,
            {"destination_location_id": destination_location_id},
            expected_revision=expected_revision,
            actor_user_id=actor_user_id,
            now=self._now(),
            authorize=lambda: self._manage(actor_user_id),
            expected_session_revision=expected_session_revision,
        )

    def rename_development(
        self, session_id: int, operation_id: int, title: str, *,
        expected_revision: int, expected_session_revision: int | None = None,
        actor_user_id: int,
    ) -> dict[str, Any]:
        self._manage(actor_user_id)
        cleaned = title.strip()
        if not 1 <= len(cleaned) <= 200:
            raise WarehouseCorrectionError("Development title is invalid")
        return self.repository.rename_development(
            session_id, operation_id, cleaned,
            expected_revision=expected_revision,
            actor_user_id=actor_user_id,
            now=self._now(),
            authorize=lambda: self._manage(actor_user_id),
            expected_session_revision=expected_session_revision,
        )

    def delete_operation(
        self, session_id: int, operation_id: int, *, expected_revision: int,
        reason: str, actor_user_id: int,
        expected_session_revision: int | None = None,
    ) -> dict[str, Any]:
        self._manage(actor_user_id)
        cleaned = reason.strip()
        if not 1 <= len(cleaned) <= 500:
            raise WarehouseCorrectionError("Deletion reason is required")
        return self.repository.set_deleted(
            session_id, operation_id, True,
            expected_revision=expected_revision, actor_user_id=actor_user_id,
            reason=cleaned, now=self._now(),
            authorize=lambda: self._manage(actor_user_id),
            expected_session_revision=expected_session_revision,
        )

    def restore_operation(
        self, session_id: int, operation_id: int, *, expected_revision: int,
        reason: str, actor_user_id: int,
        expected_session_revision: int | None = None,
    ) -> dict[str, Any]:
        self._manage(actor_user_id)
        cleaned = reason.strip()
        if not 1 <= len(cleaned) <= 500:
            raise WarehouseCorrectionError("Restore reason is required")
        return self.repository.set_deleted(
            session_id, operation_id, False,
            expected_revision=expected_revision, actor_user_id=actor_user_id,
            reason=cleaned, now=self._now(),
            authorize=lambda: self._manage(actor_user_id),
            expected_session_revision=expected_session_revision,
        )

    def cancel_correction(
        self, session_id: int, *, expected_revision: int, actor_user_id: int,
    ) -> dict[str, Any]:
        self._manage(actor_user_id)
        return self.repository.cancel(
            session_id, expected_revision=expected_revision,
            actor_user_id=actor_user_id, now=self._now(),
            authorize=lambda: self._manage(actor_user_id),
        )

    def finalize_correction(
        self, session_id: int, *, expected_revision: int, actor_user_id: int,
        require_mutations: bool = False,
    ) -> dict[str, Any]:
        self._manage(actor_user_id)
        current = self.repository.session(session_id)
        if current is None:
            raise WarehouseCorrectionError("Correction session not found")
        shift_id = int(current["shift_id"])
        with _shift_lock(shift_id):
            prepared = self.repository.begin_finalize(
                session_id, expected_revision=expected_revision,
                actor_user_id=actor_user_id, now=self._now(),
                authorize=lambda: self._manage(actor_user_id),
                require_mutations=require_mutations,
            )
            if not prepared["ready"]:
                return prepared
            report = prepared["report"]
            assert report is not None
            try:
                artifact = (
                    self.xlsx.verify_ready(report)
                    if report["status"] == "ready"
                    else self.xlsx.build_final(report)
                )
            except Exception:
                self.repository.mark_build_failed(
                    session_id, actor_user_id=actor_user_id, now=self._now()
                )
                raise
            completed = self.repository.complete(
                session_id, int(report["id"]),
                relative_file_path=artifact.relative_file_path,
                file_sha256=artifact.file_sha256,
                file_size_bytes=artifact.file_size_bytes,
                actor_user_id=actor_user_id,
                now=self._now(),
                authorize=lambda: self._manage(actor_user_id),
            )
            return {
                "ready": True, "session": completed,
                "report": self.repository.report(int(report["id"])),
                "artifact": artifact, "missing": [],
            }

    def release_correction(
        self, session_id: int, *, expected_revision: int, actor_user_id: int,
    ) -> dict[str, Any]:
        return self.finalize_correction(
            session_id,
            expected_revision=expected_revision,
            actor_user_id=actor_user_id,
            require_mutations=True,
        )

    def recover_correction(
        self, session_id: int, *, actor_user_id: int,
    ) -> dict[str, Any]:
        self._manage(actor_user_id)
        session = self.repository.session(session_id)
        if session is None or session["status"] not in {"building", "failed", "completed"}:
            raise WarehouseCorrectionError("Correction is not recoverable")
        return self.finalize_correction(
            session_id, expected_revision=int(session["revision"]),
            actor_user_id=actor_user_id,
        )

    def delivery_preview(
        self, session_id: int, *, actor_user_id: int,
    ) -> dict[str, Any]:
        self._manage(actor_user_id)
        return self.repository.delivery_preview(session_id)

    def validate_delivery_selection(
        self, session_id: int, recipient_ids: tuple[int, ...], *, actor_user_id: int,
    ) -> tuple[dict[str, Any], ...]:
        self._manage(actor_user_id)
        self.result(session_id, actor_user_id=actor_user_id)
        return self.repository.validate_delivery_recipients(session_id, recipient_ids)

    async def deliver_correction(
        self, session_id: int, delivery_service, *, actor_user_id: int,
        retry_failed: bool = False,
        recipient_ids: tuple[int, ...] | None = None,
    ) -> dict[str, Any]:
        preview = self.delivery_preview(session_id, actor_user_id=actor_user_id)
        selected = (
            preview["recipient_ids"]
            if recipient_ids is None
            else tuple(
                int(row["id"])
                for row in self.validate_delivery_selection(
                    session_id, recipient_ids, actor_user_id=actor_user_id
                )
            )
        )
        caption = (
            f"Исправленный отчёт\nДата: {preview['business_date']}\n"
            f"Версия: v{preview['version']:03d}\n"
            f"Причина: {escape(preview['reason'])}"
        )
        try:
            result = await delivery_service.deliver(
                preview["report_id"], actor_user_id=actor_user_id,
                retry_failed=retry_failed,
                recipient_ids=selected, caption=caption,
                require_all_recipients_active=True,
            )
        except WarehouseDeliveryError as exc:
            if "selection changed" in str(exc).casefold():
                raise WarehouseCorrectionConflict(
                    "Recipient selection changed"
                ) from exc
            raise
        result["skipped_inactive"] = 0
        return result

    def retry_preview(
        self, session_id: int, *, actor_user_id: int
    ) -> dict[str, Any]:
        self._manage(actor_user_id)
        preview = self.delivery_preview(session_id, actor_user_id=actor_user_id)
        failed = self.repository.failed_delivery_recipient_ids(session_id)
        active = (
            self.repository.validate_delivery_recipients(session_id, failed["active"])
            if failed["active"] else ()
        )
        return {
            **preview,
            "recipient_ids": failed["active"],
            "recipients": active,
            "skipped_inactive": len(failed["inactive"]),
        }

    async def retry_correction_delivery(
        self, session_id: int, delivery_service, *, actor_user_id: int,
    ) -> dict[str, Any]:
        preview = self.retry_preview(session_id, actor_user_id=actor_user_id)
        if not preview["recipient_ids"]:
            history = await asyncio.to_thread(
                delivery_service.history,
                preview["report_id"], actor_user_id=actor_user_id,
            )
            return {
                "report": history["report"], "deliveries": history["details"],
                "delivered": history["delivered"], "failed": history["failed"],
                "pending": history["pending"],
                "skipped_inactive": preview["skipped_inactive"],
            }
        result = await self.deliver_correction(
            session_id, delivery_service, actor_user_id=actor_user_id,
            retry_failed=True, recipient_ids=preview["recipient_ids"],
        )
        result["skipped_inactive"] = preview["skipped_inactive"]
        return result


__all__ = [
    "WarehouseCorrectionError",
    "WarehouseCorrectionService",
]
