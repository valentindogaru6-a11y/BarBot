"""Authorized warehouse intake and operation workflow."""

from __future__ import annotations

from datetime import UTC
from typing import Any

from app.models.warehouse.nomenclature import BaseUnit
from app.models.warehouse.operations import WarehouseOperationType
from app.services.access_service import AccessService
from app.services.warehouse.business_date_service import (
    BusinessDateService,
    Clock,
    utc_rfc3339,
)
from app.services.warehouse.intake_parser import (
    IntakeParseError,
    ParsedIntakeLine,
    parse_intake_message,
)
from app.services.warehouse.quantity_service import QuantityError, parse_quantity
from app.services.warehouse.resolution_service import (
    Resolution,
    ProductOption,
    resolve_destination,
    resolve_product,
)
from app.storage.warehouse_operations_database import (
    WarehouseOperationsDatabase,
    WarehouseOperationsError,
)


class WarehouseIntakeError(RuntimeError):
    pass


class WarehouseOperationsService:
    def __init__(
        self,
        repository: WarehouseOperationsDatabase,
        access: AccessService,
        *,
        clock: Clock | None = None,
    ) -> None:
        self.repository = repository
        self.access = access
        self.clock = clock

    def _now(self) -> str:
        service = BusinessDateService(clock=self.clock)
        return utc_rfc3339(service.clock.now())

    def is_configured(self) -> bool:
        return self.repository.active_product_count() > 0

    def current_shift(self, *, actor_user_id: int) -> dict[str, Any]:
        self.access.require_warehouse_view(actor_user_id)
        settings = self.repository.runtime_settings()
        period = BusinessDateService(
            timezone=str(settings["timezone"]),
            cutoff=str(settings["cutoff_local_time"]),
            clock=self.clock,
        ).current_period()
        stored = self.repository.shift_for_date(
            int(settings["source_location_id"]), period.business_date.isoformat()
        )
        return stored or {
            "id": None,
            "source_location_id": int(settings["source_location_id"]),
            "source_location_name": str(settings["source_location_name"]),
            "business_date": period.business_date.isoformat(),
            "status": "open",
        }

    def pending_batch(self, *, actor_user_id: int) -> dict[str, Any] | None:
        self.access.require_warehouse_mutation(actor_user_id)
        settings = self.repository.runtime_settings()
        period = BusinessDateService(
            timezone=str(settings["timezone"]),
            cutoff=str(settings["cutoff_local_time"]),
            clock=self.clock,
        ).current_period()
        return self.repository.active_batch_for_actor(
            actor_user_id,
            int(settings["source_location_id"]),
            period.business_date.isoformat(),
        )

    def owned_batch(
        self, batch_id: int, *, actor_user_id: int
    ) -> dict[str, Any] | None:
        self.access.require_warehouse_mutation(actor_user_id)
        batch = self.repository.get_batch(batch_id)
        if batch is not None:
            self._require_batch_owner(batch, actor_user_id)
            if batch["status"] not in {"ready", "needs_resolution"}:
                return None
        return batch

    def shift_by_id(
        self, shift_id: int, *, actor_user_id: int
    ) -> dict[str, Any] | None:
        self.access.require_warehouse_view(actor_user_id)
        current = self.current_shift(actor_user_id=actor_user_id)
        if current["id"] != shift_id:
            return None
        return self.repository.shift(shift_id)

    @staticmethod
    def _require_batch_owner(batch: dict[str, Any], actor_user_id: int) -> None:
        if int(batch["actor_user_id"]) != actor_user_id:
            raise WarehouseOperationsError("Batch belongs to another user")

    def _line_payload(
        self,
        line: ParsedIntakeLine,
        operation_type: WarehouseOperationType,
        products: tuple[ProductOption, ...],
        *,
        source_location_id: int,
    ) -> dict[str, Any]:
        product_result = resolve_product(line.product_text, operation_type, products)
        payload: dict[str, Any] = {
            "line_number": line.line_number,
            "raw_line": line.raw_line,
            "status": "ambiguous" if product_result.candidates else "invalid",
            "product_text": line.product_text,
            "product_id": product_result.exact_id,
            "input_quantity_text": line.quantity_text,
            "input_unit_text": line.unit_text,
            "quantity_scaled": None,
            "base_unit": None,
            "reason_text": line.reason_text,
            "comment_text": line.comment_text,
            "destination_text": line.destination_text,
            "destination_location_id": None,
            "error_code": product_result.error_code,
            "candidates": product_result.candidates,
        }
        if product_result.exact_id is None:
            return payload
        product = next(item for item in products if item.id == product_result.exact_id)
        try:
            quantity = parse_quantity(line.quantity_text, line.unit_text, product.base_unit)
        except QuantityError as exc:
            payload["status"] = "invalid"
            payload["error_code"] = exc.code
            return payload
        payload.update(
            quantity_scaled=quantity.quantity_scaled,
            base_unit=quantity.base_unit.value,
            error_code=None,
        )
        if operation_type is WarehouseOperationType.TRANSFER:
            destination = resolve_destination(
                line.destination_text or "",
                self.repository.locations(),
                source_location_id=source_location_id,
            )
            if destination.exact_id is None:
                payload["status"] = "ambiguous" if destination.candidates else "invalid"
                payload["error_code"] = destination.error_code
                return payload
            payload["destination_location_id"] = destination.exact_id
        payload["status"] = "resolved"
        return payload

    def _batch_lines(
        self,
        raw_text: str,
        products: tuple[ProductOption, ...],
        *,
        source_location_id: int,
    ) -> tuple[WarehouseOperationType, list[dict[str, Any]]]:
        """Parse independent lines while preserving correctable line failures."""

        try:
            parsed = parse_intake_message(raw_text)
        except IntakeParseError as exc:
            if exc.line_number is None:
                raise WarehouseIntakeError(f"{exc.code}: {exc}") from exc

            source_lines = raw_text.splitlines()
            first_index = next(
                index for index, value in enumerate(source_lines) if value.strip()
            )
            header = source_lines[first_index].strip()
            operation_type = (
                WarehouseOperationType.WRITEOFF
                if header.casefold().startswith("списание:")
                else WarehouseOperationType.TRANSFER
            )
            trigger = (
                "списание:"
                if operation_type is WarehouseOperationType.WRITEOFF
                else "перемещение:"
            )
            body: list[str] = []
            inline = header.partition(":")[2].strip()
            if inline:
                body.append(inline)
            body.extend(
                value.strip()
                for value in source_lines[first_index + 1 :]
                if value.strip()
            )
            lines: list[dict[str, Any]] = []
            for line_number, raw_line in enumerate(body, start=1):
                try:
                    item = parse_intake_message(f"{trigger} {raw_line}").lines[0]
                except IntakeParseError as line_error:
                    lines.append(
                        {
                            "line_number": line_number,
                            "raw_line": raw_line,
                            "status": "invalid",
                            "product_text": None,
                            "product_id": None,
                            "input_quantity_text": None,
                            "input_unit_text": None,
                            "quantity_scaled": None,
                            "base_unit": None,
                            "reason_text": None,
                            "comment_text": None,
                            "destination_text": None,
                            "destination_location_id": None,
                            "error_code": line_error.code,
                            "candidates": (),
                        }
                    )
                    continue
                item = ParsedIntakeLine(
                    line_number=line_number,
                    raw_line=item.raw_line,
                    product_text=item.product_text,
                    quantity_text=item.quantity_text,
                    unit_text=item.unit_text,
                    destination_text=item.destination_text,
                    reason_text=item.reason_text,
                    comment_text=item.comment_text,
                )
                lines.append(
                    self._line_payload(
                        item,
                        operation_type,
                        products,
                        source_location_id=source_location_id,
                    )
                )
            return operation_type, lines

        return parsed.operation_type, [
            self._line_payload(
                line,
                parsed.operation_type,
                products,
                source_location_id=source_location_id,
            )
            for line in parsed.lines
        ]

    def prepare_batch(
        self,
        *,
        actor_user_id: int,
        source_chat_id: int,
        source_message_id: int,
        raw_text: str,
    ) -> dict[str, Any]:
        self.access.require_warehouse_mutation(actor_user_id)
        existing = self.repository.batch_by_message(source_chat_id, source_message_id)
        if existing is not None:
            self._require_batch_owner(existing, actor_user_id)
            return existing
        settings = self.repository.runtime_settings()
        period_service = BusinessDateService(
            timezone=str(settings["timezone"]),
            cutoff=str(settings["cutoff_local_time"]),
            clock=self.clock,
        )
        now_value = period_service.clock.now()
        if now_value.tzinfo is None:
            raise WarehouseIntakeError("Clock must return timezone-aware datetime")
        now = utc_rfc3339(now_value.astimezone(UTC))
        period = period_service.period_for(now_value)
        pending = self.repository.active_batch_for_actor(
            actor_user_id,
            int(settings["source_location_id"]),
            period.business_date.isoformat(),
        )
        if pending is not None:
            return pending
        operation_type, lines = self._batch_lines(
            raw_text,
            self.repository.products(),
            source_location_id=int(settings["source_location_id"]),
        )
        status = (
            "ready" if all(line["status"] == "resolved" for line in lines)
            else "needs_resolution"
        )
        return self.repository.create_batch(
            {
                "actor_user_id": actor_user_id,
                "source_chat_id": source_chat_id,
                "source_message_id": source_message_id,
                "operation_type": operation_type.value,
                "raw_text": raw_text,
                "status": status,
                "lines": lines,
                "source_location_id": settings["source_location_id"],
                "business_date": period.business_date.isoformat(),
                "timezone": period.timezone,
                "cutoff": period.cutoff,
                "started_at_utc": utc_rfc3339(period.started_at_utc),
                "scheduled_close_at_utc": utc_rfc3339(period.scheduled_close_at_utc),
                "now": now,
            }
        )

    def replace_line_input(
        self,
        batch_id: int,
        line_id: int,
        raw_line: str,
        *,
        actor_user_id: int,
    ) -> dict[str, Any]:
        self.access.require_warehouse_mutation(actor_user_id)
        batch = self.repository.get_batch(batch_id)
        if batch is None:
            raise WarehouseOperationsError("Batch not found")
        self._require_batch_owner(batch, actor_user_id)
        trigger = "списание:" if batch["operation_type"] == "writeoff" else "перемещение:"
        try:
            parsed = parse_intake_message(f"{trigger}\n{raw_line}").lines[0]
        except IntakeParseError as exc:
            raise WarehouseIntakeError(f"{exc.code}: {exc}") from exc
        parsed = ParsedIntakeLine(
            line_number=next(
                int(line["line_number"]) for line in batch["lines"] if line["id"] == line_id
            ),
            raw_line=parsed.raw_line,
            product_text=parsed.product_text,
            quantity_text=parsed.quantity_text,
            unit_text=parsed.unit_text,
            destination_text=parsed.destination_text,
            reason_text=parsed.reason_text,
            comment_text=parsed.comment_text,
        )
        settings = self.repository.runtime_settings()
        payload = self._line_payload(
            parsed, WarehouseOperationType(str(batch["operation_type"])),
            self.repository.products(),
            source_location_id=int(settings["source_location_id"]),
        )
        return self.repository.replace_line(
            batch_id, line_id, payload, actor_user_id=actor_user_id, now=self._now()
        )

    def resolve_product(
        self,
        batch_id: int,
        line_id: int,
        product_id: int,
        *,
        actor_user_id: int,
    ) -> dict[str, Any]:
        self.access.require_warehouse_mutation(actor_user_id)
        batch = self.repository.get_batch(batch_id)
        product = self.repository.product(product_id)
        if batch is None or product is None:
            raise WarehouseOperationsError("Batch or active product not found")
        self._require_batch_owner(batch, actor_user_id)
        line = next((item for item in batch["lines"] if item["id"] == line_id), None)
        if line is None:
            raise WarehouseOperationsError("Batch line not found")
        operation_type = WarehouseOperationType(str(batch["operation_type"]))
        required = "product_writeoff" if operation_type is WarehouseOperationType.WRITEOFF else "transfer"
        if required not in product.operation_types:
            raise WarehouseOperationsError("Product does not support operation")
        try:
            quantity = parse_quantity(
                str(line["input_quantity_text"]), line["input_unit_text"], product.base_unit
            )
            line.update(
                product_id=product.id, quantity_scaled=quantity.quantity_scaled,
                base_unit=product.base_unit.value, candidates=[], error_code=None,
            )
        except QuantityError as exc:
            line.update(product_id=product.id, status="invalid", error_code=exc.code, candidates=[])
        if operation_type is WarehouseOperationType.TRANSFER and not line["destination_location_id"]:
            settings = self.repository.runtime_settings()
            destination = resolve_destination(
                str(line["destination_text"]), self.repository.locations(),
                source_location_id=int(settings["source_location_id"]),
            )
            line["destination_location_id"] = destination.exact_id
            line["error_code"] = destination.error_code
        line["status"] = (
            "resolved"
            if line.get("quantity_scaled") and (
                operation_type is WarehouseOperationType.WRITEOFF
                or line.get("destination_location_id")
            )
            else "invalid"
        )
        return self.repository.replace_line(
            batch_id, line_id, line, actor_user_id=actor_user_id, now=self._now()
        )

    def resolve_destination(
        self,
        batch_id: int,
        line_id: int,
        location_id: int,
        *,
        actor_user_id: int,
    ) -> dict[str, Any]:
        self.access.require_warehouse_mutation(actor_user_id)
        batch = self.repository.get_batch(batch_id)
        location = self.repository.location(location_id)
        settings = self.repository.runtime_settings()
        if batch is None or location is None or not location.can_be_destination:
            raise WarehouseOperationsError("Destination not found")
        self._require_batch_owner(batch, actor_user_id)
        if location.id == int(settings["source_location_id"]):
            raise WarehouseOperationsError("Source and destination must differ")
        line = next((item for item in batch["lines"] if item["id"] == line_id), None)
        if line is None or batch["operation_type"] != "transfer":
            raise WarehouseOperationsError("Transfer line not found")
        line["destination_location_id"] = location.id
        line["error_code"] = None
        line["status"] = "resolved" if line.get("product_id") and line.get("quantity_scaled") else "invalid"
        return self.repository.replace_line(
            batch_id, line_id, line, actor_user_id=actor_user_id, now=self._now()
        )

    def cancel_batch(self, batch_id: int, *, actor_user_id: int) -> dict[str, Any]:
        self.access.require_warehouse_mutation(actor_user_id)
        batch = self.repository.get_batch(batch_id)
        if batch is None:
            raise WarehouseOperationsError("Batch not found")
        self._require_batch_owner(batch, actor_user_id)
        return self.repository.cancel_batch(
            batch_id, actor_user_id=actor_user_id, now=self._now()
        )

    def commit_batch(self, batch_id: int, *, actor_user_id: int) -> list[dict[str, Any]]:
        self.access.require_warehouse_mutation(actor_user_id)
        batch = self.repository.get_batch(batch_id)
        if batch is None:
            raise WarehouseOperationsError("Batch not found")
        self._require_batch_owner(batch, actor_user_id)
        return self.repository.commit_batch(
            batch_id, actor_user_id=actor_user_id, now=self._now()
        )

    def remove_line(
        self, batch_id: int, line_id: int, *, actor_user_id: int
    ) -> dict[str, Any]:
        self.access.require_warehouse_mutation(actor_user_id)
        batch = self.repository.get_batch(batch_id)
        if batch is None:
            raise WarehouseOperationsError("Batch not found")
        self._require_batch_owner(batch, actor_user_id)
        return self.repository.remove_line(
            batch_id, line_id, actor_user_id=actor_user_id, now=self._now()
        )

    def operation_page(
        self,
        shift_id: int,
        operation_type: WarehouseOperationType | str,
        *,
        page: int,
        actor_user_id: int,
    ) -> tuple[list[dict[str, Any]], int]:
        self.access.require_warehouse_view(actor_user_id)
        if self.current_shift(actor_user_id=actor_user_id)["id"] != shift_id:
            raise WarehouseOperationsError("Shift is not current")
        return self.repository.operation_page(
            shift_id, WarehouseOperationType(operation_type).value, page=page
        )

    def aggregates(self, shift_id: int, *, actor_user_id: int) -> list[dict[str, Any]]:
        self.access.require_warehouse_view(actor_user_id)
        if self.current_shift(actor_user_id=actor_user_id)["id"] != shift_id:
            raise WarehouseOperationsError("Shift is not current")
        return self.repository.aggregates(shift_id)

    def operation_view(
        self, operation_id: int, *, actor_user_id: int
    ) -> dict[str, Any] | None:
        self.access.require_warehouse_view(actor_user_id)
        operation = self.repository.operation_view(operation_id)
        if operation is None:
            return None
        if self.current_shift(actor_user_id=actor_user_id)["id"] != operation["shift_id"]:
            return None
        return operation

    def find_product(
        self,
        text: str,
        operation_type: WarehouseOperationType | str,
        *,
        actor_user_id: int,
    ) -> Resolution:
        self.access.require_warehouse_mutation(actor_user_id)
        return resolve_product(
            text,
            WarehouseOperationType(operation_type),
            self.repository.products(),
        )

    def find_destination(
        self,
        text: str,
        *,
        source_location_id: int,
        actor_user_id: int,
    ) -> Resolution:
        self.access.require_warehouse_mutation(actor_user_id)
        return resolve_destination(
            text,
            self.repository.locations(),
            source_location_id=source_location_id,
        )

    def destination_options(self, *, actor_user_id: int) -> tuple:
        self.access.require_warehouse_mutation(actor_user_id)
        settings = self.repository.runtime_settings()
        source_id = int(settings["source_location_id"])
        return tuple(
            location for location in self.repository.locations()
            if location.can_be_destination and location.id != source_id
        )

    def product_option(self, product_id: int, *, actor_user_id: int) -> ProductOption | None:
        self.access.require_warehouse_mutation(actor_user_id)
        return self.repository.product(product_id)

    def update_product_and_quantity(
        self,
        operation_id: int,
        product_id: int,
        quantity_text: str,
        unit_text: str | None,
        *,
        expected_revision: int,
        actor_user_id: int,
    ) -> dict[str, Any]:
        self.access.require_warehouse_mutation(actor_user_id)
        operation = self.repository.get_operation(operation_id)
        product = self.repository.product(product_id)
        if operation is None or product is None:
            raise WarehouseOperationsError("Operation or product not found")
        required = (
            "product_writeoff"
            if operation["operation_type"] == "writeoff" else "transfer"
        )
        if required not in product.operation_types:
            raise WarehouseOperationsError("Product does not support operation")
        quantity = parse_quantity(quantity_text, unit_text, product.base_unit)
        return self.repository.update_operation(
            operation_id,
            {
                "product_id": product.id,
                "product_name_snapshot": product.official_name,
                "base_unit": product.base_unit.value,
                "quantity_scaled": quantity.quantity_scaled,
                "input_quantity_text": quantity.input_quantity_text,
                "input_unit_text": quantity.input_unit_text,
            },
            expected_revision=expected_revision,
            actor_user_id=actor_user_id,
            now=self._now(),
        )

    def create_manual_operation(
        self,
        *,
        actor_user_id: int,
        operation_type: WarehouseOperationType | str,
        product_id: int,
        quantity_text: str,
        unit_text: str | None = None,
        destination_location_id: int | None = None,
        reason_text: str | None = None,
        comment_text: str | None = None,
    ) -> dict[str, Any]:
        self.access.require_warehouse_mutation(actor_user_id)
        kind = WarehouseOperationType(operation_type)
        product = self.repository.product(product_id)
        if product is None:
            raise WarehouseOperationsError("Active product not found")
        required = "product_writeoff" if kind is WarehouseOperationType.WRITEOFF else "transfer"
        if required not in product.operation_types:
            raise WarehouseOperationsError("Product does not support operation")
        quantity = parse_quantity(quantity_text, unit_text, product.base_unit)
        settings = self.repository.runtime_settings()
        source_id = int(settings["source_location_id"])
        if kind is WarehouseOperationType.WRITEOFF:
            if destination_location_id is not None:
                raise WarehouseOperationsError("Writeoff cannot have destination")
        else:
            destination = (
                self.repository.location(destination_location_id)
                if destination_location_id is not None else None
            )
            if (
                destination is None or not destination.can_be_destination
                or destination.id == source_id
            ):
                raise WarehouseOperationsError("Valid transfer destination is required")
        period_service = BusinessDateService(
            timezone=str(settings["timezone"]),
            cutoff=str(settings["cutoff_local_time"]),
            clock=self.clock,
        )
        now_value = period_service.clock.now()
        period = period_service.period_for(now_value)
        now = utc_rfc3339(now_value)
        raw_text = f"{product.official_name} {quantity_text}"
        if unit_text:
            raw_text += f" {unit_text}"
        return self.repository.create_manual_operation(
            {
                "actor_user_id": actor_user_id,
                "operation_type": kind.value,
                "product_id": product.id,
                "product_name_snapshot": product.official_name,
                "quantity_scaled": quantity.quantity_scaled,
                "base_unit": product.base_unit.value,
                "input_quantity_text": quantity.input_quantity_text,
                "input_unit_text": quantity.input_unit_text,
                "source_location_id": source_id,
                "destination_location_id": destination_location_id,
                "reason_text": reason_text,
                "comment_text": comment_text,
                "raw_text": raw_text,
                "business_date": period.business_date.isoformat(),
                "timezone": period.timezone,
                "cutoff": period.cutoff,
                "started_at_utc": utc_rfc3339(period.started_at_utc),
                "scheduled_close_at_utc": utc_rfc3339(period.scheduled_close_at_utc),
                "now": now,
            }
        )

    def list_operations(self, shift_id: int, *, actor_user_id: int) -> list[dict[str, Any]]:
        self.access.require_warehouse_view(actor_user_id)
        return self.repository.list_operations(shift_id)

    def get_operation(self, operation_id: int, *, actor_user_id: int) -> dict[str, Any] | None:
        self.access.require_warehouse_view(actor_user_id)
        return self.repository.get_operation(operation_id)

    def update_product(
        self, operation_id: int, product_id: int, *, expected_revision: int,
        actor_user_id: int,
    ) -> dict[str, Any]:
        self.access.require_warehouse_mutation(actor_user_id)
        operation = self.repository.get_operation(operation_id)
        product = self.repository.product(product_id)
        if operation is None or product is None:
            raise WarehouseOperationsError("Operation or product not found")
        required = "product_writeoff" if operation["operation_type"] == "writeoff" else "transfer"
        if required not in product.operation_types:
            raise WarehouseOperationsError("Product does not support operation")
        if product.base_unit.value != operation["base_unit"]:
            raise WarehouseOperationsError("Product base unit differs from operation")
        return self.repository.update_operation(
            operation_id,
            {"product_id": product.id, "product_name_snapshot": product.official_name,
             "base_unit": product.base_unit.value},
            expected_revision=expected_revision, actor_user_id=actor_user_id, now=self._now(),
        )

    def update_quantity(
        self, operation_id: int, quantity_text: str, unit_text: str | None, *,
        expected_revision: int, actor_user_id: int,
    ) -> dict[str, Any]:
        self.access.require_warehouse_mutation(actor_user_id)
        operation = self.repository.get_operation(operation_id)
        if operation is None:
            raise WarehouseOperationsError("Operation not found")
        quantity = parse_quantity(quantity_text, unit_text, BaseUnit(operation["base_unit"]))
        return self.repository.update_operation(
            operation_id,
            {"quantity_scaled": quantity.quantity_scaled,
             "input_quantity_text": quantity.input_quantity_text,
             "input_unit_text": quantity.input_unit_text},
            expected_revision=expected_revision, actor_user_id=actor_user_id, now=self._now(),
        )

    def update_note(
        self,
        operation_id: int,
        *,
        reason_text: str | None,
        comment_text: str | None,
        expected_revision: int,
        actor_user_id: int,
    ) -> dict[str, Any]:
        self.access.require_warehouse_mutation(actor_user_id)
        if reason_text is not None and len(reason_text) > 500:
            raise WarehouseOperationsError("Reason is too long")
        if comment_text is not None and len(comment_text) > 500:
            raise WarehouseOperationsError("Comment is too long")
        return self.repository.update_operation(
            operation_id,
            {"reason_text": reason_text, "comment_text": comment_text},
            expected_revision=expected_revision, actor_user_id=actor_user_id,
            now=self._now(),
        )

    def update_destination(
        self,
        operation_id: int,
        location_id: int,
        *,
        expected_revision: int,
        actor_user_id: int,
    ) -> dict[str, Any]:
        self.access.require_warehouse_mutation(actor_user_id)
        operation = self.repository.get_operation(operation_id)
        location = self.repository.location(location_id)
        if operation is None or operation["operation_type"] != "transfer":
            raise WarehouseOperationsError("Transfer operation not found")
        if location is None or not location.can_be_destination:
            raise WarehouseOperationsError("Destination not found")
        if location.id == int(operation["source_location_id"]):
            raise WarehouseOperationsError("Source and destination must differ")
        return self.repository.update_operation(
            operation_id, {"destination_location_id": location.id},
            expected_revision=expected_revision, actor_user_id=actor_user_id,
            now=self._now(),
        )

    def update_notes(
        self, operation_id: int, *, reason_text: str | None, comment_text: str | None,
        expected_revision: int, actor_user_id: int,
    ) -> dict[str, Any]:
        return self.update_note(
            operation_id,
            reason_text=reason_text,
            comment_text=comment_text,
            expected_revision=expected_revision,
            actor_user_id=actor_user_id,
        )

    def delete_operation(
        self, operation_id: int, *, expected_revision: int, actor_user_id: int,
        deletion_reason: str,
    ) -> dict[str, Any]:
        self.access.require_warehouse_mutation(actor_user_id)
        return self.repository.soft_delete_operation(
            operation_id, expected_revision=expected_revision,
            actor_user_id=actor_user_id, deletion_reason=deletion_reason, now=self._now(),
        )
