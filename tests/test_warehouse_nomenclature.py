"""Tests for warehouse nomenclature repository and service boundaries."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.models.user import Role
from app.models.warehouse import (
    BaseUnit,
    ProductOperationType,
    WarehouseLocationCreate,
    WarehouseLocationUpdate,
    WarehouseProductCreate,
    WarehouseProductUpdate,
    WarehouseSettingsUpdate,
)
from app.services.access_service import AccessDeniedError, AccessService
from app.services.user_service import UserService
from app.services.warehouse import (
    WarehouseNomenclatureService,
    WarehouseValidationError,
    normalize_warehouse_name,
)
from app.storage.user_database import UserDatabase
from app.storage.warehouse_database import (
    WarehouseDatabase,
    WarehouseDatabaseError,
    WarehouseInvariantError,
    WarehouseNameConflictError,
    WarehouseRevisionConflictError,
)

ADMIN_ID = 10
SUPER_ADMIN_ID = 1


@pytest.fixture
def warehouse_services(
    tmp_path: Path,
) -> tuple[WarehouseNomenclatureService, WarehouseDatabase, AccessService]:
    path = tmp_path / "warehouse.db"
    users_database = UserDatabase(path)
    users_database.initialize()
    users = UserService(users_database)
    for user_id in (SUPER_ADMIN_ID, ADMIN_ID, 20, 30, 40, 50):
        users.ensure_user(user_id)
    users_database.set_role(ADMIN_ID, Role.ADMIN, actor_user_id=SUPER_ADMIN_ID)
    users_database.set_role(20, Role.BARTENDER, actor_user_id=ADMIN_ID)
    users_database.set_role(30, Role.ACCOUNTANT, actor_user_id=ADMIN_ID)
    users_database.set_role(40, Role.USER, actor_user_id=ADMIN_ID)
    users_database.set_role(50, Role.BLOCKED, actor_user_id=ADMIN_ID)
    access = AccessService(frozenset({SUPER_ADMIN_ID}), users)
    database = WarehouseDatabase(path)
    return WarehouseNomenclatureService(database, access), database, access


def _product(
    name: str,
    *,
    aliases: list[str] | None = None,
    base_unit: BaseUnit = BaseUnit.KG,
    operation_types: set[ProductOperationType] | None = None,
) -> WarehouseProductCreate:
    return WarehouseProductCreate(
        official_name=name,
        base_unit=base_unit,
        aliases=aliases or [],
        operation_types=operation_types
        or {ProductOperationType.PRODUCT_WRITEOFF},
    )


def test_warehouse_management_capability_is_admin_only(
    warehouse_services,
) -> None:
    service, _, access = warehouse_services
    assert access.can_manage_warehouse_nomenclature(SUPER_ADMIN_ID)
    assert access.can_manage_warehouse_nomenclature(ADMIN_ID)
    for user_id in (20, 30, 40, 50):
        assert not access.can_manage_warehouse_nomenclature(user_id)
        with pytest.raises(AccessDeniedError):
            service.list_products(user_id)


def test_location_crud_rename_keeps_stable_id(warehouse_services) -> None:
    service, _, _ = warehouse_services
    created = service.create_location(
        ADMIN_ID, WarehouseLocationCreate(name="Склад кухни")
    )
    assert service.get_location(ADMIN_ID, created.stable_id) == created
    assert {entry.stable_id for entry in service.list_locations(ADMIN_ID)} == {
        "bar",
        "kitchen",
        "confectionery",
        created.stable_id,
    }
    updated = service.update_location(
        ADMIN_ID,
        created.stable_id,
        WarehouseLocationUpdate(
            name="Основной склад",
            is_active=True,
            can_be_source=True,
            can_be_destination=True,
            expected_revision=created.revision,
        ),
    )
    assert updated.stable_id == created.stable_id
    assert updated.name == "Основной склад"
    assert updated.revision == 2


def test_location_collision_duplicate_capabilities_and_activation(
    warehouse_services,
) -> None:
    service, _, _ = warehouse_services
    first = service.create_location(
        ADMIN_ID, WarehouseLocationCreate(name="Бар-Склад")
    )
    second = service.create_location(
        ADMIN_ID, WarehouseLocationCreate(name="Бар Склад")
    )
    assert (first.stable_id, second.stable_id) == ("bar_sklad", "bar_sklad_2")
    with pytest.raises(WarehouseNameConflictError):
        service.create_location(
            ADMIN_ID, WarehouseLocationCreate(name="  бар   склад  ")
        )
    with pytest.raises(WarehouseValidationError):
        service.create_location(
            ADMIN_ID,
            WarehouseLocationCreate(
                name="Невозможная точка",
                can_be_source=False,
                can_be_destination=False,
            ),
        )
    inactive = service.set_location_active(
        ADMIN_ID,
        first.stable_id,
        is_active=False,
        expected_revision=first.revision,
    )
    assert not inactive.is_active
    with pytest.raises(WarehouseRevisionConflictError):
        service.set_location_active(
            ADMIN_ID,
            first.stable_id,
            is_active=True,
            expected_revision=first.revision,
        )
    with pytest.raises(WarehouseInvariantError):
        service.set_location_active(
            ADMIN_ID, "bar", is_active=False, expected_revision=1
        )


def test_product_create_get_with_aliases_and_operation_types(
    warehouse_services,
) -> None:
    service, _, _ = warehouse_services
    created = service.create_product(
        ADMIN_ID,
        _product(
            "Апельсин",
            aliases=["Апельсин свежий"],
            operation_types={
                ProductOperationType.PRODUCT_WRITEOFF,
                ProductOperationType.TRANSFER,
            },
        ),
    )
    assert service.get_product(ADMIN_ID, created.stable_id) == created
    assert created.base_unit is BaseUnit.KG
    assert {alias.alias for alias in created.aliases} == {"Апельсин свежий"}
    assert created.operation_types == frozenset(
        {
            ProductOperationType.PRODUCT_WRITEOFF,
            ProductOperationType.TRANSFER,
        }
    )


def test_product_rename_keeps_stable_id_and_replaces_children(
    warehouse_services,
) -> None:
    service, _, _ = warehouse_services
    created = service.create_product(
        ADMIN_ID, _product("Лимон", aliases=["Лимон старый"])
    )
    updated = service.update_product(
        ADMIN_ID,
        created.stable_id,
        WarehouseProductUpdate(
            official_name="Лимон свежий",
            base_unit=BaseUnit.PCS,
            aliases=["Лимон новый"],
            operation_types={ProductOperationType.TRANSFER},
            is_active=True,
            expected_revision=created.revision,
        ),
    )
    assert updated.stable_id == created.stable_id
    assert updated.base_unit is BaseUnit.PCS
    assert {alias.alias for alias in updated.aliases} == {"Лимон новый"}
    assert updated.operation_types == frozenset({ProductOperationType.TRANSFER})


def test_product_stable_id_collision_gets_suffix(warehouse_services) -> None:
    service, _, _ = warehouse_services
    first = service.create_product(ADMIN_ID, _product("Сироп-Микс"))
    second = service.create_product(ADMIN_ID, _product("Сироп Микс"))

    assert first.stable_id == "sirop_miks"
    assert second.stable_id == "sirop_miks_2"


def test_product_logical_namespace_rejects_cross_conflicts(
    warehouse_services,
) -> None:
    service, _, _ = warehouse_services
    orange = service.create_product(
        ADMIN_ID,
        _product("Апельсин", aliases=["Цитрус", "Апельсин свежий"]),
    )
    with pytest.raises(WarehouseNameConflictError):
        service.create_product(ADMIN_ID, _product("  апельсин "))
    with pytest.raises(WarehouseNameConflictError):
        service.create_product(ADMIN_ID, _product("Цитрус"))

    lemon = service.create_product(ADMIN_ID, _product("Лимон"))
    with pytest.raises(WarehouseNameConflictError):
        service.update_product(
            ADMIN_ID,
            lemon.stable_id,
            WarehouseProductUpdate(
                official_name=lemon.official_name,
                base_unit=lemon.base_unit,
                aliases=["Апельсин", "Апельсин свежий"],
                operation_types=set(lemon.operation_types),
                is_active=True,
                expected_revision=lemon.revision,
            ),
        )
    assert service.get_product(ADMIN_ID, orange.stable_id) == orange
    assert service.get_product(ADMIN_ID, lemon.stable_id) == lemon


def test_product_validation_activation_and_revision(
    warehouse_services,
) -> None:
    service, _, _ = warehouse_services
    with pytest.raises(WarehouseValidationError):
        service.create_product(
            ADMIN_ID,
            WarehouseProductCreate(
                official_name="Без операций",
                base_unit=BaseUnit.KG,
                operation_types=set(),
            ),
        )
    with pytest.raises(ValidationError):
        WarehouseProductCreate(
            official_name="Неверная единица",
            base_unit="gram",
            operation_types={ProductOperationType.TRANSFER},
        )

    product = service.create_product(ADMIN_ID, _product("Мята"))
    inactive = service.set_product_active(
        ADMIN_ID,
        product.stable_id,
        is_active=False,
        expected_revision=product.revision,
    )
    assert not inactive.is_active
    assert service.list_products(ADMIN_ID, include_inactive=False) == []
    with pytest.raises(WarehouseRevisionConflictError):
        service.set_product_active(
            ADMIN_ID,
            product.stable_id,
            is_active=True,
            expected_revision=product.revision,
        )


def test_product_transaction_rolls_back_after_child_failure(
    warehouse_services,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, database, _ = warehouse_services

    def fail_alias_write(*_values) -> None:
        raise RuntimeError("simulated alias write failure")

    monkeypatch.setattr(database, "_replace_aliases", fail_alias_write)
    with pytest.raises(WarehouseDatabaseError, match="прервана"):
        service.create_product(ADMIN_ID, _product("Не сохранится"))
    assert database.list_products() == []


def test_settings_validation_update_and_reinitialize(warehouse_services) -> None:
    service, database, _ = warehouse_services
    source = service.create_location(
        ADMIN_ID, WarehouseLocationCreate(name="Новый source")
    )
    settings = service.update_settings(
        ADMIN_ID,
        WarehouseSettingsUpdate(
            timezone="Europe/Warsaw",
            cutoff_local_time="22:30",
            default_source_location_id=source.id,
        ),
    )
    assert settings.timezone == "Europe/Warsaw"
    assert settings.default_source_location_id == source.id
    with pytest.raises(WarehouseValidationError):
        service.update_settings(
            ADMIN_ID,
            WarehouseSettingsUpdate(
                timezone="Not/A_Zone",
                cutoff_local_time="22:30",
                default_source_location_id=source.id,
            ),
        )
    UserDatabase(database.path).initialize()


def test_normalization_is_conservative() -> None:
    assert normalize_warehouse_name("  АПЕЛЬСИН... ") == "апельсин"
    assert normalize_warehouse_name("Ёжевика") == "ежевика"
    assert normalize_warehouse_name("Сок яблочный") != normalize_warehouse_name(
        "Фреш яблочный"
    )
