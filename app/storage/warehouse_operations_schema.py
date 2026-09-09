"""SQLite schema v4 for warehouse shifts, intake, operations, and audit."""

from __future__ import annotations

import re
import sqlite3

WAREHOUSE_OPERATION_TABLES = (
    "warehouse_shifts",
    "warehouse_intake_batches",
    "warehouse_intake_lines",
    "warehouse_intake_candidates",
    "warehouse_operations",
    "warehouse_audit_events",
)

WAREHOUSE_OPERATION_SCHEMA = (
    """
    CREATE TABLE warehouse_shifts (
        id INTEGER PRIMARY KEY,
        source_location_id INTEGER NOT NULL,
        business_date TEXT NOT NULL CHECK (length(business_date) = 10),
        timezone_snapshot TEXT NOT NULL CHECK (trim(timezone_snapshot) <> ''),
        cutoff_snapshot TEXT NOT NULL CHECK (length(cutoff_snapshot) = 5),
        status TEXT NOT NULL CHECK (status IN (
            'open', 'waiting_required', 'closing', 'closed', 'correction_open'
        )),
        started_at_utc TEXT NOT NULL,
        scheduled_close_at_utc TEXT NOT NULL,
        closed_at_utc TEXT NULL,
        closed_by INTEGER NULL,
        revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
        created_at_utc TEXT NOT NULL,
        updated_at_utc TEXT NOT NULL,
        FOREIGN KEY (source_location_id) REFERENCES warehouse_locations(id)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    )
    """,
    """
    CREATE UNIQUE INDEX ux_warehouse_shifts_source_date
    ON warehouse_shifts(source_location_id, business_date)
    """,
    """
    CREATE INDEX ix_warehouse_shifts_status
    ON warehouse_shifts(status)
    """,
    """
    CREATE TABLE warehouse_intake_batches (
        id INTEGER PRIMARY KEY,
        shift_id INTEGER NOT NULL,
        actor_user_id INTEGER NOT NULL,
        source_chat_id INTEGER NOT NULL,
        source_message_id INTEGER NOT NULL,
        operation_type TEXT NOT NULL
            CHECK (operation_type IN ('writeoff', 'transfer')),
        raw_text TEXT NOT NULL CHECK (trim(raw_text) <> ''),
        status TEXT NOT NULL CHECK (status IN (
            'needs_resolution', 'ready', 'committed', 'cancelled', 'failed'
        )),
        created_at_utc TEXT NOT NULL,
        updated_at_utc TEXT NOT NULL,
        committed_at_utc TEXT NULL,
        FOREIGN KEY (shift_id) REFERENCES warehouse_shifts(id)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    )
    """,
    """
    CREATE UNIQUE INDEX ux_warehouse_batches_message
    ON warehouse_intake_batches(source_chat_id, source_message_id)
    """,
    """
    CREATE INDEX ix_warehouse_batches_shift_status
    ON warehouse_intake_batches(shift_id, status)
    """,
    """
    CREATE TABLE warehouse_intake_lines (
        id INTEGER PRIMARY KEY,
        batch_id INTEGER NOT NULL,
        line_number INTEGER NOT NULL CHECK (line_number > 0),
        raw_line TEXT NOT NULL CHECK (trim(raw_line) <> ''),
        status TEXT NOT NULL CHECK (status IN (
            'parsed', 'ambiguous', 'invalid', 'resolved', 'committed', 'cancelled'
        )),
        product_text TEXT NULL,
        product_id INTEGER NULL,
        input_quantity_text TEXT NULL,
        input_unit_text TEXT NULL,
        quantity_scaled INTEGER NULL CHECK (
            quantity_scaled IS NULL OR quantity_scaled > 0
        ),
        base_unit TEXT NULL CHECK (
            base_unit IS NULL OR base_unit IN ('kg', 'l', 'pcs')
        ),
        reason_text TEXT NULL,
        comment_text TEXT NULL,
        destination_text TEXT NULL,
        destination_location_id INTEGER NULL,
        error_code TEXT NULL,
        created_at_utc TEXT NOT NULL,
        updated_at_utc TEXT NOT NULL,
        FOREIGN KEY (batch_id) REFERENCES warehouse_intake_batches(id)
            ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY (product_id) REFERENCES warehouse_products(id)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY (destination_location_id) REFERENCES warehouse_locations(id)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    )
    """,
    """
    CREATE UNIQUE INDEX ux_warehouse_lines_batch_number
    ON warehouse_intake_lines(batch_id, line_number)
    """,
    """
    CREATE INDEX ix_warehouse_lines_product
    ON warehouse_intake_lines(product_id)
    """,
    """
    CREATE TABLE warehouse_intake_candidates (
        line_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        score_scaled INTEGER NOT NULL CHECK (
            score_scaled >= 0 AND score_scaled <= 10000
        ),
        rank INTEGER NOT NULL CHECK (rank > 0),
        PRIMARY KEY (line_id, product_id),
        FOREIGN KEY (line_id) REFERENCES warehouse_intake_lines(id)
            ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY (product_id) REFERENCES warehouse_products(id)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    ) WITHOUT ROWID
    """,
    """
    CREATE UNIQUE INDEX ux_warehouse_candidates_line_rank
    ON warehouse_intake_candidates(line_id, rank)
    """,
    """
    CREATE TABLE warehouse_operations (
        id INTEGER PRIMARY KEY,
        shift_id INTEGER NOT NULL,
        operation_type TEXT NOT NULL
            CHECK (operation_type IN ('writeoff', 'transfer')),
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
            (operation_type = 'writeoff' AND destination_location_id IS NULL)
            OR
            (operation_type = 'transfer' AND destination_location_id IS NOT NULL)
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
        FOREIGN KEY (product_id) REFERENCES warehouse_products(id)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY (source_location_id) REFERENCES warehouse_locations(id)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY (destination_location_id) REFERENCES warehouse_locations(id)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY (intake_line_id) REFERENCES warehouse_intake_lines(id)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    )
    """,
    """
    CREATE UNIQUE INDEX ux_warehouse_operations_intake_line
    ON warehouse_operations(intake_line_id)
    WHERE intake_line_id IS NOT NULL
    """,
    """
    CREATE INDEX ix_warehouse_operations_shift
    ON warehouse_operations(shift_id, is_deleted, occurred_at_utc)
    """,
    """
    CREATE INDEX ix_warehouse_operations_product
    ON warehouse_operations(product_id)
    """,
    """
    CREATE TABLE warehouse_audit_events (
        id INTEGER PRIMARY KEY,
        shift_id INTEGER NULL,
        operation_id INTEGER NULL,
        actor_kind TEXT NOT NULL CHECK (actor_kind IN ('user', 'system')),
        actor_user_id INTEGER NULL,
        event_type TEXT NOT NULL CHECK (trim(event_type) <> ''),
        entity_type TEXT NOT NULL CHECK (trim(entity_type) <> ''),
        entity_id INTEGER NOT NULL,
        occurred_at_utc TEXT NOT NULL,
        before_json TEXT NULL,
        after_json TEXT NULL,
        reason_text TEXT NULL,
        source_raw_text TEXT NULL,
        correlation_id TEXT NOT NULL CHECK (trim(correlation_id) <> ''),
        FOREIGN KEY (shift_id) REFERENCES warehouse_shifts(id)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY (operation_id) REFERENCES warehouse_operations(id)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX ix_warehouse_audit_shift
    ON warehouse_audit_events(shift_id, occurred_at_utc)
    """,
    """
    CREATE INDEX ix_warehouse_audit_correlation
    ON warehouse_audit_events(correlation_id)
    """,
)


