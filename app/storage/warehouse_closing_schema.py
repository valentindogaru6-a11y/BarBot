"""SQLite schema v5 for required write-offs and shift-closing sessions."""

from __future__ import annotations

import re
import sqlite3

WAREHOUSE_CLOSING_TABLES = (
    "warehouse_required_rules",
    "warehouse_shift_requirements",
    "warehouse_closing_sessions",
)

WAREHOUSE_CLOSING_SCHEMA = (
    """
    CREATE TABLE warehouse_required_rules (
        id INTEGER PRIMARY KEY,
        stable_id TEXT NOT NULL UNIQUE CHECK (trim(stable_id) <> ''),
        product_id INTEGER NOT NULL,
        operation_type TEXT NOT NULL DEFAULT 'writeoff'
            CHECK (operation_type = 'writeoff'),
        quantity_mode TEXT NOT NULL
            CHECK (quantity_mode IN ('recommended', 'manual')),
        suggested_quantity_scaled INTEGER NULL
            CHECK (suggested_quantity_scaled IS NULL
                OR suggested_quantity_scaled > 0),
        base_unit TEXT NOT NULL CHECK (base_unit IN ('kg', 'l', 'pcs')),
        weekdays_mask INTEGER NOT NULL DEFAULT 127
            CHECK (weekdays_mask BETWEEN 1 AND 127),
        question_text TEXT NOT NULL CHECK (
            trim(question_text) <> '' AND length(question_text) <= 500
        ),
        sort_order INTEGER NOT NULL DEFAULT 0,
        allow_not_applicable INTEGER NOT NULL DEFAULT 1
            CHECK (allow_not_applicable IN (0, 1)),
        minimum_scaled INTEGER NULL
            CHECK (minimum_scaled IS NULL OR minimum_scaled > 0),
        maximum_scaled INTEGER NULL
            CHECK (maximum_scaled IS NULL OR maximum_scaled > 0),
        is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
        created_at_utc TEXT NOT NULL,
        created_by INTEGER NULL,
        updated_at_utc TEXT NOT NULL,
        updated_by INTEGER NULL,
        revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
        CHECK (
            (quantity_mode = 'recommended'
                AND suggested_quantity_scaled IS NOT NULL)
            OR
            (quantity_mode = 'manual'
                AND suggested_quantity_scaled IS NULL)
        ),
        CHECK (
            minimum_scaled IS NULL OR maximum_scaled IS NULL
            OR minimum_scaled <= maximum_scaled
        ),
        FOREIGN KEY (product_id) REFERENCES warehouse_products(id)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX ix_warehouse_required_rules_active_order
    ON warehouse_required_rules(is_active, sort_order, id)
    """,
    """
    CREATE TRIGGER trg_warehouse_required_rules_unit_insert
    BEFORE INSERT ON warehouse_required_rules
    WHEN NOT EXISTS (
        SELECT 1 FROM warehouse_products p
        WHERE p.id=NEW.product_id AND p.base_unit=NEW.base_unit
    )
    BEGIN
        SELECT RAISE(ABORT, 'required rule unit does not match product');
    END
    """,
    """
    CREATE TRIGGER trg_warehouse_required_rules_unit_update
    BEFORE UPDATE OF product_id, base_unit ON warehouse_required_rules
    WHEN NOT EXISTS (
        SELECT 1 FROM warehouse_products p
        WHERE p.id=NEW.product_id AND p.base_unit=NEW.base_unit
    )
    BEGIN
        SELECT RAISE(ABORT, 'required rule unit does not match product');
    END
    """,
    """
    CREATE TRIGGER trg_warehouse_products_required_rule_unit
    BEFORE UPDATE OF base_unit ON warehouse_products
    WHEN EXISTS (
        SELECT 1 FROM warehouse_required_rules r
        WHERE r.product_id=NEW.id AND r.base_unit<>NEW.base_unit
    )
    BEGIN
        SELECT RAISE(ABORT, 'product unit is used by required rule');
    END
    """,
    """
    CREATE TABLE warehouse_shift_requirements (
        id INTEGER PRIMARY KEY,
        shift_id INTEGER NOT NULL,
        rule_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        operation_type TEXT NOT NULL CHECK (operation_type = 'writeoff'),
        quantity_mode TEXT NOT NULL
            CHECK (quantity_mode IN ('recommended', 'manual')),
        suggested_quantity_scaled INTEGER NULL
            CHECK (suggested_quantity_scaled IS NULL
                OR suggested_quantity_scaled > 0),
        base_unit TEXT NOT NULL CHECK (base_unit IN ('kg', 'l', 'pcs')),
        question_text TEXT NOT NULL CHECK (
            trim(question_text) <> '' AND length(question_text) <= 500
        ),
        sort_order INTEGER NOT NULL,
        allow_not_applicable INTEGER NOT NULL
            CHECK (allow_not_applicable IN (0, 1)),
        minimum_scaled INTEGER NULL
            CHECK (minimum_scaled IS NULL OR minimum_scaled > 0),
        maximum_scaled INTEGER NULL
            CHECK (maximum_scaled IS NULL OR maximum_scaled > 0),
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'satisfied', 'not_applicable')),
        resolved_at_utc TEXT NULL,
        resolved_by INTEGER NULL,
        resolution_reason TEXT NULL,
        created_at_utc TEXT NOT NULL,
        updated_at_utc TEXT NOT NULL,
        UNIQUE (shift_id, rule_id),
        CHECK (
            (quantity_mode = 'recommended'
                AND suggested_quantity_scaled IS NOT NULL)
            OR
            (quantity_mode = 'manual'
                AND suggested_quantity_scaled IS NULL)
        ),
        CHECK (
            minimum_scaled IS NULL OR maximum_scaled IS NULL
            OR minimum_scaled <= maximum_scaled
        ),
        CHECK (
            (status = 'pending' AND resolved_at_utc IS NULL
                AND resolved_by IS NULL AND resolution_reason IS NULL)
            OR
            (status = 'satisfied' AND resolved_at_utc IS NOT NULL
                AND resolved_by IS NOT NULL)
            OR
            (status = 'not_applicable' AND resolved_at_utc IS NOT NULL
                AND resolved_by IS NOT NULL
                AND trim(resolution_reason) <> '')
        ),
        FOREIGN KEY (shift_id) REFERENCES warehouse_shifts(id)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY (rule_id) REFERENCES warehouse_required_rules(id)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY (product_id) REFERENCES warehouse_products(id)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX ix_warehouse_shift_requirements_status
    ON warehouse_shift_requirements(shift_id, status, sort_order, id)
    """,
    """
    CREATE TABLE warehouse_closing_sessions (
        id INTEGER PRIMARY KEY,
        shift_id INTEGER NOT NULL,
        initiated_by INTEGER NOT NULL,
        trigger_type TEXT NOT NULL
            CHECK (trigger_type IN ('manual', 'scheduled', 'recovery')),
        status TEXT NOT NULL CHECK (status IN (
            'collecting', 'ready', 'committing', 'completed', 'cancelled'
        )),
        current_requirement_id INTEGER NULL,
        created_at_utc TEXT NOT NULL,
        last_activity_at_utc TEXT NOT NULL,
        completed_at_utc TEXT NULL,
        revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
        CHECK (
            (status = 'completed' AND completed_at_utc IS NOT NULL)
            OR (status <> 'completed' AND completed_at_utc IS NULL)
        ),
        FOREIGN KEY (shift_id) REFERENCES warehouse_shifts(id)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY (current_requirement_id)
            REFERENCES warehouse_shift_requirements(id)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    )
    """,
    """
    CREATE UNIQUE INDEX ux_warehouse_closing_sessions_active
    ON warehouse_closing_sessions(shift_id)
    WHERE status IN ('collecting', 'ready', 'committing')
    """,
    """
    CREATE INDEX ix_warehouse_closing_sessions_shift
    ON warehouse_closing_sessions(shift_id, created_at_utc)
    """,
)


