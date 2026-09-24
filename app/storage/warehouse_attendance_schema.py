"""Schema v12 for personal warehouse-shift attendance sessions."""

from __future__ import annotations

import sqlite3

TABLES = ("warehouse_shift_sessions", "warehouse_reminder_event_deliveries")

SCHEMA = (
    """CREATE TABLE warehouse_shift_sessions (
        id INTEGER PRIMARY KEY,
        warehouse_shift_id INTEGER NOT NULL,
        source_location_id INTEGER NOT NULL,
        business_date TEXT NOT NULL CHECK(
            length(business_date)=10 AND date(business_date) IS NOT NULL
            AND date(business_date)=business_date
        ),
        user_id INTEGER NOT NULL,
        opened_at_utc TEXT NOT NULL CHECK(
            opened_at_utc GLOB '????-??-??T??:??:??*Z'
            AND datetime(opened_at_utc) IS NOT NULL
        ),
        closed_at_utc TEXT NULL CHECK(
            closed_at_utc IS NULL OR (
                closed_at_utc GLOB '????-??-??T??:??:??*Z'
                AND datetime(closed_at_utc) IS NOT NULL
            )
        ),
        opened_by_user_id INTEGER NOT NULL,
        closed_by_user_id INTEGER NULL,
        close_reason TEXT NULL,
        status TEXT NOT NULL CHECK(status IN ('open','closed','force_closed')),
        display_name_snapshot TEXT NOT NULL CHECK(trim(display_name_snapshot)<>''),
        username_snapshot TEXT NULL,
        created_at_utc TEXT NOT NULL CHECK(
            created_at_utc GLOB '????-??-??T??:??:??*Z'
            AND datetime(created_at_utc) IS NOT NULL
        ),
        updated_at_utc TEXT NOT NULL CHECK(
            updated_at_utc GLOB '????-??-??T??:??:??*Z'
            AND datetime(updated_at_utc) IS NOT NULL
        ),
        revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>0),
        FOREIGN KEY(warehouse_shift_id) REFERENCES warehouse_shifts(id)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY(source_location_id) REFERENCES warehouse_locations(id)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY(user_id) REFERENCES users(telegram_user_id)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY(opened_by_user_id) REFERENCES users(telegram_user_id)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY(closed_by_user_id) REFERENCES users(telegram_user_id)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        CHECK((status='open' AND closed_at_utc IS NULL AND closed_by_user_id IS NULL)
           OR (status IN ('closed','force_closed') AND closed_at_utc IS NOT NULL
               AND closed_by_user_id IS NOT NULL)),
        CHECK(status!='force_closed' OR (close_reason IS NOT NULL AND trim(close_reason)<>''))
        ,CHECK(status!='closed' OR close_reason IS NULL)
        ,CHECK(closed_at_utc IS NULL OR julianday(closed_at_utc)>=julianday(opened_at_utc))
    )""",
    """CREATE UNIQUE INDEX ux_warehouse_shift_sessions_user_open
       ON warehouse_shift_sessions(user_id) WHERE status='open'""",
    """CREATE INDEX ix_warehouse_shift_sessions_shift_status
       ON warehouse_shift_sessions(warehouse_shift_id,status)""",
    """CREATE INDEX ix_warehouse_shift_sessions_business_date
       ON warehouse_shift_sessions(business_date,user_id)""",
    """CREATE TABLE warehouse_reminder_event_deliveries (
        event_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('sending','sent','failed')),
        telegram_message_id INTEGER NULL,
        last_error_code TEXT NULL,
        attempted_at_utc TEXT NOT NULL,
        updated_at_utc TEXT NOT NULL,
        PRIMARY KEY(event_id,user_id),
        FOREIGN KEY(event_id) REFERENCES warehouse_reminder_events(id)
            ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(user_id) REFERENCES users(telegram_user_id)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    )""",
)


class WarehouseAttendanceSchemaError(RuntimeError):
    pass


def create_warehouse_attendance_schema(connection: sqlite3.Connection) -> None:
    for statement in SCHEMA:
        connection.execute(statement)


def warehouse_attendance_schema_is_absent(connection: sqlite3.Connection) -> bool:
    names = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    return not any(table in names for table in TABLES)


def validate_warehouse_attendance_schema(connection: sqlite3.Connection) -> None:
    names = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if not set(TABLES) <= names:
        raise WarehouseAttendanceSchemaError("Attendance tables are incomplete")
    columns = {row[1] for row in connection.execute(
        "PRAGMA table_info(warehouse_shift_sessions)"
    )}
    required = {
        "id", "warehouse_shift_id", "source_location_id", "business_date",
        "user_id", "opened_at_utc", "closed_at_utc", "opened_by_user_id",
        "closed_by_user_id", "close_reason", "status", "display_name_snapshot",
        "username_snapshot", "created_at_utc", "updated_at_utc", "revision",
    }
    if columns != required:
        raise WarehouseAttendanceSchemaError("Attendance columns are invalid")
    indexes = {row[1] for row in connection.execute(
        "PRAGMA index_list(warehouse_shift_sessions)"
    )}
    if not {
        "ux_warehouse_shift_sessions_user_open",
        "ix_warehouse_shift_sessions_shift_status",
        "ix_warehouse_shift_sessions_business_date",
    } <= indexes:
        raise WarehouseAttendanceSchemaError("Attendance indexes are incomplete")
    table_sql_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='warehouse_shift_sessions'"
    ).fetchone()
    table_sql = "".join(str(table_sql_row[0] if table_sql_row else "").casefold().split())
    for fragment in (
        "statusin('open','closed','force_closed')",
        "revision>0",
        "status!='force_closed'",
        "date(business_date)=business_date",
        "opened_at_utcglob'????-??-??t??:??:??*z'",
        "datetime(opened_at_utc)isnotnull",
        "closed_at_utcglob'????-??-??t??:??:??*z'",
        "datetime(closed_at_utc)isnotnull",
        "datetime(created_at_utc)isnotnull",
        "datetime(updated_at_utc)isnotnull",
        "julianday(closed_at_utc)>=julianday(opened_at_utc)",
        "status!='closed'orclose_reasonisnull",
    ):
        if fragment not in table_sql:
            raise WarehouseAttendanceSchemaError("Attendance CHECK constraints are incomplete")
    foreign_targets = [row[2] for row in connection.execute(
        "PRAGMA foreign_key_list(warehouse_shift_sessions)"
    )]
    if foreign_targets.count("users") != 3 or not {
        "warehouse_shifts", "warehouse_locations"
    } <= set(foreign_targets):
        raise WarehouseAttendanceSchemaError("Attendance foreign keys are incomplete")
    unique_index = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='ux_warehouse_shift_sessions_user_open'"
    ).fetchone()
    if unique_index is None or "where status='open'" not in str(unique_index[0]).casefold():
        raise WarehouseAttendanceSchemaError("Open-session uniqueness is not partial")
    delivery_columns = {row[1] for row in connection.execute(
        "PRAGMA table_info(warehouse_reminder_event_deliveries)"
    )}
    if delivery_columns != {
        "event_id", "user_id", "status", "telegram_message_id",
        "last_error_code", "attempted_at_utc", "updated_at_utc",
    }:
        raise WarehouseAttendanceSchemaError("Reminder delivery columns are invalid")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise WarehouseAttendanceSchemaError("Foreign key check failed for v12")