class WarehouseOperationsSchemaError(RuntimeError):
    """Raised when the concrete v4 operations schema is incompatible."""


def _table_sql(connection: sqlite3.Connection, table: str) -> str | None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return str(row[0]) if row is not None and row[0] is not None else None


def create_warehouse_operations_schema(connection: sqlite3.Connection) -> None:
    for statement in WAREHOUSE_OPERATION_SCHEMA:
        connection.execute(statement)


def warehouse_operations_schema_is_absent(connection: sqlite3.Connection) -> bool:
    return all(_table_sql(connection, table) is None for table in WAREHOUSE_OPERATION_TABLES)


def seed_v4_locations(connection: sqlite3.Connection) -> None:
    now_sql = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
    for stable_id, name, normalized in (
        ("kitchen", "Кухня", "кухня"),
        ("confectionery", "Кондитерка", "кондитерка"),
    ):
        connection.execute(
            f"""
            INSERT OR IGNORE INTO warehouse_locations (
                stable_id, name, normalized_name, is_active,
                can_be_source, can_be_destination,
                created_at_utc, created_by, updated_at_utc, updated_by, revision
            ) VALUES (?, ?, ?, 1, 0, 1, {now_sql}, NULL, {now_sql}, NULL, 1)
            """,
            (stable_id, name, normalized),
        )


