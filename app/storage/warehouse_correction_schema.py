"""Schema v11 for auditable corrections of closed warehouse shifts."""

from __future__ import annotations

import re
import sqlite3

WAREHOUSE_CORRECTION_TABLES = ("warehouse_correction_sessions",)

WAREHOUSE_CORRECTION_SCHEMA = (
    """
    CREATE TABLE warehouse_correction_sessions (
        id INTEGER PRIMARY KEY,
        shift_id INTEGER NOT NULL,
        base_report_id INTEGER NOT NULL,
        result_report_id INTEGER NULL,
        status TEXT NOT NULL CHECK (
            status IN ('open', 'building', 'completed', 'cancelled', 'failed')
        ),
        reason_text TEXT NOT NULL CHECK (
            reason_text=trim(reason_text)
            AND length(reason_text) BETWEEN 1 AND 500
        ),
        opened_by INTEGER NOT NULL,
        opened_at_utc TEXT NOT NULL CHECK (
            opened_at_utc GLOB '????-??-??T??:??:??Z'
            AND datetime(opened_at_utc) IS NOT NULL
        ),
        completed_by INTEGER NULL,
        completed_at_utc TEXT NULL CHECK (
            completed_at_utc IS NULL OR (
                completed_at_utc GLOB '????-??-??T??:??:??Z'
                AND datetime(completed_at_utc) IS NOT NULL
            )
        ),
        revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
        created_at_utc TEXT NOT NULL CHECK (
            created_at_utc GLOB '????-??-??T??:??:??Z'
            AND datetime(created_at_utc) IS NOT NULL
        ),
        updated_at_utc TEXT NOT NULL CHECK (
            updated_at_utc GLOB '????-??-??T??:??:??Z'
            AND datetime(updated_at_utc) IS NOT NULL
        ),
        CHECK (
            (status='completed' AND result_report_id IS NOT NULL
                AND completed_by IS NOT NULL AND completed_at_utc IS NOT NULL)
            OR
            (status='cancelled' AND result_report_id IS NULL
                AND completed_by IS NOT NULL AND completed_at_utc IS NOT NULL)
            OR
            (status IN ('open','building','failed') AND result_report_id IS NULL
                AND completed_by IS NULL AND completed_at_utc IS NULL)
        ),
        FOREIGN KEY (shift_id) REFERENCES warehouse_shifts(id)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY (base_report_id) REFERENCES warehouse_reports(id)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY (result_report_id) REFERENCES warehouse_reports(id)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    )
    """,
    """
    CREATE UNIQUE INDEX ux_warehouse_correction_sessions_active
    ON warehouse_correction_sessions(shift_id)
    WHERE status IN ('open','building')
    """,
    """
    CREATE INDEX ix_warehouse_correction_sessions_history
    ON warehouse_correction_sessions(shift_id, id)
    """,
    """
    CREATE TRIGGER trg_warehouse_correction_base_insert
    BEFORE INSERT ON warehouse_correction_sessions
    WHEN NOT EXISTS (
        SELECT 1 FROM warehouse_reports r
        WHERE r.id=NEW.base_report_id AND r.shift_id=NEW.shift_id
          AND r.status='ready'
    )
    BEGIN SELECT RAISE(ABORT, 'correction base report must be ready'); END
    """,
    """
    CREATE TRIGGER trg_warehouse_correction_result_update
    BEFORE UPDATE OF result_report_id ON warehouse_correction_sessions
    WHEN NEW.result_report_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM warehouse_reports r
        WHERE r.id=NEW.result_report_id AND r.shift_id=NEW.shift_id
          AND r.correction_session_id=NEW.id AND r.status='ready'
    )
    BEGIN SELECT RAISE(ABORT, 'correction result report is invalid'); END
    """,
    """
    CREATE TRIGGER trg_warehouse_correction_report_insert
    BEFORE INSERT ON warehouse_reports
    WHEN NEW.correction_session_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM warehouse_correction_sessions cs
        WHERE cs.id=NEW.correction_session_id AND cs.shift_id=NEW.shift_id
          AND cs.status IN ('open','building','failed')
    )
    BEGIN SELECT RAISE(ABORT, 'report correction session is invalid'); END
    """,
)


