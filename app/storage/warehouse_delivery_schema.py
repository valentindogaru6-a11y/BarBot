"""Schema v8 for Telegram report recipients and delivery attempts."""

from __future__ import annotations

import re
import sqlite3

WAREHOUSE_DELIVERY_TABLES = (
    "warehouse_report_recipients",
    "warehouse_report_deliveries",
)

WAREHOUSE_DELIVERY_SCHEMA = (
    """
    CREATE TABLE warehouse_report_recipients (
        id INTEGER PRIMARY KEY,
        chat_id INTEGER NOT NULL UNIQUE,
        title TEXT NOT NULL CHECK (
            trim(title) <> '' AND title = trim(title) AND length(title) <= 200
        ),
        chat_type TEXT NOT NULL CHECK (
            chat_type IN ('private', 'group', 'supergroup', 'channel')
        ),
        is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
        created_by INTEGER NOT NULL,
        created_at_utc TEXT NOT NULL,
        updated_at_utc TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0)
    )
    """,
    """
    CREATE INDEX ix_warehouse_report_recipients_active_title
    ON warehouse_report_recipients(is_active, title, id)
    """,
    """
    CREATE TABLE warehouse_report_deliveries (
        id INTEGER PRIMARY KEY,
        report_id INTEGER NOT NULL,
        recipient_id INTEGER NOT NULL,
        status TEXT NOT NULL CHECK (
            status IN ('pending', 'sending', 'delivered', 'failed')
        ),
        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
        telegram_message_id INTEGER NULL CHECK (
            telegram_message_id IS NULL OR telegram_message_id > 0
        ),
        last_error_code TEXT NULL CHECK (
            last_error_code IS NULL
            OR (trim(last_error_code) <> '' AND length(last_error_code) <= 100)
        ),
        last_error_text TEXT NULL CHECK (
            last_error_text IS NULL OR length(last_error_text) <= 500
        ),
        last_attempt_at_utc TEXT NULL,
        delivered_at_utc TEXT NULL,
        created_at_utc TEXT NOT NULL,
        updated_at_utc TEXT NOT NULL,
        UNIQUE (report_id, recipient_id),
        CHECK (
            (status = 'pending' AND attempt_count = 0
                AND telegram_message_id IS NULL
                AND last_error_code IS NULL AND last_error_text IS NULL
                AND last_attempt_at_utc IS NULL AND delivered_at_utc IS NULL)
            OR
            (status = 'sending' AND attempt_count > 0
                AND telegram_message_id IS NULL
                AND last_error_code IS NULL AND last_error_text IS NULL
                AND last_attempt_at_utc IS NOT NULL AND delivered_at_utc IS NULL)
            OR
            (status = 'failed' AND attempt_count > 0
                AND telegram_message_id IS NULL
                AND last_error_code IS NOT NULL
                AND last_attempt_at_utc IS NOT NULL AND delivered_at_utc IS NULL)
            OR
            (status = 'delivered' AND attempt_count > 0
                AND telegram_message_id IS NOT NULL
                AND last_error_code IS NULL AND last_error_text IS NULL
                AND last_attempt_at_utc IS NOT NULL AND delivered_at_utc IS NOT NULL)
        ),
        FOREIGN KEY (report_id) REFERENCES warehouse_reports(id)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY (recipient_id) REFERENCES warehouse_report_recipients(id)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX ix_warehouse_report_deliveries_report_status
    ON warehouse_report_deliveries(report_id, status, id)
    """,
    """
    CREATE INDEX ix_warehouse_report_deliveries_recipient
    ON warehouse_report_deliveries(recipient_id, created_at_utc)
    """,
)


class WarehouseDeliverySchemaError(RuntimeError):
    """Raised when the concrete v8 delivery schema is incompatible."""


def _table_sql(connection: sqlite3.Connection, table: str) -> str | None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return str(row[0]) if row is not None and row[0] is not None else None


def create_warehouse_delivery_schema(connection: sqlite3.Connection) -> None:
    for statement in WAREHOUSE_DELIVERY_SCHEMA:
        connection.execute(statement)


