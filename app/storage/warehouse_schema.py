"""SQLite schema and validation for warehouse nomenclature v3."""

from __future__ import annotations

import re
import sqlite3
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

WAREHOUSE_TABLES = (
    "warehouse_locations",
    "warehouse_products",
    "warehouse_product_aliases",
    "warehouse_product_operation_types",
    "warehouse_settings",
)

WAREHOUSE_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE warehouse_locations (
        id INTEGER PRIMARY KEY,
        stable_id TEXT NOT NULL,
        name TEXT NOT NULL CHECK (trim(name) <> ''),
        normalized_name TEXT NOT NULL CHECK (trim(normalized_name) <> ''),
        is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
        can_be_source INTEGER NOT NULL DEFAULT 1
            CHECK (can_be_source IN (0, 1)),
        can_be_destination INTEGER NOT NULL DEFAULT 1
            CHECK (can_be_destination IN (0, 1)),
        created_at_utc TEXT NOT NULL,
        created_by INTEGER NULL,
        updated_at_utc TEXT NOT NULL,
        updated_by INTEGER NULL,
        revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
        CHECK (can_be_source = 1 OR can_be_destination = 1)
    )
    """,
    """
    CREATE UNIQUE INDEX ux_warehouse_locations_stable_id
    ON warehouse_locations(stable_id)
    """,
    """
    CREATE UNIQUE INDEX ux_warehouse_locations_normalized_name
    ON warehouse_locations(normalized_name)
    """,
    """
    CREATE TABLE warehouse_products (
        id INTEGER PRIMARY KEY,
        stable_id TEXT NOT NULL,
        official_name TEXT NOT NULL CHECK (trim(official_name) <> ''),
        normalized_name TEXT NOT NULL CHECK (trim(normalized_name) <> ''),
        base_unit TEXT NOT NULL CHECK (base_unit IN ('kg', 'l', 'pcs')),
        is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
        created_at_utc TEXT NOT NULL,
        created_by INTEGER NULL,
        updated_at_utc TEXT NOT NULL,
        updated_by INTEGER NULL,
        revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0)
    )
    """,
    """
    CREATE UNIQUE INDEX ux_warehouse_products_stable_id
    ON warehouse_products(stable_id)
    """,
    """
    CREATE UNIQUE INDEX ux_warehouse_products_normalized_name
    ON warehouse_products(normalized_name)
    """,
    """
    CREATE TABLE warehouse_product_aliases (
        id INTEGER PRIMARY KEY,
        product_id INTEGER NOT NULL,
        alias TEXT NOT NULL CHECK (trim(alias) <> ''),
        normalized_alias TEXT NOT NULL CHECK (trim(normalized_alias) <> ''),
        created_at_utc TEXT NOT NULL,
        created_by INTEGER NULL,
        FOREIGN KEY (product_id) REFERENCES warehouse_products(id)
            ON UPDATE RESTRICT ON DELETE CASCADE
    )
    """,
    """
    CREATE UNIQUE INDEX ux_warehouse_product_aliases_normalized
    ON warehouse_product_aliases(normalized_alias)
    """,
    """
    CREATE INDEX ix_warehouse_product_aliases_product
    ON warehouse_product_aliases(product_id)
    """,
    """
    CREATE TABLE warehouse_product_operation_types (
        product_id INTEGER NOT NULL,
        operation_type TEXT NOT NULL
            CHECK (operation_type IN ('product_writeoff', 'transfer')),
        PRIMARY KEY (product_id, operation_type),
        FOREIGN KEY (product_id) REFERENCES warehouse_products(id)
            ON UPDATE RESTRICT ON DELETE CASCADE
    ) WITHOUT ROWID
    """,
    """
    CREATE INDEX ix_warehouse_product_operation_types_type
    ON warehouse_product_operation_types(operation_type)
    """,
    """
    CREATE TABLE warehouse_settings (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        timezone TEXT NOT NULL CHECK (trim(timezone) <> ''),
        cutoff_local_time TEXT NOT NULL
            CHECK (
                length(cutoff_local_time) = 5
                AND substr(cutoff_local_time, 3, 1) = ':'
            ),
        default_source_location_id INTEGER NOT NULL,
        updated_at_utc TEXT NOT NULL,
        updated_by INTEGER NULL,
        FOREIGN KEY (default_source_location_id)
            REFERENCES warehouse_locations(id)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX ix_warehouse_settings_default_source
    ON warehouse_settings(default_source_location_id)
    """,
)

_EXPECTED_COLUMNS = {
    "warehouse_locations": (
        ("id", "INTEGER", 0, None, 1),
        ("stable_id", "TEXT", 1, None, 0),
        ("name", "TEXT", 1, None, 0),
        ("normalized_name", "TEXT", 1, None, 0),
        ("is_active", "INTEGER", 1, "1", 0),
        ("can_be_source", "INTEGER", 1, "1", 0),
        ("can_be_destination", "INTEGER", 1, "1", 0),
        ("created_at_utc", "TEXT", 1, None, 0),
        ("created_by", "INTEGER", 0, None, 0),
        ("updated_at_utc", "TEXT", 1, None, 0),
        ("updated_by", "INTEGER", 0, None, 0),
        ("revision", "INTEGER", 1, "1", 0),
    ),
    "warehouse_products": (
        ("id", "INTEGER", 0, None, 1),
        ("stable_id", "TEXT", 1, None, 0),
        ("official_name", "TEXT", 1, None, 0),
        ("normalized_name", "TEXT", 1, None, 0),
        ("base_unit", "TEXT", 1, None, 0),
        ("is_active", "INTEGER", 1, "1", 0),
        ("created_at_utc", "TEXT", 1, None, 0),
        ("created_by", "INTEGER", 0, None, 0),
        ("updated_at_utc", "TEXT", 1, None, 0),
        ("updated_by", "INTEGER", 0, None, 0),
        ("revision", "INTEGER", 1, "1", 0),
    ),
    "warehouse_product_aliases": (
        ("id", "INTEGER", 0, None, 1),
        ("product_id", "INTEGER", 1, None, 0),
        ("alias", "TEXT", 1, None, 0),
        ("normalized_alias", "TEXT", 1, None, 0),
        ("created_at_utc", "TEXT", 1, None, 0),
        ("created_by", "INTEGER", 0, None, 0),
    ),
    "warehouse_product_operation_types": (
        ("product_id", "INTEGER", 1, None, 1),
        ("operation_type", "TEXT", 1, None, 2),
    ),
    "warehouse_settings": (
        ("id", "INTEGER", 0, None, 1),
        ("timezone", "TEXT", 1, None, 0),
        ("cutoff_local_time", "TEXT", 1, None, 0),
        ("default_source_location_id", "INTEGER", 1, None, 0),
        ("updated_at_utc", "TEXT", 1, None, 0),
        ("updated_by", "INTEGER", 0, None, 0),
    ),
}

_EXPECTED_INDEXES = {
    "warehouse_locations": {
        "ux_warehouse_locations_stable_id": 1,
        "ux_warehouse_locations_normalized_name": 1,
    },
    "warehouse_products": {
        "ux_warehouse_products_stable_id": 1,
        "ux_warehouse_products_normalized_name": 1,
    },
    "warehouse_product_aliases": {
        "ux_warehouse_product_aliases_normalized": 1,
        "ix_warehouse_product_aliases_product": 0,
    },
    "warehouse_product_operation_types": {
        "ix_warehouse_product_operation_types_type": 0,
    },
    "warehouse_settings": {
        "ix_warehouse_settings_default_source": 0,
    },
}

_EXPECTED_FOREIGN_KEYS = {
    "warehouse_locations": set(),
    "warehouse_products": set(),
    "warehouse_product_aliases": {
        ("product_id", "warehouse_products", "id", "RESTRICT", "CASCADE")
    },
    "warehouse_product_operation_types": {
        ("product_id", "warehouse_products", "id", "RESTRICT", "CASCADE")
    },
    "warehouse_settings": {
        (
            "default_source_location_id",
            "warehouse_locations",
            "id",
            "RESTRICT",
            "RESTRICT",
        )
    },
}

_REQUIRED_SQL_FRAGMENTS = {
    "warehouse_locations": (
        "check(is_activein(0,1))",
        "check(can_be_sourcein(0,1))",
        "check(can_be_destinationin(0,1))",
        "check(revision>0)",
        "check(can_be_source=1orcan_be_destination=1)",
    ),
    "warehouse_products": (
        "check(base_unitin('kg','l','pcs'))",
        "check(is_activein(0,1))",
        "check(revision>0)",
    ),
    "warehouse_product_aliases": (),
    "warehouse_product_operation_types": (
        "check(operation_typein('product_writeoff','transfer'))",
    ),
    "warehouse_settings": (
        "check(id=1)",
        "check(length(cutoff_local_time)=5andsubstr(cutoff_local_time,3,1)=':')",
    ),
}


class WarehouseSchemaValidationError(RuntimeError):
    """Raised when the concrete warehouse schema is not the expected v3."""


def create_warehouse_schema(connection: sqlite3.Connection) -> None:
    for statement in WAREHOUSE_SCHEMA_STATEMENTS:
        connection.execute(statement)


def seed_warehouse_defaults(connection: sqlite3.Connection) -> None:
    now_sql = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
    connection.execute(
        f"""
        INSERT OR IGNORE INTO warehouse_locations (
            stable_id, name, normalized_name, is_active,
            can_be_source, can_be_destination,
            created_at_utc, created_by, updated_at_utc, updated_by, revision
        ) VALUES ('bar', 'Бар', 'бар', 1, 1, 1, {now_sql}, NULL,
                  {now_sql}, NULL, 1)
        """
    )
    connection.execute(
        f"""
        INSERT OR IGNORE INTO warehouse_settings (
            id, timezone, cutoff_local_time, default_source_location_id,
            updated_at_utc, updated_by
        )
        SELECT 1, 'Europe/Kyiv', '23:00', id, {now_sql}, NULL
        FROM warehouse_locations
        WHERE stable_id = 'bar'
        """
    )


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _table_sql(connection: sqlite3.Connection, table: str) -> str | None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return str(row[0]) if row is not None and row[0] is not None else None


def _column_signature(
    connection: sqlite3.Connection,
    table: str,
) -> tuple[tuple[str, str, int, str | None, int], ...]:
    rows = connection.execute(
        f"PRAGMA table_info({_quoted_identifier(table)})"
    ).fetchall()
    return tuple(
        (
            str(row[1]),
            str(row[2]).upper(),
            int(row[3]),
            str(row[4]) if row[4] is not None else None,
            int(row[5]),
        )
        for row in rows
    )


def _named_indexes(
    connection: sqlite3.Connection,
    table: str,
) -> dict[str, int]:
    rows = connection.execute(
        f"PRAGMA index_list({_quoted_identifier(table)})"
    ).fetchall()
    return {
        str(row[1]): int(row[2])
        for row in rows
        if not str(row[1]).startswith("sqlite_autoindex_")
    }


def _foreign_keys(
    connection: sqlite3.Connection,
    table: str,
) -> set[tuple[str, str, str, str, str]]:
    rows = connection.execute(
        f"PRAGMA foreign_key_list({_quoted_identifier(table)})"
    ).fetchall()
    return {
        (str(row[3]), str(row[2]), str(row[4]), str(row[5]), str(row[6]))
        for row in rows
    }


def _compact_sql(value: str) -> str:
    compact = re.sub(r"\s+", "", value.casefold())
    return compact.translate(str.maketrans("", "", '"`[]'))


def warehouse_schema_is_absent(connection: sqlite3.Connection) -> bool:
    return all(_table_sql(connection, table) is None for table in WAREHOUSE_TABLES)


def validate_warehouse_schema(connection: sqlite3.Connection) -> None:
    for table in WAREHOUSE_TABLES:
        sql = _table_sql(connection, table)
        if sql is None:
            raise WarehouseSchemaValidationError(
                f"Обязательная таблица {table} отсутствует"
            )
        if _column_signature(connection, table) != _EXPECTED_COLUMNS[table]:
            raise WarehouseSchemaValidationError(
                f"Колонки таблицы {table} не соответствуют schema v3"
            )
        if _named_indexes(connection, table) != _EXPECTED_INDEXES[table]:
            raise WarehouseSchemaValidationError(
                f"Индексы таблицы {table} не соответствуют schema v3"
            )
        if _foreign_keys(connection, table) != _EXPECTED_FOREIGN_KEYS[table]:
            raise WarehouseSchemaValidationError(
                f"Foreign keys таблицы {table} не соответствуют schema v3"
            )
        compact = _compact_sql(sql)
        if any(
            fragment not in compact
            for fragment in _REQUIRED_SQL_FRAGMENTS[table]
        ):
            raise WarehouseSchemaValidationError(
                f"CHECK constraints таблицы {table} не соответствуют schema v3"
            )

    settings = connection.execute(
        """
        SELECT ws.timezone, ws.cutoff_local_time,
               ws.default_source_location_id,
               wl.is_active, wl.can_be_source
        FROM warehouse_settings AS ws
        JOIN warehouse_locations AS wl
          ON wl.id = ws.default_source_location_id
        WHERE ws.id = 1
        """
    ).fetchone()
    if settings is None:
        raise WarehouseSchemaValidationError(
            "Warehouse settings или default source location отсутствуют"
        )
    if not bool(settings[3]) or not bool(settings[4]):
        raise WarehouseSchemaValidationError(
            "Warehouse default source location должна быть активным source"
        )
    if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(settings[1])) is None:
        raise WarehouseSchemaValidationError(
            "Warehouse cutoff_local_time имеет неверный формат"
        )
    try:
        ZoneInfo(str(settings[0]))
    except ZoneInfoNotFoundError as exc:
        raise WarehouseSchemaValidationError(
            "Warehouse timezone не поддерживается системой"
        ) from exc
