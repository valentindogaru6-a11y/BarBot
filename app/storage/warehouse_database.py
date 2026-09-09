"""Synchronous SQLite repository for warehouse nomenclature."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from app.models.warehouse import (
    BaseUnit,
    ProductOperationType,
    WarehouseLocation,
    WarehouseProduct,
    WarehouseProductAlias,
    WarehouseSettings,
)

StableIdFactory = Callable[[set[str]], str]


class WarehouseDatabaseError(RuntimeError):
    """Base repository error with no SQLite implementation details."""


class WarehouseNotFoundError(WarehouseDatabaseError):
    """Raised when a requested warehouse record does not exist."""


class WarehouseNameConflictError(WarehouseDatabaseError):
    """Raised for a normalized name or alias namespace conflict."""


class WarehouseRevisionConflictError(WarehouseDatabaseError):
    """Raised when optimistic revision checking detects stale input."""


class WarehouseInvariantError(WarehouseDatabaseError):
    """Raised when a persisted cross-table invariant would be broken."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class WarehouseDatabase:
    """Connection-per-operation repository; migrations are owned elsewhere."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @contextmanager
    def _connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                self.path,
                timeout=5.0,
                isolation_level=None if write else "DEFERRED",
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
        except Exception as exc:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            if isinstance(exc, WarehouseDatabaseError):
                raise
            if isinstance(exc, sqlite3.IntegrityError):
                raise WarehouseNameConflictError(
                    "Номенклатура конфликтует с существующими данными"
                ) from exc
            if isinstance(exc, sqlite3.Error):
                raise WarehouseDatabaseError(
                    "Не удалось выполнить операцию с warehouse SQLite"
                ) from exc
            raise WarehouseDatabaseError(
                "Warehouse transaction прервана и отменена"
            ) from exc
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _location(row: sqlite3.Row | None) -> WarehouseLocation | None:
        return WarehouseLocation.model_validate(dict(row)) if row else None

    def _product(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row | None,
    ) -> WarehouseProduct | None:
        if row is None:
            return None
        product_id = int(row["id"])
        aliases = connection.execute(
            """
            SELECT * FROM warehouse_product_aliases
            WHERE product_id = ? ORDER BY normalized_alias, id
            """,
            (product_id,),
        ).fetchall()
        operations = connection.execute(
            """
            SELECT operation_type FROM warehouse_product_operation_types
            WHERE product_id = ? ORDER BY operation_type
            """,
            (product_id,),
        ).fetchall()
        payload = dict(row)
        payload["aliases"] = [
            WarehouseProductAlias.model_validate(dict(alias))
            for alias in aliases
        ]
        payload["operation_types"] = [entry[0] for entry in operations]
        return WarehouseProduct.model_validate(payload)

    def list_locations(self, *, include_inactive: bool = True) -> list[WarehouseLocation]:
        with self._connect() as connection:
            if include_inactive:
                rows = connection.execute(
                    "SELECT * FROM warehouse_locations ORDER BY normalized_name, id"
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM warehouse_locations
                    WHERE is_active = 1 ORDER BY normalized_name, id
                    """
                ).fetchall()
        return [location for row in rows if (location := self._location(row))]

    def get_location(self, stable_id: str) -> WarehouseLocation | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM warehouse_locations WHERE stable_id = ?",
                (stable_id,),
            ).fetchone()
        return self._location(row)

    def create_location(
        self,
        *,
        name: str,
        normalized_name: str,
        is_active: bool,
        can_be_source: bool,
        can_be_destination: bool,
        actor_user_id: int,
        stable_id_factory: StableIdFactory,
    ) -> WarehouseLocation:
        with self._connect(write=True) as connection:
            if connection.execute(
                "SELECT 1 FROM warehouse_locations WHERE normalized_name = ?",
                (normalized_name,),
            ).fetchone():
                raise WarehouseNameConflictError(
                    "Location с таким нормализованным названием уже существует"
                )
            existing_ids = {
                str(row[0])
                for row in connection.execute(
                    "SELECT stable_id FROM warehouse_locations"
                ).fetchall()
            }
            stable_id = stable_id_factory(existing_ids)
            now = _utc_now()
            cursor = connection.execute(
                """
                INSERT INTO warehouse_locations (
                    stable_id, name, normalized_name, is_active,
                    can_be_source, can_be_destination,
                    created_at_utc, created_by, updated_at_utc, updated_by,
                    revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    stable_id,
                    name,
                    normalized_name,
                    int(is_active),
                    int(can_be_source),
                    int(can_be_destination),
                    now,
                    actor_user_id,
                    now,
                    actor_user_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM warehouse_locations WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
            result = self._location(row)
            if result is None:
                raise WarehouseDatabaseError("Location не была сохранена")
            return result

    def update_location(
        self,
        stable_id: str,
        *,
        name: str,
        normalized_name: str,
        is_active: bool,
        can_be_source: bool,
        can_be_destination: bool,
        expected_revision: int,
        actor_user_id: int,
    ) -> WarehouseLocation:
        with self._connect(write=True) as connection:
            current = connection.execute(
                "SELECT * FROM warehouse_locations WHERE stable_id = ?",
                (stable_id,),
            ).fetchone()
            if current is None:
                raise WarehouseNotFoundError("Location не найдена")
            if int(current["revision"]) != expected_revision:
                raise WarehouseRevisionConflictError(
                    "Location уже была изменена другим запросом"
                )
            duplicate = connection.execute(
                """
                SELECT 1 FROM warehouse_locations
                WHERE normalized_name = ? AND id <> ?
                """,
                (normalized_name, int(current["id"])),
            ).fetchone()
            if duplicate:
                raise WarehouseNameConflictError(
                    "Location с таким нормализованным названием уже существует"
                )
            default = connection.execute(
                """
                SELECT 1 FROM warehouse_settings
                WHERE id = 1 AND default_source_location_id = ?
                """,
                (int(current["id"]),),
            ).fetchone()
            if default and (not is_active or not can_be_source):
                raise WarehouseInvariantError(
                    "Default source location должна оставаться активным source"
                )
            now = _utc_now()
            cursor = connection.execute(
                """
                UPDATE warehouse_locations
                SET name = ?, normalized_name = ?, is_active = ?,
                    can_be_source = ?, can_be_destination = ?,
                    updated_at_utc = ?, updated_by = ?, revision = revision + 1
                WHERE id = ? AND revision = ?
                """,
                (
                    name,
                    normalized_name,
                    int(is_active),
                    int(can_be_source),
                    int(can_be_destination),
                    now,
                    actor_user_id,
                    int(current["id"]),
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise WarehouseRevisionConflictError(
                    "Location уже была изменена другим запросом"
                )
            row = connection.execute(
                "SELECT * FROM warehouse_locations WHERE id = ?",
                (int(current["id"]),),
            ).fetchone()
            result = self._location(row)
            if result is None:
                raise WarehouseDatabaseError("Location не была обновлена")
            return result

    def list_products(self, *, include_inactive: bool = True) -> list[WarehouseProduct]:
        with self._connect() as connection:
            if include_inactive:
                rows = connection.execute(
                    "SELECT * FROM warehouse_products ORDER BY normalized_name, id"
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM warehouse_products
                    WHERE is_active = 1 ORDER BY normalized_name, id
                    """
                ).fetchall()
            return [
                product
                for row in rows
                if (product := self._product(connection, row))
            ]

    def get_product(self, stable_id: str) -> WarehouseProduct | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM warehouse_products WHERE stable_id = ?",
                (stable_id,),
            ).fetchone()
            return self._product(connection, row)

    @staticmethod
    def _assert_product_namespace(
        connection: sqlite3.Connection,
        official_normalized: str,
        aliases: list[tuple[str, str]],
        *,
        exclude_product_id: int | None = None,
    ) -> None:
        exclusion = "" if exclude_product_id is None else " AND id <> ?"
        parameters: tuple[object, ...] = (
            (official_normalized,)
            if exclude_product_id is None
            else (official_normalized, exclude_product_id)
        )
        if connection.execute(
            "SELECT 1 FROM warehouse_products WHERE normalized_name = ?" + exclusion,
            parameters,
        ).fetchone():
            raise WarehouseNameConflictError(
                "Official name уже используется другим продуктом"
            )
        alias_exclusion = (
            "" if exclude_product_id is None else " AND product_id <> ?"
        )
        if connection.execute(
            "SELECT 1 FROM warehouse_product_aliases "
            "WHERE normalized_alias = ?" + alias_exclusion,
            parameters,
        ).fetchone():
            raise WarehouseNameConflictError(
                "Official name уже используется как alias другого продукта"
            )

        normalized_aliases = [normalized for _alias, normalized in aliases]
        if len(normalized_aliases) != len(set(normalized_aliases)):
            raise WarehouseNameConflictError("Aliases продукта содержат дубли")
        if official_normalized in normalized_aliases:
            raise WarehouseNameConflictError(
                "Alias не должен совпадать с official name продукта"
            )
        for normalized_alias in normalized_aliases:
            product_parameters: tuple[object, ...] = (
                (normalized_alias,)
                if exclude_product_id is None
                else (normalized_alias, exclude_product_id)
            )
            if connection.execute(
                "SELECT 1 FROM warehouse_products "
                "WHERE normalized_name = ?" + exclusion,
                product_parameters,
            ).fetchone():
                raise WarehouseNameConflictError(
                    "Alias совпадает с official name другого продукта"
                )
            if connection.execute(
                "SELECT 1 FROM warehouse_product_aliases "
                "WHERE normalized_alias = ?" + alias_exclusion,
                product_parameters,
            ).fetchone():
                raise WarehouseNameConflictError(
                    "Alias уже принадлежит другому продукту"
                )

    @staticmethod
    def _replace_aliases(
        connection: sqlite3.Connection,
        product_id: int,
        aliases: list[tuple[str, str]],
        actor_user_id: int,
    ) -> None:
        connection.execute(
            "DELETE FROM warehouse_product_aliases WHERE product_id = ?",
            (product_id,),
        )
        now = _utc_now()
        connection.executemany(
            """
            INSERT INTO warehouse_product_aliases (
                product_id, alias, normalized_alias, created_at_utc, created_by
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                (product_id, alias, normalized, now, actor_user_id)
                for alias, normalized in aliases
            ],
        )

    @staticmethod
    def _replace_operation_types(
        connection: sqlite3.Connection,
        product_id: int,
        operation_types: set[ProductOperationType],
    ) -> None:
        connection.execute(
            "DELETE FROM warehouse_product_operation_types WHERE product_id = ?",
            (product_id,),
        )
        connection.executemany(
            """
            INSERT INTO warehouse_product_operation_types (
                product_id, operation_type
            ) VALUES (?, ?)
            """,
            [
                (product_id, operation_type.value)
                for operation_type in sorted(operation_types, key=str)
            ],
        )

    def create_product(
        self,
        *,
        official_name: str,
        normalized_name: str,
        base_unit: BaseUnit,
        aliases: list[tuple[str, str]],
        operation_types: set[ProductOperationType],
        is_active: bool,
        actor_user_id: int,
        stable_id_factory: StableIdFactory,
    ) -> WarehouseProduct:
        with self._connect(write=True) as connection:
            self._assert_product_namespace(connection, normalized_name, aliases)
            existing_ids = {
                str(row[0])
                for row in connection.execute(
                    "SELECT stable_id FROM warehouse_products"
                ).fetchall()
            }
            stable_id = stable_id_factory(existing_ids)
            now = _utc_now()
            cursor = connection.execute(
                """
                INSERT INTO warehouse_products (
                    stable_id, official_name, normalized_name, base_unit,
                    is_active, created_at_utc, created_by, updated_at_utc,
                    updated_by, revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    stable_id,
                    official_name,
                    normalized_name,
                    base_unit.value,
                    int(is_active),
                    now,
                    actor_user_id,
                    now,
                    actor_user_id,
                ),
            )
            product_id = int(cursor.lastrowid)
            self._replace_aliases(connection, product_id, aliases, actor_user_id)
            self._replace_operation_types(connection, product_id, operation_types)
            row = connection.execute(
                "SELECT * FROM warehouse_products WHERE id = ?",
                (product_id,),
            ).fetchone()
            result = self._product(connection, row)
            if result is None:
                raise WarehouseDatabaseError("Product не был сохранён")
            return result

    def update_product(
        self,
        stable_id: str,
        *,
        official_name: str,
        normalized_name: str,
        base_unit: BaseUnit,
        aliases: list[tuple[str, str]],
        operation_types: set[ProductOperationType],
        is_active: bool,
        expected_revision: int,
        actor_user_id: int,
    ) -> WarehouseProduct:
        with self._connect(write=True) as connection:
            current = connection.execute(
                "SELECT * FROM warehouse_products WHERE stable_id = ?",
                (stable_id,),
            ).fetchone()
            if current is None:
                raise WarehouseNotFoundError("Product не найден")
            product_id = int(current["id"])
            if int(current["revision"]) != expected_revision:
                raise WarehouseRevisionConflictError(
                    "Product уже был изменён другим запросом"
                )
            self._assert_product_namespace(
                connection,
                normalized_name,
                aliases,
                exclude_product_id=product_id,
            )
            now = _utc_now()
            cursor = connection.execute(
                """
                UPDATE warehouse_products
                SET official_name = ?, normalized_name = ?, base_unit = ?,
                    is_active = ?, updated_at_utc = ?, updated_by = ?,
                    revision = revision + 1
                WHERE id = ? AND revision = ?
                """,
                (
                    official_name,
                    normalized_name,
                    base_unit.value,
                    int(is_active),
                    now,
                    actor_user_id,
                    product_id,
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise WarehouseRevisionConflictError(
                    "Product уже был изменён другим запросом"
                )
            self._replace_aliases(connection, product_id, aliases, actor_user_id)
            self._replace_operation_types(connection, product_id, operation_types)
            row = connection.execute(
                "SELECT * FROM warehouse_products WHERE id = ?",
                (product_id,),
            ).fetchone()
            result = self._product(connection, row)
            if result is None:
                raise WarehouseDatabaseError("Product не был обновлён")
            return result

    def set_product_active(
        self,
        stable_id: str,
        *,
        is_active: bool,
        expected_revision: int,
        actor_user_id: int,
    ) -> WarehouseProduct:
        with self._connect(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM warehouse_products WHERE stable_id = ?",
                (stable_id,),
            ).fetchone()
            if row is None:
                raise WarehouseNotFoundError("Product не найден")
            if int(row["revision"]) != expected_revision:
                raise WarehouseRevisionConflictError(
                    "Product уже был изменён другим запросом"
                )
            cursor = connection.execute(
                """
                UPDATE warehouse_products
                SET is_active = ?, updated_at_utc = ?, updated_by = ?,
                    revision = revision + 1
                WHERE id = ? AND revision = ?
                """,
                (
                    int(is_active),
                    _utc_now(),
                    actor_user_id,
                    int(row["id"]),
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise WarehouseRevisionConflictError(
                    "Product уже был изменён другим запросом"
                )
            updated = connection.execute(
                "SELECT * FROM warehouse_products WHERE id = ?",
                (int(row["id"]),),
            ).fetchone()
            result = self._product(connection, updated)
            if result is None:
                raise WarehouseDatabaseError("Product не был обновлён")
            return result

    def get_settings(self) -> WarehouseSettings:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM warehouse_settings WHERE id = 1"
            ).fetchone()
        if row is None:
            raise WarehouseInvariantError("Warehouse settings отсутствуют")
        return WarehouseSettings.model_validate(dict(row))

    def update_settings(
        self,
        *,
        timezone: str,
        cutoff_local_time: str,
        default_source_location_id: int,
        actor_user_id: int,
    ) -> WarehouseSettings:
        with self._connect(write=True) as connection:
            location = connection.execute(
                """
                SELECT is_active, can_be_source FROM warehouse_locations
                WHERE id = ?
                """,
                (default_source_location_id,),
            ).fetchone()
            if location is None:
                raise WarehouseNotFoundError("Default source location не найдена")
            if not bool(location["is_active"]) or not bool(
                location["can_be_source"]
            ):
                raise WarehouseInvariantError(
                    "Default source location должна быть активным source"
                )
            connection.execute(
                """
                UPDATE warehouse_settings
                SET timezone = ?, cutoff_local_time = ?,
                    default_source_location_id = ?, updated_at_utc = ?,
                    updated_by = ?
                WHERE id = 1
                """,
                (
                    timezone,
                    cutoff_local_time,
                    default_source_location_id,
                    _utc_now(),
                    actor_user_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM warehouse_settings WHERE id = 1"
            ).fetchone()
            if row is None:
                raise WarehouseInvariantError("Warehouse settings отсутствуют")
            return WarehouseSettings.model_validate(dict(row))