_EXPECTED_COLUMNS = {
    "warehouse_shifts": (
        "id", "source_location_id", "business_date", "timezone_snapshot",
        "cutoff_snapshot", "status", "started_at_utc", "scheduled_close_at_utc",
        "closed_at_utc", "closed_by", "revision", "created_at_utc", "updated_at_utc",
    ),
    "warehouse_intake_batches": (
        "id", "shift_id", "actor_user_id", "source_chat_id", "source_message_id",
        "operation_type", "raw_text", "status", "created_at_utc", "updated_at_utc",
        "committed_at_utc",
    ),
    "warehouse_intake_lines": (
        "id", "batch_id", "line_number", "raw_line", "status", "product_text",
        "product_id", "input_quantity_text", "input_unit_text", "quantity_scaled",
        "base_unit", "reason_text", "comment_text", "destination_text",
        "destination_location_id", "error_code", "created_at_utc", "updated_at_utc",
    ),
    "warehouse_intake_candidates": (
        "line_id", "product_id", "score_scaled", "rank",
    ),
    "warehouse_operations": (
        "id", "shift_id", "operation_type", "product_id", "product_name_snapshot",
        "quantity_scaled", "base_unit", "input_quantity_text", "input_unit_text",
        "source_location_id", "destination_location_id", "reason_text",
        "comment_text", "raw_text", "author_user_id", "occurred_at_utc",
        "intake_line_id", "origin", "is_deleted", "deleted_at_utc", "deleted_by",
        "deletion_reason", "created_at_utc", "updated_at_utc", "revision",
    ),
    "warehouse_audit_events": (
        "id", "shift_id", "operation_id", "actor_kind", "actor_user_id",
        "event_type", "entity_type", "entity_id", "occurred_at_utc", "before_json",
        "after_json", "reason_text", "source_raw_text", "correlation_id",
    ),
}

_EXPECTED_INDEXES = {
    "warehouse_shifts": {
        "ux_warehouse_shifts_source_date": 1,
        "ix_warehouse_shifts_status": 0,
    },
    "warehouse_intake_batches": {
        "ux_warehouse_batches_message": 1,
        "ix_warehouse_batches_shift_status": 0,
    },
    "warehouse_intake_lines": {
        "ux_warehouse_lines_batch_number": 1,
        "ix_warehouse_lines_product": 0,
    },
    "warehouse_intake_candidates": {"ux_warehouse_candidates_line_rank": 1},
    "warehouse_operations": {
        "ux_warehouse_operations_intake_line": 1,
        "ix_warehouse_operations_shift": 0,
        "ix_warehouse_operations_product": 0,
    },
    "warehouse_audit_events": {
        "ix_warehouse_audit_shift": 0,
        "ix_warehouse_audit_correlation": 0,
    },
}

_REQUIRED_CHECKS = {
    "warehouse_shifts": ("revision>0", "correction_open"),
    "warehouse_intake_batches": ("needs_resolution", "committed", "transfer"),
    "warehouse_intake_lines": ("quantity_scaledisnullorquantity_scaled>0", "ambiguous"),
    "warehouse_intake_candidates": (
        "score_scaled>=0andscore_scaled<=10000",
        "rank>0",
    ),
    "warehouse_operations": (
        "quantity_scaled>0", "revision>0", "required_rule",
        "operation_type='writeoff'anddestination_location_idisnull",
        "operation_type='transfer'anddestination_location_idisnotnull",
    ),
    "warehouse_audit_events": ("actor_kindin('user','system')",),
}

