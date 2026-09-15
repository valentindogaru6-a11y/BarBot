"""SQLite schema v6 for immutable warehouse XLSX report snapshots."""

from __future__ import annotations

import re
import sqlite3

WAREHOUSE_REPORT_TABLES = (
    "warehouse_reports",
    "warehouse_report_lines",
)

WAREHOUSE_REPORT_SCHEMA = (
    """
    CREATE TABLE warehouse_reports (
        id INTEGER PRIMARY KEY,
        shift_id INTEGER NOT NULL,
        version INTEGER NOT NULL CHECK (version > 0),
        report_kind TEXT NOT NULL CHECK (report_kind = 'final'),
        status TEXT NOT NULL CHECK (
            status IN ('building', 'ready', 'failed', 'superseded')
        ),
        relative_file_path TEXT NULL
            CHECK (relative_file_path IS NULL OR trim(relative_file_path) <> ''),
        file_sha256 TEXT NULL CHECK (
            file_sha256 IS NULL
            OR (length(file_sha256) = 64 AND file_sha256 NOT GLOB '*[^0-9a-f]*')
        ),
        file_size_bytes INTEGER NULL
            CHECK (file_size_bytes IS NULL OR file_size_bytes > 0),
        created_at_utc TEXT NOT NULL,
        created_by INTEGER NOT NULL,
        ready_at_utc TEXT NULL,
        failure_code TEXT NULL
            CHECK (failure_code IS NULL OR trim(failure_code) <> ''),
        supersedes_report_id INTEGER NULL,
        correction_reason TEXT NULL
            CHECK (correction_reason IS NULL OR trim(correction_reason) <> ''),
        UNIQUE (shift_id, version),
        CHECK (
            (status = 'building'
                AND relative_file_path IS NULL
                AND file_sha256 IS NULL
                AND file_size_bytes IS NULL
                AND ready_at_utc IS NULL
                AND failure_code IS NULL)
            OR
            (status = 'failed'
                AND relative_file_path IS NULL
                AND file_sha256 IS NULL
                AND file_size_bytes IS NULL
                AND ready_at_utc IS NULL
                AND failure_code IS NOT NULL)
            OR
            (status IN ('ready', 'superseded')
                AND relative_file_path IS NOT NULL
                AND file_sha256 IS NOT NULL
                AND file_size_bytes IS NOT NULL
                AND ready_at_utc IS NOT NULL
                AND failure_code IS NULL)
        ),
        CHECK (
            (supersedes_report_id IS NULL AND correction_reason IS NULL)
            OR
            (supersedes_report_id IS NOT NULL AND correction_reason IS NOT NULL)
        ),
        FOREIGN KEY (shift_id) REFERENCES warehouse_shifts(id)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY (supersedes_report_id) REFERENCES warehouse_reports(id)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    )
    """,
    """
    CREATE UNIQUE INDEX ux_warehouse_reports_building
    ON warehouse_reports(shift_id)
    WHERE status = 'building'
    """,
    """
    CREATE UNIQUE INDEX ux_warehouse_reports_file_path
    ON warehouse_reports(relative_file_path)
    WHERE relative_file_path IS NOT NULL
    """,
    """
    CREATE INDEX ix_warehouse_reports_history
    ON warehouse_reports(status, shift_id, version)
    """,
    """
    CREATE TABLE warehouse_report_lines (
        id INTEGER PRIMARY KEY,
        report_id INTEGER NOT NULL,
        sheet_type TEXT NOT NULL
            CHECK (sheet_type IN ('writeoff', 'transfer')),
        row_number INTEGER NOT NULL CHECK (row_number > 0),
        product_id INTEGER NOT NULL,
        product_name TEXT NOT NULL CHECK (trim(product_name) <> ''),
        quantity_scaled INTEGER NOT NULL CHECK (quantity_scaled > 0),
        base_unit TEXT NOT NULL CHECK (base_unit IN ('kg', 'l', 'pcs')),
        reason_or_comment TEXT NULL,
        source_location_name TEXT NOT NULL
            CHECK (trim(source_location_name) <> ''),
        destination_location_name TEXT NULL,
        business_date TEXT NOT NULL CHECK (length(business_date) = 10),
        UNIQUE (report_id, sheet_type, row_number),
        CHECK (
            (sheet_type = 'writeoff' AND destination_location_name IS NULL)
            OR
            (sheet_type = 'transfer'
                AND destination_location_name IS NOT NULL
                AND trim(destination_location_name) <> '')
        ),
        FOREIGN KEY (report_id) REFERENCES warehouse_reports(id)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY (product_id) REFERENCES warehouse_products(id)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX ix_warehouse_report_lines_report_sheet
    ON warehouse_report_lines(report_id, sheet_type, row_number)
    """,
    """
    CREATE TRIGGER trg_warehouse_report_lines_building_insert
    BEFORE INSERT ON warehouse_report_lines
    WHEN NOT EXISTS (
        SELECT 1 FROM warehouse_reports r
        WHERE r.id=NEW.report_id AND r.status='building'
    )
    BEGIN
        SELECT RAISE(ABORT, 'report snapshot is not building');
    END
    """,
    """
    CREATE TRIGGER trg_warehouse_report_lines_immutable_update
    BEFORE UPDATE ON warehouse_report_lines
    BEGIN
        SELECT RAISE(ABORT, 'report snapshot lines are immutable');
    END
    """,
    """
    CREATE TRIGGER trg_warehouse_report_lines_immutable_delete
    BEFORE DELETE ON warehouse_report_lines
    BEGIN
        SELECT RAISE(ABORT, 'report snapshot lines are immutable');
    END
    """,
    """
    CREATE TRIGGER trg_warehouse_report_ready_immutable
    BEFORE UPDATE ON warehouse_reports
    WHEN OLD.status IN ('ready', 'superseded')
    BEGIN
        SELECT RAISE(ABORT, 'ready report is immutable');
    END
    """,
    """
    CREATE TRIGGER trg_warehouse_report_ready_delete
    BEFORE DELETE ON warehouse_reports
    WHEN OLD.status IN ('ready', 'superseded')
    BEGIN
        SELECT RAISE(ABORT, 'ready report is immutable');
    END
    """,
)