def warehouse_delivery_schema_is_absent(connection: sqlite3.Connection) -> bool:
    return all(_table_sql(connection, table) is None for table in WAREHOUSE_DELIVERY_TABLES)


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value.casefold()).translate(
        str.maketrans("", "", '"`[]')
    )


def validate_warehouse_delivery_schema(connection: sqlite3.Connection) -> None:
    expected_columns = {
        "warehouse_report_recipients": (
            "id", "chat_id", "title", "chat_type", "is_active", "created_by",
            "created_at_utc", "updated_at_utc", "revision",
        ),
        "warehouse_report_deliveries": (
            "id", "report_id", "recipient_id", "status", "attempt_count",
            "telegram_message_id", "last_error_code", "last_error_text",
            "last_attempt_at_utc", "delivered_at_utc", "created_at_utc",
            "updated_at_utc",
        ),
    }
    expected_indexes = {
        "warehouse_report_recipients": {
            "ix_warehouse_report_recipients_active_title": 0,
        },
        "warehouse_report_deliveries": {
            "ix_warehouse_report_deliveries_report_status": 0,
            "ix_warehouse_report_deliveries_recipient": 0,
        },
    }
    expected_foreign_keys = {
        "warehouse_report_recipients": set(),
        "warehouse_report_deliveries": {
            ("report_id", "warehouse_reports", "id", "RESTRICT", "RESTRICT"),
            (
                "recipient_id", "warehouse_report_recipients", "id",
                "RESTRICT", "RESTRICT",
            ),
        },
    }
    expected_unique_indexes = {
        "warehouse_report_recipients": {("chat_id",)},
        "warehouse_report_deliveries": {("report_id", "recipient_id")},
    }
    required_checks = {
        "warehouse_report_recipients": (
            "title=trim(title)", "length(title)<=200",
            "chat_typein('private','group','supergroup','channel')",
            "is_activein(0,1)", "revision>0",
        ),
        "warehouse_report_deliveries": (
            "statusin('pending','sending','delivered','failed')",
            "attempt_count>=0", "status='pending'", "status='sending'",
            "status='failed'", "status='delivered'",
            "length(last_error_code)<=100", "length(last_error_text)<=500",
        ),
    }
    for table in WAREHOUSE_DELIVERY_TABLES:
        sql = _table_sql(connection, table)
        if sql is None:
            raise WarehouseDeliverySchemaError(f"Missing v8 table: {table}")
        columns = tuple(
            str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')
        )
        if columns != expected_columns[table]:
            raise WarehouseDeliverySchemaError(f"Invalid columns: {table}")
        indexes = {
            str(row[1]): int(row[2])
            for row in connection.execute(f'PRAGMA index_list("{table}")')
            if not str(row[1]).startswith("sqlite_autoindex")
        }
        if indexes != expected_indexes[table]:
            raise WarehouseDeliverySchemaError(f"Invalid indexes: {table}")
        unique_indexes = {
            tuple(
                str(column[2])
                for column in connection.execute(
                    f'PRAGMA index_info("{row[1]}")'
                )
            )
            for row in connection.execute(f'PRAGMA index_list("{table}")')
            if int(row[2]) == 1
        }
        if unique_indexes != expected_unique_indexes[table]:
            raise WarehouseDeliverySchemaError(
                f"Invalid unique indexes: {table}"
            )
        foreign_keys = {
            (str(row[3]), str(row[2]), str(row[4]), str(row[5]), str(row[6]))
            for row in connection.execute(f'PRAGMA foreign_key_list("{table}")')
        }
        if foreign_keys != expected_foreign_keys[table]:
            raise WarehouseDeliverySchemaError(f"Invalid foreign keys: {table}")
        compact = _compact(sql)
        if any(fragment not in compact for fragment in required_checks[table]):
            raise WarehouseDeliverySchemaError(f"Invalid CHECK clauses: {table}")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise WarehouseDeliverySchemaError("Foreign key check failed for schema v8")
