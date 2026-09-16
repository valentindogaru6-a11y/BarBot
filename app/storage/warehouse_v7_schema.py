"""Schema-v7 replacement migration for warehouse development operations."""

from __future__ import annotations

import re
import sqlite3

from app.storage.warehouse_operations_schema import (
    WarehouseOperationsSchemaError,
    validate_warehouse_operations_schema,
)
from app.storage.warehouse_reports_schema import (
    WarehouseReportsSchemaError,
    validate_warehouse_reports_schema,
)
from app.storage.warehouse_schema import (
    WarehouseSchemaValidationError,
    validate_warehouse_schema,
)

DEVELOPMENT_DRAFT_TABLE = "warehouse_development_title_drafts"

_PRODUCT_OPERATION_TYPES_SQL = """
CREATE TABLE __warehouse_product_operation_types_v7 (
    product_id INTEGER NOT NULL,
    operation_type TEXT NOT NULL CHECK (
        operation_type IN ('product_writeoff', 'transfer', 'development')
    ),
    PRIMARY KEY (product_id, operation_type),
    FOREIGN KEY (product_id) REFERENCES warehouse_products(id)
        ON UPDATE RESTRICT ON DELETE CASCADE
) WITHOUT ROWID
"""

_INTAKE_BATCHES_SQL = """
CREATE TABLE __warehouse_intake_batches_v7 (
    id INTEGER PRIMARY KEY,
    shift_id INTEGER NOT NULL,
    actor_user_id INTEGER NOT NULL,
    source_chat_id INTEGER NOT NULL,
    source_message_id INTEGER NOT NULL,
    operation_type TEXT NOT NULL CHECK (
        operation_type IN ('writeoff', 'transfer', 'development')
    ),
    development_title TEXT NULL,
    raw_text TEXT NOT NULL CHECK (trim(raw_text) <> ''),
    status TEXT NOT NULL CHECK (status IN (
        'needs_resolution', 'ready', 'committed', 'cancelled', 'failed'
    )),
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    committed_at_utc TEXT NULL,
    CHECK (
        (operation_type = 'development'
            AND development_title IS NOT NULL
            AND trim(development_title) <> ''
            AND length(development_title) <= 200)
        OR
        (operation_type IN ('writeoff', 'transfer')
            AND development_title IS NULL)
    ),
    FOREIGN KEY (shift_id) REFERENCES warehouse_shifts(id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
)
"""

_OPERATIONS_SQL = """
CREATE TABLE __warehouse_operations_v7 (
    id INTEGER PRIMARY KEY,
    shift_id INTEGER NOT NULL,
    operation_type TEXT NOT NULL CHECK (
        operation_type IN ('writeoff', 'transfer', 'development')
    ),
    development_title TEXT NULL,
    development_group_id INTEGER NULL,
    product_id INTEGER NOT NULL,
    product_name_snapshot TEXT NOT NULL,
    quantity_scaled INTEGER NOT NULL CHECK (quantity_scaled > 0),
    base_unit TEXT NOT NULL CHECK (base_unit IN ('kg', 'l', 'pcs')),
    input_quantity_text TEXT NOT NULL,
    input_unit_text TEXT NULL,
    source_location_id INTEGER NOT NULL,
    destination_location_id INTEGER NULL,
    reason_text TEXT NULL,
    comment_text TEXT NULL,
    raw_text TEXT NOT NULL,
    author_user_id INTEGER NOT NULL,
    occurred_at_utc TEXT NOT NULL,
    intake_line_id INTEGER NULL,
    origin TEXT NOT NULL CHECK (
        origin IN ('message', 'manual', 'required_rule')
    ),
    is_deleted INTEGER NOT NULL DEFAULT 0 CHECK (is_deleted IN (0, 1)),
    deleted_at_utc TEXT NULL,
    deleted_by INTEGER NULL,
    deletion_reason TEXT NULL,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    CHECK (
        (operation_type = 'writeoff'
            AND destination_location_id IS NULL
            AND development_title IS NULL
            AND development_group_id IS NULL)
        OR
        (operation_type = 'transfer'
            AND destination_location_id IS NOT NULL
            AND development_title IS NULL
            AND development_group_id IS NULL)
        OR
        (operation_type = 'development'
            AND destination_location_id IS NULL
            AND development_title IS NOT NULL
            AND trim(development_title) <> ''
            AND length(development_title) <= 200
            AND development_group_id IS NOT NULL)
    ),
    CHECK (
        destination_location_id IS NULL
        OR destination_location_id <> source_location_id
    ),
    CHECK (
        (is_deleted = 0 AND deleted_at_utc IS NULL AND deleted_by IS NULL
            AND deletion_reason IS NULL)
        OR
        (is_deleted = 1 AND deleted_at_utc IS NOT NULL
            AND deleted_by IS NOT NULL AND trim(deletion_reason) <> '')
    ),
    FOREIGN KEY (shift_id) REFERENCES warehouse_shifts(id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (development_group_id) REFERENCES warehouse_intake_batches(id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (product_id) REFERENCES warehouse_products(id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (source_location_id) REFERENCES warehouse_locations(id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (destination_location_id) REFERENCES warehouse_locations(id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (intake_line_id) REFERENCES warehouse_intake_lines(id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
)
"""

