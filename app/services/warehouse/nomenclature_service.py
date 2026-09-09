"""Authorized business workflows for warehouse nomenclature."""

from __future__ import annotations

import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.models.catalog_item import MAX_STABLE_ID_LENGTH
from app.models.warehouse import (
    WarehouseLocation,
    WarehouseLocationCreate,
    WarehouseLocationUpdate,
    ProductOperationType,
    WarehouseProduct,
    WarehouseProductCreate,
    WarehouseProductUpdate,
    WarehouseSettings,
    WarehouseSettingsUpdate,
)
from app.services.access_service import AccessDeniedError, AccessService
from app.services.stable_id import generate_stable_id
from app.services.warehouse.normalization import normalize_warehouse_name
from app.storage.warehouse_database import (
    WarehouseDatabase,
    WarehouseNotFoundError,
)

_LOCAL_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class WarehouseValidationError(ValueError):
    """Raised when a warehouse command violates business validation."""


class WarehouseNomenclatureService:
    def __init__(
        self,
        database: WarehouseDatabase,
        access: AccessService,
    ) -> None:
        self.database = database
        self.access = access

    def _authorize(self, actor_user_id: int) -> None:
        if not self.access.can_manage_warehouse_nomenclature(actor_user_id):
            raise AccessDeniedError("Недостаточно прав для warehouse nomenclature")

    @staticmethod
    def _normalized_name(value: str) -> str:
        normalized = normalize_warehouse_name(value)
        if not normalized:
            raise WarehouseValidationError(
                "Название пусто после безопасной нормализации"
            )
        return normalized

    @classmethod
    def _aliases(cls, values: list[str]) -> list[tuple[str, str]]:
        return [(value.strip(), cls._normalized_name(value)) for value in values]

    @staticmethod
    def _validate_location_capabilities(
        can_be_source: bool,
        can_be_destination: bool,
    ) -> None:
        if not can_be_source and not can_be_destination:
            raise WarehouseValidationError(
                "Location должна быть source, destination или обоими"
            )

    @staticmethod
    def _validate_operation_types(values: set[ProductOperationType]) -> None:
        if not values:
            raise WarehouseValidationError(
                "Product должен поддерживать хотя бы один operation type"
            )

    def list_locations(
        self,
        actor_user_id: int,
        *,
        include_inactive: bool = True,
    ) -> list[WarehouseLocation]:
        self._authorize(actor_user_id)
        return self.database.list_locations(include_inactive=include_inactive)

    def get_location(
        self,
        actor_user_id: int,
        stable_id: str,
    ) -> WarehouseLocation | None:
        self._authorize(actor_user_id)
        return self.database.get_location(stable_id)

    def create_location(
        self,
        actor_user_id: int,
        command: WarehouseLocationCreate,
    ) -> WarehouseLocation:
        self._authorize(actor_user_id)
        self._validate_location_capabilities(
            command.can_be_source,
            command.can_be_destination,
        )
        normalized = self._normalized_name(command.name)
        return self.database.create_location(
            name=command.name,
            normalized_name=normalized,
            is_active=command.is_active,
            can_be_source=command.can_be_source,
            can_be_destination=command.can_be_destination,
            actor_user_id=actor_user_id,
            stable_id_factory=lambda existing: generate_stable_id(
                command.name,
                existing,
                MAX_STABLE_ID_LENGTH,
            ),
        )

    def update_location(
        self,
        actor_user_id: int,
        stable_id: str,
        command: WarehouseLocationUpdate,
    ) -> WarehouseLocation:
        self._authorize(actor_user_id)
        self._validate_location_capabilities(
            command.can_be_source,
            command.can_be_destination,
        )
        return self.database.update_location(
            stable_id,
            name=command.name,
            normalized_name=self._normalized_name(command.name),
            is_active=command.is_active,
            can_be_source=command.can_be_source,
            can_be_destination=command.can_be_destination,
            expected_revision=command.expected_revision,
            actor_user_id=actor_user_id,
        )

    def set_location_active(
        self,
        actor_user_id: int,
        stable_id: str,
        *,
        is_active: bool,
        expected_revision: int,
    ) -> WarehouseLocation:
        self._authorize(actor_user_id)
        current = self.database.get_location(stable_id)
        if current is None:
            raise WarehouseNotFoundError("Location не найдена")
        return self.update_location(
            actor_user_id,
            stable_id,
            WarehouseLocationUpdate(
                name=current.name,
                is_active=is_active,
                can_be_source=current.can_be_source,
                can_be_destination=current.can_be_destination,
                expected_revision=expected_revision,
            ),
        )

    def list_products(
        self,
        actor_user_id: int,
        *,
        include_inactive: bool = True,
    ) -> list[WarehouseProduct]:
        self._authorize(actor_user_id)
        return self.database.list_products(include_inactive=include_inactive)

    def get_product(
        self,
        actor_user_id: int,
        stable_id: str,
    ) -> WarehouseProduct | None:
        self._authorize(actor_user_id)
        return self.database.get_product(stable_id)

    def create_product(
        self,
        actor_user_id: int,
        command: WarehouseProductCreate,
    ) -> WarehouseProduct:
        self._authorize(actor_user_id)
        self._validate_operation_types(command.operation_types)
        normalized = self._normalized_name(command.official_name)
        return self.database.create_product(
            official_name=command.official_name,
            normalized_name=normalized,
            base_unit=command.base_unit,
            aliases=self._aliases(command.aliases),
            operation_types=command.operation_types,
            is_active=command.is_active,
            actor_user_id=actor_user_id,
            stable_id_factory=lambda existing: generate_stable_id(
                command.official_name,
                existing,
                MAX_STABLE_ID_LENGTH,
            ),
        )

    def update_product(
        self,
        actor_user_id: int,
        stable_id: str,
        command: WarehouseProductUpdate,
    ) -> WarehouseProduct:
        self._authorize(actor_user_id)
        self._validate_operation_types(command.operation_types)
        return self.database.update_product(
            stable_id,
            official_name=command.official_name,
            normalized_name=self._normalized_name(command.official_name),
            base_unit=command.base_unit,
            aliases=self._aliases(command.aliases),
            operation_types=command.operation_types,
            is_active=command.is_active,
            expected_revision=command.expected_revision,
            actor_user_id=actor_user_id,
        )

    def set_product_active(
        self,
        actor_user_id: int,
        stable_id: str,
        *,
        is_active: bool,
        expected_revision: int,
    ) -> WarehouseProduct:
        self._authorize(actor_user_id)
        return self.database.set_product_active(
            stable_id,
            is_active=is_active,
            expected_revision=expected_revision,
            actor_user_id=actor_user_id,
        )

    def get_settings(self, actor_user_id: int) -> WarehouseSettings:
        self._authorize(actor_user_id)
        return self.database.get_settings()

    def update_settings(
        self,
        actor_user_id: int,
        command: WarehouseSettingsUpdate,
    ) -> WarehouseSettings:
        self._authorize(actor_user_id)
        try:
            ZoneInfo(command.timezone)
        except ZoneInfoNotFoundError as exc:
            raise WarehouseValidationError("Неизвестная timezone") from exc
        if not _LOCAL_TIME_RE.fullmatch(command.cutoff_local_time):
            raise WarehouseValidationError(
                "cutoff_local_time должен иметь формат HH:MM"
            )
        return self.database.update_settings(
            timezone=command.timezone,
            cutoff_local_time=command.cutoff_local_time,
            default_source_location_id=command.default_source_location_id,
            actor_user_id=actor_user_id,
        )