class WarehouseCorrectionSchemaError(RuntimeError):
    pass


def create_warehouse_correction_schema(connection: sqlite3.Connection) -> None:
    connection.execute(WAREHOUSE_CORRECTION_SCHEMA[0])
    connection.execute(
        "ALTER TABLE warehouse_reports ADD COLUMN correction_session_id "
        "INTEGER NULL REFERENCES warehouse_correction_sessions(id) "
        "ON UPDATE RESTRICT ON DELETE RESTRICT"
    )
    connection.execute(
        "CREATE UNIQUE INDEX ux_warehouse_reports_correction_session "
        "ON warehouse_reports(correction_session_id) "
        "WHERE correction_session_id IS NOT NULL"
    )
    for statement in WAREHOUSE_CORRECTION_SCHEMA[1:]:
        connection.execute(statement)


def warehouse_correction_schema_is_absent(connection: sqlite3.Connection) -> bool:
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='warehouse_correction_sessions'"
    ).fetchone()
    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(warehouse_reports)")
    }
    return table is None and "correction_session_id" not in columns


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value.casefold()).translate(
        str.maketrans("", "", '"`[]')
    )


def validate_warehouse_correction_schema(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='warehouse_correction_sessions'"
    ).fetchone()
    if row is None:
        raise WarehouseCorrectionSchemaError("Missing v11 correction table")
    columns = tuple(
        str(item[1])
        for item in connection.execute(
            "PRAGMA table_info(warehouse_correction_sessions)"
        )
    )
    if columns != (
        "id", "shift_id", "base_report_id", "result_report_id", "status",
        "reason_text", "opened_by", "opened_at_utc", "completed_by",
        "completed_at_utc", "revision", "created_at_utc", "updated_at_utc",
    ):
        raise WarehouseCorrectionSchemaError("Invalid correction columns")
    indexes = {
        str(item[1]): int(item[2])
        for item in connection.execute(
            "PRAGMA index_list(warehouse_correction_sessions)"
        )
        if not str(item[1]).startswith("sqlite_autoindex")
    }
    if indexes != {
        "ux_warehouse_correction_sessions_active": 1,
        "ix_warehouse_correction_sessions_history": 0,
    }:
        raise WarehouseCorrectionSchemaError("Invalid correction indexes")
    report_columns = tuple(
        str(item[1])
        for item in connection.execute("PRAGMA table_info(warehouse_reports)")
    )
    if not report_columns or report_columns[-1] != "correction_session_id":
        raise WarehouseCorrectionSchemaError("Missing report correction link")
    report_indexes = {
        str(item[1])
        for item in connection.execute("PRAGMA index_list(warehouse_reports)")
    }
    if "ux_warehouse_reports_correction_session" not in report_indexes:
        raise WarehouseCorrectionSchemaError("Missing report correction index")
    foreign_keys = {
        (str(item[3]), str(item[2]), str(item[4]))
        for item in connection.execute(
            "PRAGMA foreign_key_list(warehouse_correction_sessions)"
        )
    }
    if foreign_keys != {
        ("shift_id", "warehouse_shifts", "id"),
        ("base_report_id", "warehouse_reports", "id"),
        ("result_report_id", "warehouse_reports", "id"),
    }:
        raise WarehouseCorrectionSchemaError("Invalid correction foreign keys")
    compact = _compact(str(row[0]))
    for fragment in (
        "statusin('open','building','completed','cancelled','failed')",
        "length(reason_text)between1and500",
        "revision>0",
        "status='completed'",
        "status='cancelled'",
    ):
        if fragment not in compact:
            raise WarehouseCorrectionSchemaError("Invalid correction checks")
    triggers = {
        str(item[0])
        for item in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'trg_warehouse_correction_%'"
        )
    }
    if triggers != {
        "trg_warehouse_correction_base_insert",
        "trg_warehouse_correction_result_update",
        "trg_warehouse_correction_report_insert",
    }:
        raise WarehouseCorrectionSchemaError("Invalid correction triggers")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise WarehouseCorrectionSchemaError("Foreign key check failed for v11")
