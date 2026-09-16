"""Admin-only orchestration for immutable closed-shift corrections."""

from __future__ import annotations

from datetime import UTC
from html import escape
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
    WarehouseCorrectionDatabase,
    WarehouseCorrectionError,
)


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
        self.access.require_warehouse_view(actor_user_id)
        result = self.repository.session(session_id)
        if result is None:
            raise WarehouseCorrectionError("Correction session not found")
        return result

    def history(self, shift_id: int, *, actor_user_id: int) -> list[dict[str, Any]]:
        self.access.require_warehouse_view(actor_user_id)
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
        )

    def update_quantity(
        self, session_id: int, operation_id: int, quantity_text: str,
        unit_text: str | None, *, expected_revision: int, actor_user_id: int,
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
        )

    def update_note(
        self, session_id: int, operation_id: int, *,
        reason_text: str | None, comment_text: str | None,
        expected_revision: int, actor_user_id: int,
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
        )

    def update_destination(
        self, session_id: int, operation_id: int, destination_location_id: int,
        *, expected_revision: int, actor_user_id: int,
    ) -> dict[str, Any]:
        self._manage(actor_user_id)
        return self.repository.update_operation(
            session_id, operation_id,
            {"destination_location_id": destination_location_id},
            expected_revision=expected_revision,
            actor_user_id=actor_user_id,
            now=self._now(),
            authorize=lambda: self._manage(actor_user_id),
        )

    def rename_development(
        self, session_id: int, operation_id: int, title: str, *,
        expected_revision: int, actor_user_id: int,
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
        )

    def delete_operation(
        self, session_id: int, operation_id: int, *, expected_revision: int,
        reason: str, actor_user_id: int,
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
        )

    def restore_operation(
        self, session_id: int, operation_id: int, *, expected_revision: int,
        reason: str, actor_user_id: int,
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

    async def deliver_correction(
        self, session_id: int, delivery_service, *, actor_user_id: int,
        retry_failed: bool = False,
    ) -> dict[str, Any]:
        preview = self.delivery_preview(session_id, actor_user_id=actor_user_id)
        caption = (
            f"Исправленный отчёт\nВерсия: v{preview['version']:03d}\n"
            f"Причина: {escape(preview['reason'])}"
        )
        return await delivery_service.deliver(
            preview["report_id"], actor_user_id=actor_user_id,
            retry_failed=retry_failed,
            recipient_ids=preview["recipient_ids"], caption=caption,
        )


__all__ = [
    "WarehouseCorrectionError",
    "WarehouseCorrectionService",
]
