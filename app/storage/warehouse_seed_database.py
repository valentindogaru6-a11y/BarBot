"""Single-transaction warehouse product seed preflight and import."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from app.models.warehouse.seed import (
    SeedOperationType,
    WarehouseProductsSeed,
    WarehouseSeedImportPlan,
    WarehouseSeedProduct,
)
from app.storage.warehouse_schema import (
    WarehouseSchemaValidationError,
    validate_warehouse_schema,
)
from app.storage.warehouse_operations_schema import (
    WarehouseOperationsSchemaError,
    validate_warehouse_operations_schema,
)
from app.utils.warehouse import normalize_warehouse_name


class WarehouseSeedImportError(RuntimeError):
    """Raised when a seed cannot be safely planned or imported."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _operation_value(value: SeedOperationType) -> str:
    return "product_writeoff" if value is SeedOperationType.WRITEOFF else "transfer"


class WarehouseSeedDatabase:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def import_seed(
        self,
        seed: WarehouseProductsSeed,
        *,
        apply: bool = False,
    ) -> WarehouseSeedImportPlan:
        connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("BEGIN IMMEDIATE")
            self._validate_schema(connection)
            plan = self._plan(connection, seed)
            if apply and plan.is_valid:
                self._insert_products(connection, seed, set(plan.create))
                self._validate_final_namespace(connection)
                connection.commit()
                return plan.model_copy(update={"applied": True})
            connection.rollback()
            return plan
        except Exception as exc:
            if connection.in_transaction:
                connection.rollback()
            if isinstance(exc, WarehouseSeedImportError):
                raise
            if isinstance(exc, sqlite3.Error):
                raise WarehouseSeedImportError("Warehouse seed transaction failed") from exc
            raise WarehouseSeedImportError("Warehouse seed import rolled back") from exc
        finally:
            connection.close()

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version not in {3, 4}:
            raise WarehouseSeedImportError(
                f"Warehouse schema v3 or v4 required; found v{version}"
            )
        try:
            validate_warehouse_schema(connection)
            if version == 4:
                validate_warehouse_operations_schema(connection)
        except (
            WarehouseSchemaValidationError,
            WarehouseOperationsSchemaError,
        ) as exc:
            raise WarehouseSeedImportError(str(exc)) from exc

    @staticmethod
    def _existing_product(
        connection: sqlite3.Connection,
        stable_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM warehouse_products WHERE stable_id = ?", (stable_id,)
        ).fetchone()

    @staticmethod
    def _matches(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        product: WarehouseSeedProduct,
    ) -> bool:
        aliases = connection.execute(
            "SELECT alias, normalized_alias FROM warehouse_product_aliases "
            "WHERE product_id = ? ORDER BY normalized_alias",
            (int(row["id"]),),
        ).fetchall()
        operations = connection.execute(
            "SELECT operation_type FROM warehouse_product_operation_types "
            "WHERE product_id = ? ORDER BY operation_type",
            (int(row["id"]),),
        ).fetchall()
        expected_aliases = sorted(
            (
                (alias, normalize_warehouse_name(alias))
                for alias in product.aliases
            ),
            key=lambda value: value[1],
        )
        expected_operations = sorted(
            _operation_value(value) for value in product.allowed_operation_types
        )
        return (
            row["official_name"] == product.official_name
            and row["normalized_name"] == product.normalized_name
            and row["base_unit"] == product.base_unit.value
            and bool(row["is_active"]) is product.is_active
            and [(entry["alias"], entry["normalized_alias"]) for entry in aliases]
            == expected_aliases
            and [entry["operation_type"] for entry in operations]
            == expected_operations
        )

    @staticmethod
    def _existing_namespace(connection: sqlite3.Connection) -> dict[str, str]:
        rows = connection.execute(
            "SELECT normalized_name AS value, stable_id FROM warehouse_products "
            "UNION ALL "
            "SELECT a.normalized_alias, p.stable_id "
            "FROM warehouse_product_aliases a JOIN warehouse_products p "
            "ON p.id = a.product_id"
        ).fetchall()
        namespace: dict[str, str] = {}
        for row in rows:
            value = str(row["value"])
            if value in namespace:
                raise WarehouseSeedImportError(
                    f"Existing warehouse namespace conflict: {value}"
                )
            namespace[value] = str(row["stable_id"])
        return namespace

    def _plan(
        self,
        connection: sqlite3.Connection,
        seed: WarehouseProductsSeed,
    ) -> WarehouseSeedImportPlan:
        namespace = self._existing_namespace(connection)
        creates: list[str] = []
        unchanged: list[str] = []
        conflicts: list[str] = []
        for product in seed.products:
            existing = self._existing_product(connection, product.stable_id)
            if existing is not None:
                if self._matches(connection, existing, product):
                    unchanged.append(product.stable_id)
                else:
                    conflicts.append(
                        f"stable_id {product.stable_id} exists with different data"
                    )
                continue
            claimed = [product.normalized_name]
            claimed.extend(normalize_warehouse_name(v) for v in product.aliases)
            owners = {namespace[name] for name in claimed if name in namespace}
            if owners:
                owner_text = ", ".join(sorted(owners))
                conflicts.append(
                    f"namespace for {product.stable_id} belongs to {owner_text}"
                )
            else:
                creates.append(product.stable_id)
        return WarehouseSeedImportPlan(
            create=tuple(creates),
            unchanged=tuple(unchanged),
            conflicts=tuple(conflicts),
        )

    @staticmethod
    def _insert_products(
        connection: sqlite3.Connection,
        seed: WarehouseProductsSeed,
        create_ids: set[str],
    ) -> None:
        now = _utc_now()
        for product in seed.products:
            if product.stable_id not in create_ids:
                continue
            cursor = connection.execute(
                """
                INSERT INTO warehouse_products (
                    stable_id, official_name, normalized_name, base_unit,
                    is_active, created_at_utc, created_by, updated_at_utc,
                    updated_by, revision
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, NULL, 1)
                """,
                (
                    product.stable_id,
                    product.official_name,
                    product.normalized_name,
                    product.base_unit.value,
                    int(product.is_active),
                    now,
                    now,
                ),
            )
            product_id = int(cursor.lastrowid)
            connection.executemany(
                """
                INSERT INTO warehouse_product_aliases (
                    product_id, alias, normalized_alias, created_at_utc, created_by
                ) VALUES (?, ?, ?, ?, NULL)
                """,
                [
                    (product_id, alias, normalize_warehouse_name(alias), now)
                    for alias in product.aliases
                ],
            )
            connection.executemany(
                "INSERT INTO warehouse_product_operation_types "
                "(product_id, operation_type) VALUES (?, ?)",
                [
                    (product_id, _operation_value(operation))
                    for operation in product.allowed_operation_types
                ],
            )

    @staticmethod
    def _validate_final_namespace(connection: sqlite3.Connection) -> None:
        conflict = connection.execute(
            """
            SELECT normalized_value FROM (
                SELECT normalized_name AS normalized_value FROM warehouse_products
                UNION ALL
                SELECT normalized_alias FROM warehouse_product_aliases
            )
            GROUP BY normalized_value HAVING COUNT(*) > 1 LIMIT 1
            """
        ).fetchone()
        if conflict is not None:
            raise WarehouseSeedImportError(
                f"Final warehouse namespace conflict: {conflict[0]}"
            )