class WarehouseClosingSchemaError(RuntimeError):
    """Raised when the concrete v5 closing schema is incompatible."""


def _table_sql(connection: sqlite3.Connection, table: str) -> str | None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return str(row[0]) if row is not None and row[0] is not None else None


def create_warehouse_closing_schema(connection: sqlite3.Connection) -> None:
    for statement in WAREHOUSE_CLOSING_SCHEMA:
        connection.execute(statement)


def warehouse_closing_schema_is_absent(connection: sqlite3.Connection) -> bool:
    return all(_table_sql(connection, table) is None for table in WAREHOUSE_CLOSING_TABLES)


_EXPECTED_COLUMNS = {
    "warehouse_required_rules": (
        "id", "stable_id", "product_id", "operation_type", "quantity_mode",
        "suggested_quantity_scaled", "base_unit", "weekdays_mask",
        "question_text", "sort_order", "allow_not_applicable",
        "minimum_scaled", "maximum_scaled", "is_active", "created_at_utc",
        "created_by", "updated_at_utc", "updated_by", "revision",
    ),
    "warehouse_shift_requirements": (
        "id", "shift_id", "rule_id", "product_id", "operation_type",
        "quantity_mode", "suggested_quantity_scaled", "base_unit",
        "question_text", "sort_order", "allow_not_applicable",
        "minimum_scaled", "maximum_scaled", "status", "resolved_at_utc",
        "resolved_by", "resolution_reason", "created_at_utc", "updated_at_utc",
    ),
    "warehouse_closing_sessions": (
        "id", "shift_id", "initiated_by", "trigger_type", "status",
        "current_requirement_id", "created_at_utc", "last_activity_at_utc",
        "completed_at_utc", "revision",
    ),
}