_EXPECTED_FOREIGN_KEYS = {
    "warehouse_shifts": {
        ("source_location_id", "warehouse_locations", "id", "RESTRICT", "RESTRICT"),
    },
    "warehouse_intake_batches": {
        ("shift_id", "warehouse_shifts", "id", "RESTRICT", "RESTRICT"),
    },
    "warehouse_intake_lines": {
        ("batch_id", "warehouse_intake_batches", "id", "RESTRICT", "CASCADE"),
        ("product_id", "warehouse_products", "id", "RESTRICT", "RESTRICT"),
        (
            "destination_location_id", "warehouse_locations", "id",
            "RESTRICT", "RESTRICT",
        ),
    },
    "warehouse_intake_candidates": {
        ("line_id", "warehouse_intake_lines", "id", "RESTRICT", "CASCADE"),
        ("product_id", "warehouse_products", "id", "RESTRICT", "RESTRICT"),
    },
    "warehouse_operations": {
        ("shift_id", "warehouse_shifts", "id", "RESTRICT", "RESTRICT"),
        ("product_id", "warehouse_products", "id", "RESTRICT", "RESTRICT"),
        (
            "source_location_id", "warehouse_locations", "id",
            "RESTRICT", "RESTRICT",
        ),
        (
            "destination_location_id", "warehouse_locations", "id",
            "RESTRICT", "RESTRICT",
        ),
        (
            "intake_line_id", "warehouse_intake_lines", "id",
            "RESTRICT", "RESTRICT",
        ),
    },
    "warehouse_audit_events": {
        ("shift_id", "warehouse_shifts", "id", "RESTRICT", "RESTRICT"),
        (
            "operation_id", "warehouse_operations", "id",
            "RESTRICT", "RESTRICT",
        ),
    },
}


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(
        str(row[1])
        for row in connection.execute(
            f"PRAGMA table_info({_quoted_identifier(table)})"
        ).fetchall()
    )


def _indexes(connection: sqlite3.Connection, table: str) -> dict[str, int]:
    return {
        str(row[1]): int(row[2])
        for row in connection.execute(
            f"PRAGMA index_list({_quoted_identifier(table)})"
        ).fetchall()
        if not str(row[1]).startswith("sqlite_autoindex")
    }


def _foreign_keys(
    connection: sqlite3.Connection,
    table: str,
) -> set[tuple[str, str, str, str, str]]:
    return {
        (str(row[3]), str(row[2]), str(row[4]), str(row[5]), str(row[6]))
        for row in connection.execute(
            f"PRAGMA foreign_key_list({_quoted_identifier(table)})"
        ).fetchall()
    }


def _compact_sql(value: str) -> str:
    return re.sub(r"\s+", "", value.casefold()).translate(
        str.maketrans("", "", '"`[]')
    )


def validate_warehouse_operations_schema(connection: sqlite3.Connection) -> None:
    """Validate concrete v4 tables, columns, indexes, FKs, and CHECK clauses."""

    for table in WAREHOUSE_OPERATION_TABLES:
        table_sql = _table_sql(connection, table)
        if table_sql is None:
            raise WarehouseOperationsSchemaError(f"Missing v4 table: {table}")
        if _columns(connection, table) != _EXPECTED_COLUMNS[table]:
            raise WarehouseOperationsSchemaError(f"Invalid columns: {table}")
        if _indexes(connection, table) != _EXPECTED_INDEXES[table]:
            raise WarehouseOperationsSchemaError(f"Invalid indexes: {table}")
        if _foreign_keys(connection, table) != _EXPECTED_FOREIGN_KEYS[table]:
            raise WarehouseOperationsSchemaError(f"Invalid foreign keys: {table}")
        compact = _compact_sql(table_sql)
        if any(fragment not in compact for fragment in _REQUIRED_CHECKS[table]):
            raise WarehouseOperationsSchemaError(f"Invalid CHECK clauses: {table}")

    rows = connection.execute(
        "SELECT stable_id, name, is_active, can_be_source, can_be_destination "
        "FROM warehouse_locations WHERE stable_id IN ('bar', 'kitchen', "
        "'confectionery') ORDER BY stable_id"
    ).fetchall()
    actual = {
        str(row[0]): (str(row[1]), int(row[2]), int(row[3]), int(row[4]))
        for row in rows
    }
    expected = {
        "bar": ("Бар", 1, 1, 1),
        "kitchen": ("Кухня", 1, 0, 1),
        "confectionery": ("Кондитерка", 1, 0, 1),
    }
    if actual != expected:
        raise WarehouseOperationsSchemaError("Invalid v4 location defaults")