_REPORT_LINES_SQL = """
CREATE TABLE __warehouse_report_lines_v7 (
    id INTEGER PRIMARY KEY,
    report_id INTEGER NOT NULL,
    sheet_type TEXT NOT NULL CHECK (
        sheet_type IN ('writeoff', 'transfer', 'development')
    ),
    row_number INTEGER NOT NULL CHECK (row_number > 0),
    product_id INTEGER NOT NULL,
    product_name TEXT NOT NULL CHECK (trim(product_name) <> ''),
    quantity_scaled INTEGER NOT NULL CHECK (quantity_scaled > 0),
    base_unit TEXT NOT NULL CHECK (base_unit IN ('kg', 'l', 'pcs')),
    reason_or_comment TEXT NULL,
    source_location_name TEXT NOT NULL CHECK (trim(source_location_name) <> ''),
    destination_location_name TEXT NULL,
    development_title TEXT NULL,
    development_group_id INTEGER NULL,
    business_date TEXT NOT NULL CHECK (length(business_date) = 10),
    UNIQUE (report_id, sheet_type, row_number),
    CHECK (
        (sheet_type = 'writeoff'
            AND destination_location_name IS NULL
            AND development_title IS NULL
            AND development_group_id IS NULL)
        OR
        (sheet_type = 'transfer'
            AND destination_location_name IS NOT NULL
            AND trim(destination_location_name) <> ''
            AND development_title IS NULL
            AND development_group_id IS NULL)
        OR
        (sheet_type = 'development'
            AND destination_location_name IS NULL
            AND development_title IS NOT NULL
            AND trim(development_title) <> ''
            AND length(development_title) <= 200
            AND development_group_id IS NOT NULL)
    ),
    FOREIGN KEY (report_id) REFERENCES warehouse_reports(id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (product_id) REFERENCES warehouse_products(id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
)
"""

_TITLE_DRAFT_SQL = """
CREATE TABLE warehouse_development_title_drafts (
    id INTEGER PRIMARY KEY,
    shift_id INTEGER NOT NULL,
    actor_user_id INTEGER NOT NULL,
    source_chat_id INTEGER NOT NULL,
    source_message_id INTEGER NOT NULL,
    raw_text TEXT NOT NULL CHECK (trim(raw_text) <> ''),
    development_title TEXT NULL,
    ingredients_text TEXT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('needs_title', 'needs_ingredients', 'promoted', 'cancelled')
    ),
    batch_id INTEGER NULL,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    CHECK (
        (status = 'needs_title'
            AND development_title IS NULL
            AND ingredients_text IS NOT NULL
            AND trim(ingredients_text) <> ''
            AND batch_id IS NULL)
        OR
        (status = 'needs_ingredients'
            AND development_title IS NOT NULL
            AND trim(development_title) <> ''
            AND length(development_title) <= 200
            AND ingredients_text IS NULL
            AND batch_id IS NULL)
        OR
        (status = 'promoted'
            AND development_title IS NOT NULL
            AND trim(development_title) <> ''
            AND length(development_title) <= 200
            AND ingredients_text IS NOT NULL
            AND trim(ingredients_text) <> ''
            AND batch_id IS NOT NULL)
        OR
        (status = 'cancelled' AND batch_id IS NULL)
    ),
    FOREIGN KEY (shift_id) REFERENCES warehouse_shifts(id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (batch_id) REFERENCES warehouse_intake_batches(id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
)
"""


