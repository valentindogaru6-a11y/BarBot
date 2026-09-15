"""Authorized required-writeoff and shift-closing workflows."""

from __future__ import annotations

from datetime import UTC
from decimal import Decimal
from typing import Any

from app.models.catalog_item import MAX_STABLE_ID_LENGTH
from app.models.warehouse.closing import RequiredRuleInput, RequiredRuleUpdate
from app.models.warehouse.nomenclature import BaseUnit
from app.services.access_service import AccessService
from app.services.stable_id import generate_stable_id
from app.services.warehouse.business_date_service import (
    BusinessDateService,
    Clock,
    utc_rfc3339,
)
from app.services.warehouse.operations_service import WarehouseOperationsService
from app.services.warehouse.quantity_service import parse_quantity
from app.storage.warehouse_closing_database import (
    WarehouseClosingDatabase,
    WarehouseClosingError,
)


class WarehouseClosingService:
    def __init__(
        self,
        repository: WarehouseClosingDatabase,
        operations: WarehouseOperationsService,
        access: AccessService,
        *,
        clock: Clock | None = None,
    ) -> None:
        self.repository = repository
        self.operations = operations
        self.access = access
        self.clock = clock

    def _now(self) -> str:
        service = BusinessDateService(clock=self.clock)
        return utc_rfc3339(service.clock.now().astimezone(UTC))

    def _ensure_current_shift(self, actor_user_id: int) -> dict[str, Any]:
        self.access.require_warehouse_mutation(actor_user_id)
        settings = self.operations.repository.runtime_settings()
        period_service = BusinessDateService(
            timezone=str(settings["timezone"]),
            cutoff=str(settings["cutoff_local_time"]),
            clock=self.clock,
        )
        now_value = period_service.clock.now()
        period = period_service.period_for(now_value)
        return self.repository.ensure_shift(
            {
                "source_location_id": int(settings["source_location_id"]),
                "business_date": period.business_date.isoformat(),
                "timezone": period.timezone,
                "cutoff": period.cutoff,
                "started_at_utc": utc_rfc3339(period.started_at_utc),
                "scheduled_close_at_utc": utc_rfc3339(
                    period.scheduled_close_at_utc
                ),
                "now": utc_rfc3339(now_value.astimezone(UTC)),
            }
        )

    def overview(self, *, actor_user_id: int) -> dict[str, Any]:
        context = self.access.require_warehouse_view(actor_user_id)
        shift = self.operations.current_shift(actor_user_id=actor_user_id)
        if shift["id"] is None:
            if context.can_mutate_warehouse:
                shift = self._ensure_current_shift(actor_user_id)
            else:
                return {
                    "shift": shift,
                    "writeoffs": 0,
                    "transfers": 0,
                    "developments": 0,
                    "development_groups": 0,
                    "requirements_total": 0,
                    "requirements_resolved": 0,
                }
        requirements = self.repository.prepare_requirements(
            int(shift["id"]), now=self._now()
        )
        operations = self.operations.list_operations(
            int(shift["id"]), actor_user_id=actor_user_id
        )
        return {
            "shift": shift,
            "writeoffs": sum(row["operation_type"] == "writeoff" for row in operations),
            "transfers": sum(row["operation_type"] == "transfer" for row in operations),
            "developments": sum(
                row["operation_type"] == "development" for row in operations
            ),
            "development_groups": len(
                {
                    int(row["development_group_id"])
                    for row in operations
                    if row["operation_type"] == "development"
                }
            ),
            "requirements_total": len(requirements),
            "requirements_resolved": sum(
                row["status"] != "pending" for row in requirements
            ),
        }

    def active_session(self, *, actor_user_id: int) -> dict[str, Any] | None:
        self.access.require_warehouse_view(actor_user_id)
        shift = self.operations.current_shift(actor_user_id=actor_user_id)
        if shift["id"] is None:
            return None
        return self.repository.active_session(int(shift["id"]))

    def start(self, *, actor_user_id: int, trigger_type: str = "manual") -> dict[str, Any]:
        shift = self._ensure_current_shift(actor_user_id)
        return self.repository.start_closing(
            int(shift["id"]),
            actor_user_id=actor_user_id,
            trigger_type=trigger_type,
            now=self._now(),
        )

    def resume(self, session_id: int, *, actor_user_id: int) -> dict[str, Any]:
        self.access.require_warehouse_mutation(actor_user_id)
        session = self.repository.refresh_session(session_id, now=self._now())
        current = self.operations.current_shift(actor_user_id=actor_user_id)
        if current["id"] != session["shift_id"]:
            raise WarehouseClosingError("Closing session is not current")
        if session["status"] not in {"collecting", "ready"}:
            raise WarehouseClosingError("Closing session is not active")
        return session

    @staticmethod
    def _requirement(session: dict[str, Any], requirement_id: int) -> dict[str, Any]:
        requirement = next(
            (row for row in session["requirements"] if row["id"] == requirement_id),
            None,
        )
        if requirement is None:
            raise WarehouseClosingError("Requirement not found")
        return requirement

    def recommended(
        self,
        session_id: int,
        requirement_id: int,
        *,
        actor_user_id: int,
    ) -> dict[str, Any]:
        session = self.resume(session_id, actor_user_id=actor_user_id)
        requirement = self._requirement(session, requirement_id)
        quantity = requirement["suggested_quantity_scaled"]
        if requirement["quantity_mode"] != "recommended" or quantity is None:
            raise WarehouseClosingError("Requirement has no recommendation")
        self.repository.record_required_operation(
            session_id,
            requirement_id,
            quantity_scaled=int(quantity),
            input_quantity_text=(
                format(Decimal(int(quantity)) / 1_000_000, "f")
                .rstrip("0")
                .rstrip(".")
            ),
            input_unit_text=str(requirement["base_unit"]),
            actor_user_id=actor_user_id,
            now=self._now(),
        )
        return self.resume(session_id, actor_user_id=actor_user_id)

    def quantity_preview(
        self,
        session_id: int,
        requirement_id: int,
        raw_quantity: str,
        *,
        actor_user_id: int,
    ) -> dict[str, Any]:
        session = self.resume(session_id, actor_user_id=actor_user_id)
        requirement = self._requirement(session, requirement_id)
        parts = raw_quantity.strip().split(maxsplit=1)
        quantity_text = parts[0] if parts else ""
        unit_text = parts[1] if len(parts) == 2 else None
        parsed = parse_quantity(
            quantity_text,
            unit_text,
            BaseUnit(str(requirement["base_unit"])),
        )
        return {
            "session_id": session_id,
            "requirement_id": requirement_id,
            "product_name": requirement["product_name"],
            "quantity_scaled": parsed.quantity_scaled,
            "base_unit": parsed.base_unit.value,
            "input_quantity_text": parsed.input_quantity_text,
            "input_unit_text": parsed.input_unit_text,
        }

    def save_quantity(
        self,
        preview: dict[str, Any],
        *,
        actor_user_id: int,
    ) -> dict[str, Any]:
        self.access.require_warehouse_mutation(actor_user_id)
        self.repository.record_required_operation(
            int(preview["session_id"]),
            int(preview["requirement_id"]),
            quantity_scaled=int(preview["quantity_scaled"]),
            input_quantity_text=str(preview["input_quantity_text"]),
            input_unit_text=preview.get("input_unit_text"),
            actor_user_id=actor_user_id,
            now=self._now(),
        )
        return self.resume(
            int(preview["session_id"]), actor_user_id=actor_user_id
        )

    def mark_not_applicable(
        self,
        session_id: int,
        requirement_id: int,
        reason: str,
        *,
        actor_user_id: int,
    ) -> dict[str, Any]:
        self.access.require_warehouse_mutation(actor_user_id)
        cleaned = reason.strip()
        if not cleaned:
            raise WarehouseClosingError("Resolution reason is required")
        if len(cleaned) > 500:
            raise WarehouseClosingError("Resolution reason is too long")
        return self.repository.mark_not_applicable(
            session_id,
            requirement_id,
            reason=cleaned,
            actor_user_id=actor_user_id,
            now=self._now(),
        )

    def cancel(self, session_id: int, *, actor_user_id: int) -> dict[str, Any]:
        self.access.require_warehouse_mutation(actor_user_id)
        return self.repository.cancel_session(
            session_id, actor_user_id=actor_user_id, now=self._now()
        )

    def finalize(
        self,
        session_id: int,
        *,
        expected_revision: int,
        actor_user_id: int,
    ) -> dict[str, Any]:
        self.access.require_warehouse_mutation(actor_user_id)
        return self.repository.finalize(
            session_id,
            expected_revision=expected_revision,
            actor_user_id=actor_user_id,
            now=self._now(),
            authorize=lambda: self.access.require_warehouse_mutation(actor_user_id),
        )

    def list_rules(self, *, actor_user_id: int) -> list[dict[str, Any]]:
        self.access.require_warehouse_required_rule_management(actor_user_id)
        return self.repository.list_rules()

    def get_rule(self, rule_id: int, *, actor_user_id: int) -> dict[str, Any] | None:
        self.access.require_warehouse_required_rule_management(actor_user_id)
        return self.repository.get_rule(rule_id)

    def create_rule(
        self,
        command: RequiredRuleInput,
        *,
        actor_user_id: int,
    ) -> dict[str, Any]:
        self.access.require_warehouse_required_rule_management(actor_user_id)
        product = self.operations.product_option(
            command.product_id, actor_user_id=actor_user_id
        )
        if (
            product is None
            or product.base_unit is not command.base_unit
            or "product_writeoff" not in product.operation_types
        ):
            raise WarehouseClosingError("Product unit does not match rule")
        stable_id = generate_stable_id(
            f"required {product.official_name}",
            self.repository.rule_stable_ids(),
            MAX_STABLE_ID_LENGTH,
        )
        return self.repository.create_rule(
            command.model_dump(mode="json"),
            stable_id=stable_id,
            actor_user_id=actor_user_id,
            now=self._now(),
        )

    def update_rule(
        self,
        rule_id: int,
        command: RequiredRuleUpdate,
        *,
        actor_user_id: int,
    ) -> dict[str, Any]:
        self.access.require_warehouse_required_rule_management(actor_user_id)
        product = self.operations.product_option(
            command.product_id, actor_user_id=actor_user_id
        )
        if (
            product is None
            or product.base_unit is not command.base_unit
            or "product_writeoff" not in product.operation_types
        ):
            raise WarehouseClosingError("Product unit does not match rule")
        return self.repository.update_rule(
            rule_id,
            command.model_dump(mode="json", exclude={"expected_revision"}),
            expected_revision=command.expected_revision,
            actor_user_id=actor_user_id,
            now=self._now(),
        )

    def patch_rule(
        self,
        rule_id: int,
        changes: dict[str, Any],
        *,
        expected_revision: int,
        actor_user_id: int,
    ) -> dict[str, Any]:
        current = self.get_rule(rule_id, actor_user_id=actor_user_id)
        if current is None:
            raise WarehouseClosingError("Required rule not found")
        payload = {
            key: current[key]
            for key in RequiredRuleInput.model_fields
        }
        payload.update(changes)
        return self.update_rule(
            rule_id,
            RequiredRuleUpdate(
                **payload,
                expected_revision=expected_revision,
            ),
            actor_user_id=actor_user_id,
        )