class WarehouseReportsSchemaError(RuntimeError):
    """Raised when the concrete v6 report schema is incompatible."""


def _table_sql(connection: sqlite3.Connection, table: str) -> str | None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return str(row[0]) if row is not None and row[0] is not None else None


def create_warehouse_reports_schema(connection: sqlite3.Connection) -> None:
    for statement in WAREHOUSE_REPORT_SCHEMA:
        connection.execute(statement)


def warehouse_reports_schema_is_absent(connection: sqlite3.Connection) -> bool:
    return all(
        _table_sql(connection, table) is None for table in WAREHOUSE_REPORT_TABLES
    )


_EXPECTED_COLUMNS = {
    "warehouse_reports": (
        "id", "shift_id", "version", "report_kind", "status",
        "relative_file_path", "file_sha256", "file_size_bytes",
        "created_at_utc", "created_by", "ready_at_utc", "failure_code",
        "supersedes_report_id", "correction_reason",
    ),
    "warehouse_report_lines": (
        "id", "report_id", "sheet_type", "row_number", "product_id",
        "product_name", "quantity_scaled", "base_unit", "reason_or_comment",
        "source_location_name", "destination_location_name", "business_date",
    ),
}

_EXPECTED_INDEXES = {
    "warehouse_reports": {
        "ux_warehouse_reports_building": 1,
        "ux_warehouse_reports_file_path": 1,
        "ix_warehouse_reports_history": 0,
    },
    "warehouse_report_lines": {
        "ix_warehouse_report_lines_report_sheet": 0,
    },
}