class WarehouseV7SchemaError(RuntimeError):
    """Raised when development schema or a replacement is inconsistent."""


def warehouse_v7_schema_is_absent(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (DEVELOPMENT_DRAFT_TABLE,),
    ).fetchone()
    return row is None


def _copy_and_verify(
    connection: sqlite3.Connection,
    *,
    old_table: str,
    new_table: str,
    old_columns: tuple[str, ...],
    new_select: str,
) -> None:
    columns = ", ".join(old_columns)
    old_count = int(connection.execute(f"SELECT COUNT(*) FROM {old_table}").fetchone()[0])
    connection.execute(
        f"INSERT INTO {new_table} ({new_select.split(' FROM ', 1)[0]}) "
        f"SELECT {new_select}"
    )
    new_count = int(connection.execute(f"SELECT COUNT(*) FROM {new_table}").fetchone()[0])
    if new_count != old_count:
        raise WarehouseV7SchemaError(f"Row count changed for {old_table}")
    for left, right in ((old_table, new_table), (new_table, old_table)):
        missing = connection.execute(
            f"SELECT 1 FROM (SELECT {columns} FROM {left} "
            f"EXCEPT SELECT {columns} FROM {right}) LIMIT 1"
        ).fetchone()
        if missing is not None:
            raise WarehouseV7SchemaError(f"Rows changed for {old_table}")