_EXPECTED_INDEXES = {
    "warehouse_required_rules": {"ix_warehouse_required_rules_active_order": 0},
    "warehouse_shift_requirements": {"ix_warehouse_shift_requirements_status": 0},
    "warehouse_closing_sessions": {
        "ux_warehouse_closing_sessions_active": 1,
        "ix_warehouse_closing_sessions_shift": 0,
    },
}

_EXPECTED_TRIGGERS = {
    "trg_warehouse_required_rules_unit_insert",
    "trg_warehouse_required_rules_unit_update",
    "trg_warehouse_products_required_rule_unit",
}

_EXPECTED_FOREIGN_KEYS = {
    "warehouse_required_rules": {
        ("product_id", "warehouse_products", "id", "RESTRICT", "RESTRICT"),
    },
    "warehouse_shift_requirements": {
        ("shift_id", "warehouse_shifts", "id", "RESTRICT", "RESTRICT"),
        ("rule_id", "warehouse_required_rules", "id", "RESTRICT", "RESTRICT"),
        ("product_id", "warehouse_products", "id", "RESTRICT", "RESTRICT"),
    },
    "warehouse_closing_sessions": {
        ("shift_id", "warehouse_shifts", "id", "RESTRICT", "RESTRICT"),
        (
            "current_requirement_id", "warehouse_shift_requirements", "id",
            "RESTRICT", "RESTRICT",
        ),
    },
}

_REQUIRED_CHECKS = {
    "warehouse_required_rules": (
        "operation_type='writeoff'", "weekdays_maskbetween1and127",
        "revision>0", "quantity_mode='recommended'",
        "quantity_mode='manual'", "minimum_scaled<=maximum_scaled",
    ),
    "warehouse_shift_requirements": (
        "statusin('pending','satisfied','not_applicable')",
        "status='pending'", "status='not_applicable'",
        "quantity_mode='recommended'", "minimum_scaled<=maximum_scaled",
    ),
    "warehouse_closing_sessions": (
        "trigger_typein('manual','scheduled','recovery')",
        "statusin('collecting','ready','committing','completed','cancelled')",
        "revision>0", "status='completed'",
    ),
}


def _columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")'))


def _indexes(connection: sqlite3.Connection, table: str) -> dict[str, int]:
    return {
        str(row[1]): int(row[2])
        for row in connection.execute(f'PRAGMA index_list("{table}")')
        if not str(row[1]).startswith("sqlite_autoindex")
    }


def _foreign_keys(
    connection: sqlite3.Connection, table: str
) -> set[tuple[str, str, str, str, str]]:
    return {
        (str(row[3]), str(row[2]), str(row[4]), str(row[5]), str(row[6]))
        for row in connection.execute(f'PRAGMA foreign_key_list("{table}")')
    }


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value.casefold()).translate(
        str.maketrans("", "", '"`[]')
    )


def validate_warehouse_closing_schema(connection: sqlite3.Connection) -> None:
    """Validate v5 tables, indexes, foreign keys, and key CHECK clauses."""

    for table in WAREHOUSE_CLOSING_TABLES:
        sql = _table_sql(connection, table)
        if sql is None:
            raise WarehouseClosingSchemaError(f"Missing v5 table: {table}")
        if _columns(connection, table) != _EXPECTED_COLUMNS[table]:
            raise WarehouseClosingSchemaError(f"Invalid columns: {table}")
        if _indexes(connection, table) != _EXPECTED_INDEXES[table]:
            raise WarehouseClosingSchemaError(f"Invalid indexes: {table}")
        if _foreign_keys(connection, table) != _EXPECTED_FOREIGN_KEYS[table]:
            raise WarehouseClosingSchemaError(f"Invalid foreign keys: {table}")
        compact = _compact(sql)
        if any(value not in compact for value in _REQUIRED_CHECKS[table]):
            raise WarehouseClosingSchemaError(f"Invalid CHECK clauses: {table}")

    index = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' "
        "AND name='ux_warehouse_closing_sessions_active'"
    ).fetchone()
    if index is None or "wherestatusin('collecting','ready','committing')" not in _compact(str(index[0])):
        raise WarehouseClosingSchemaError("Invalid active closing-session index")
    triggers = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'trg_warehouse_%required_rule%'"
        )
    }
    if triggers != _EXPECTED_TRIGGERS:
        raise WarehouseClosingSchemaError("Invalid required-rule unit triggers")