_EXPECTED_FOREIGN_KEYS = {
    "warehouse_reports": {
        ("shift_id", "warehouse_shifts", "id", "RESTRICT", "RESTRICT"),
        (
            "supersedes_report_id", "warehouse_reports", "id",
            "RESTRICT", "RESTRICT",
        ),
    },
    "warehouse_report_lines": {
        ("report_id", "warehouse_reports", "id", "RESTRICT", "RESTRICT"),
        ("product_id", "warehouse_products", "id", "RESTRICT", "RESTRICT"),
    },
}

_EXPECTED_TRIGGERS = {
    "trg_warehouse_report_lines_building_insert",
    "trg_warehouse_report_lines_immutable_update",
    "trg_warehouse_report_lines_immutable_delete",
    "trg_warehouse_report_ready_immutable",
    "trg_warehouse_report_ready_delete",
}

_REQUIRED_CHECKS = {
    "warehouse_reports": (
        "version>0",
        "report_kind='final'",
        "statusin('building','ready','failed','superseded')",
        "status='building'",
        "status='failed'",
        "statusin('ready','superseded')",
    ),
    "warehouse_report_lines": (
        "sheet_typein('writeoff','transfer')",
        "row_number>0",
        "quantity_scaled>0",
        "sheet_type='writeoff'",
        "sheet_type='transfer'",
    ),
}


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value.casefold()).translate(
        str.maketrans("", "", '"`[]')
    )


def _columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(
        str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')
    )


def _indexes(connection: sqlite3.Connection, table: str) -> dict[str, int]:
    return {
        str(row[1]): int(row[2])
        for row in connection.execute(f'PRAGMA index_list("{table}")')
        if not str(row[1]).startswith("sqlite_autoindex")
    }


def _foreign_keys(
    connection: sqlite3.Connection,
    table: str,
) -> set[tuple[str, str, str, str, str]]:
    return {
        (str(row[3]), str(row[2]), str(row[4]), str(row[5]), str(row[6]))
        for row in connection.execute(f'PRAGMA foreign_key_list("{table}")')
    }


def validate_warehouse_reports_schema(
    connection: sqlite3.Connection,
    *,
    allow_development: bool = False,
) -> None:
    """Validate v6 tables, indexes, FKs, CHECK clauses, and immutability."""

    for table in WAREHOUSE_REPORT_TABLES:
        sql = _table_sql(connection, table)
        if sql is None:
            raise WarehouseReportsSchemaError(f"Missing v6 table: {table}")
        expected_columns = _EXPECTED_COLUMNS[table]
        if allow_development and table == "warehouse_report_lines":
            expected_columns = expected_columns[:-1] + (
                "development_title",
                "development_group_id",
                expected_columns[-1],
            )
        if _columns(connection, table) != expected_columns:
            raise WarehouseReportsSchemaError(f"Invalid columns: {table}")
        if _indexes(connection, table) != _EXPECTED_INDEXES[table]:
            raise WarehouseReportsSchemaError(f"Invalid indexes: {table}")
        if _foreign_keys(connection, table) != _EXPECTED_FOREIGN_KEYS[table]:
            raise WarehouseReportsSchemaError(f"Invalid foreign keys: {table}")
        compact = _compact(sql)
        fragments = _REQUIRED_CHECKS[table]
        if allow_development and table == "warehouse_report_lines":
            fragments = fragments[1:] + (
                "sheet_typein('writeoff','transfer','development')",
                "sheet_type='development'",
                "development_titleisnotnull",
                "development_group_idisnotnull",
                "length(development_title)<=200",
            )
        if any(value not in compact for value in fragments):
            raise WarehouseReportsSchemaError(f"Invalid CHECK clauses: {table}")

    triggers = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'trg_warehouse_report%'"
        )
    }
    if triggers != _EXPECTED_TRIGGERS:
        raise WarehouseReportsSchemaError("Invalid warehouse report triggers")