def migrate_warehouse_v6_to_v7(connection: sqlite3.Connection) -> None:
    """Replace constrained tables while preserving every v6 row exactly."""

    connection.execute(_PRODUCT_OPERATION_TYPES_SQL)
    product_columns = ("product_id", "operation_type")
    _copy_and_verify(
        connection,
        old_table="warehouse_product_operation_types",
        new_table="__warehouse_product_operation_types_v7",
        old_columns=product_columns,
        new_select="product_id, operation_type FROM warehouse_product_operation_types",
    )
    connection.execute(
        "INSERT INTO __warehouse_product_operation_types_v7 "
        "(product_id, operation_type) "
        "SELECT id, 'development' FROM warehouse_products WHERE is_active=1"
    )

    connection.execute(_INTAKE_BATCHES_SQL)
    batch_columns = (
        "id", "shift_id", "actor_user_id", "source_chat_id", "source_message_id",
        "operation_type", "raw_text", "status", "created_at_utc", "updated_at_utc",
        "committed_at_utc",
    )
    batch_insert = ", ".join(batch_columns[:6] + ("development_title",) + batch_columns[6:])
    connection.execute(
        f"INSERT INTO __warehouse_intake_batches_v7 ({batch_insert}) "
        f"SELECT {', '.join(batch_columns[:6])}, NULL, "
        f"{', '.join(batch_columns[6:])} FROM warehouse_intake_batches"
    )
    _verify_preserved(connection, "warehouse_intake_batches", "__warehouse_intake_batches_v7", batch_columns)

    connection.execute(_OPERATIONS_SQL)
    operation_columns = (
        "id", "shift_id", "operation_type", "product_id", "product_name_snapshot",
        "quantity_scaled", "base_unit", "input_quantity_text", "input_unit_text",
        "source_location_id", "destination_location_id", "reason_text", "comment_text",
        "raw_text", "author_user_id", "occurred_at_utc", "intake_line_id", "origin",
        "is_deleted", "deleted_at_utc", "deleted_by", "deletion_reason",
        "created_at_utc", "updated_at_utc", "revision",
    )
    operation_insert = ", ".join(
        operation_columns[:3]
        + ("development_title", "development_group_id")
        + operation_columns[3:]
    )
    connection.execute(
        f"INSERT INTO __warehouse_operations_v7 ({operation_insert}) "
        f"SELECT {', '.join(operation_columns[:3])}, NULL, NULL, "
        f"{', '.join(operation_columns[3:])} FROM warehouse_operations"
    )
    _verify_preserved(connection, "warehouse_operations", "__warehouse_operations_v7", operation_columns)

    connection.execute(_REPORT_LINES_SQL)
    report_columns = (
        "id", "report_id", "sheet_type", "row_number", "product_id", "product_name",
        "quantity_scaled", "base_unit", "reason_or_comment", "source_location_name",
        "destination_location_name", "business_date",
    )
    report_insert = ", ".join(
        report_columns[:-1]
        + ("development_title", "development_group_id", report_columns[-1])
    )
    connection.execute(
        f"INSERT INTO __warehouse_report_lines_v7 ({report_insert}) "
        f"SELECT {', '.join(report_columns[:-1])}, NULL, NULL, business_date "
        "FROM warehouse_report_lines"
    )
    _verify_preserved(connection, "warehouse_report_lines", "__warehouse_report_lines_v7", report_columns)

    connection.execute("DROP TABLE warehouse_product_operation_types")
    connection.execute("ALTER TABLE __warehouse_product_operation_types_v7 RENAME TO warehouse_product_operation_types")
    connection.execute("DROP TABLE warehouse_operations")
    connection.execute("ALTER TABLE __warehouse_operations_v7 RENAME TO warehouse_operations")
    connection.execute("DROP TABLE warehouse_intake_batches")
    connection.execute("ALTER TABLE __warehouse_intake_batches_v7 RENAME TO warehouse_intake_batches")
    connection.execute("DROP TABLE warehouse_report_lines")
    connection.execute("ALTER TABLE __warehouse_report_lines_v7 RENAME TO warehouse_report_lines")

    for statement in _V7_INDEXES_AND_TRIGGERS:
        connection.execute(statement)
    connection.execute(_TITLE_DRAFT_SQL)
    connection.execute(
        "CREATE UNIQUE INDEX ux_warehouse_development_drafts_message "
        "ON warehouse_development_title_drafts(source_chat_id, source_message_id)"
    )
    connection.execute(
        "CREATE INDEX ix_warehouse_development_drafts_actor_status "
        "ON warehouse_development_title_drafts(actor_user_id, status)"
    )
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise WarehouseV7SchemaError("Foreign key check failed after v7 replacement")


def _verify_preserved(
    connection: sqlite3.Connection,
    old_table: str,
    new_table: str,
    columns: tuple[str, ...],
) -> None:
    column_sql = ", ".join(columns)
    old_count = int(connection.execute(f"SELECT COUNT(*) FROM {old_table}").fetchone()[0])
    new_count = int(connection.execute(f"SELECT COUNT(*) FROM {new_table}").fetchone()[0])
    if old_count != new_count:
        raise WarehouseV7SchemaError(f"Row count changed for {old_table}")
    for left, right in ((old_table, new_table), (new_table, old_table)):
        row = connection.execute(
            f"SELECT 1 FROM (SELECT {column_sql} FROM {left} "
            f"EXCEPT SELECT {column_sql} FROM {right}) LIMIT 1"
        ).fetchone()
        if row is not None:
            raise WarehouseV7SchemaError(f"Rows changed for {old_table}")


_V7_INDEXES_AND_TRIGGERS = (
    "CREATE INDEX ix_warehouse_product_operation_types_type ON warehouse_product_operation_types(operation_type)",
    "CREATE UNIQUE INDEX ux_warehouse_batches_message ON warehouse_intake_batches(source_chat_id, source_message_id)",
    "CREATE INDEX ix_warehouse_batches_shift_status ON warehouse_intake_batches(shift_id, status)",
    "CREATE UNIQUE INDEX ux_warehouse_operations_intake_line ON warehouse_operations(intake_line_id) WHERE intake_line_id IS NOT NULL",
    "CREATE INDEX ix_warehouse_operations_shift ON warehouse_operations(shift_id, is_deleted, occurred_at_utc)",
    "CREATE INDEX ix_warehouse_operations_product ON warehouse_operations(product_id)",
    "CREATE INDEX ix_warehouse_operations_development_group ON warehouse_operations(development_group_id) WHERE development_group_id IS NOT NULL",
    "CREATE INDEX ix_warehouse_report_lines_report_sheet ON warehouse_report_lines(report_id, sheet_type, row_number)",
    """
    CREATE TRIGGER trg_warehouse_report_lines_building_insert
    BEFORE INSERT ON warehouse_report_lines
    WHEN NOT EXISTS (
        SELECT 1 FROM warehouse_reports r
        WHERE r.id=NEW.report_id AND r.status='building'
    )
    BEGIN SELECT RAISE(ABORT, 'report snapshot is not building'); END
    """,
    """
    CREATE TRIGGER trg_warehouse_report_lines_immutable_update
    BEFORE UPDATE ON warehouse_report_lines
    BEGIN SELECT RAISE(ABORT, 'report snapshot lines are immutable'); END
    """,
    """
    CREATE TRIGGER trg_warehouse_report_lines_immutable_delete
    BEFORE DELETE ON warehouse_report_lines
    BEGIN SELECT RAISE(ABORT, 'report snapshot lines are immutable'); END
    """,
)


def validate_warehouse_v7_schema(
    connection: sqlite3.Connection,
    *,
    allow_correction: bool = False,
) -> None:
    try:
        validate_warehouse_schema(connection, allow_development=True)
        validate_warehouse_operations_schema(connection, allow_development=True)
        validate_warehouse_reports_schema(
            connection,
            allow_development=True,
            allow_correction=allow_correction,
        )
    except (
        WarehouseSchemaValidationError,
        WarehouseOperationsSchemaError,
        WarehouseReportsSchemaError,
    ) as exc:
        raise WarehouseV7SchemaError(str(exc)) from exc

    sql_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (DEVELOPMENT_DRAFT_TABLE,),
    ).fetchone()
    if sql_row is None:
        raise WarehouseV7SchemaError("Missing development title draft table")
    columns = tuple(
        str(row[1])
        for row in connection.execute(
            "PRAGMA table_info(warehouse_development_title_drafts)"
        )
    )
    if columns != (
        "id", "shift_id", "actor_user_id", "source_chat_id", "source_message_id",
        "raw_text", "development_title", "ingredients_text", "status", "batch_id", "created_at_utc",
        "updated_at_utc",
    ):
        raise WarehouseV7SchemaError("Invalid development title draft columns")
    indexes = {
        str(row[1]): int(row[2])
        for row in connection.execute(
            "PRAGMA index_list(warehouse_development_title_drafts)"
        )
        if not str(row[1]).startswith("sqlite_autoindex")
    }
    if indexes != {
        "ux_warehouse_development_drafts_message": 1,
        "ix_warehouse_development_drafts_actor_status": 0,
    }:
        raise WarehouseV7SchemaError("Invalid development title draft indexes")
    foreign_keys = {
        (str(row[3]), str(row[2]), str(row[4]), str(row[5]), str(row[6]))
        for row in connection.execute(
            "PRAGMA foreign_key_list(warehouse_development_title_drafts)"
        )
    }
    if foreign_keys != {
        ("shift_id", "warehouse_shifts", "id", "RESTRICT", "RESTRICT"),
        (
            "batch_id",
            "warehouse_intake_batches",
            "id",
            "RESTRICT",
            "RESTRICT",
        ),
    }:
        raise WarehouseV7SchemaError("Invalid development title draft foreign keys")
    compact = re.sub(r"\s+", "", str(sql_row[0]).casefold())
    if any(
        fragment not in compact
        for fragment in (
            "statusin('needs_title','needs_ingredients','promoted','cancelled')",
            "status='needs_title'",
            "status='needs_ingredients'",
            "status='promoted'",
            "length(development_title)<=200",
        )
    ):
        raise WarehouseV7SchemaError("Invalid development title draft checks")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise WarehouseV7SchemaError("Foreign key check failed for schema v7")
