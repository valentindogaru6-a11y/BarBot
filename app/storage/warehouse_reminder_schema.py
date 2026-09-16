"""Schema v9 for configurable warehouse shift reminders."""
from __future__ import annotations
import re
import sqlite3

WAREHOUSE_REMINDER_TABLES = ("warehouse_reminder_settings", "warehouse_reminder_events")
WAREHOUSE_REMINDER_SCHEMA = (
    """CREATE TABLE warehouse_reminder_settings (
        source_location_id INTEGER PRIMARY KEY,
        enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0,1)),
        target_chat_id INTEGER NULL,
        target_title TEXT NULL CHECK (target_title IS NULL OR (trim(target_title) <> '' AND length(target_title) <= 200)),
        timezone TEXT NOT NULL DEFAULT 'Europe/Kyiv' CHECK (trim(timezone) <> ''),
        first_reminder_minutes_before_close INTEGER NOT NULL DEFAULT 30 CHECK (first_reminder_minutes_before_close BETWEEN 0 AND 1440),
        repeat_interval_minutes INTEGER NOT NULL DEFAULT 30 CHECK (repeat_interval_minutes BETWEEN 5 AND 1440),
        max_reminders INTEGER NOT NULL DEFAULT 3 CHECK (max_reminders BETWEEN 1 AND 10),
        revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
        created_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL,
        CHECK ((enabled=0) OR (target_chat_id IS NOT NULL AND target_title IS NOT NULL)),
        FOREIGN KEY (source_location_id) REFERENCES warehouse_locations(id) ON UPDATE RESTRICT ON DELETE RESTRICT
    )""",
    "CREATE INDEX ix_warehouse_reminder_settings_enabled ON warehouse_reminder_settings(enabled, source_location_id)",
    """CREATE TABLE warehouse_reminder_events (
        id INTEGER PRIMARY KEY, shift_id INTEGER NOT NULL, sequence_number INTEGER NOT NULL CHECK (sequence_number > 0),
        scheduled_at_utc TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('pending','sending','sent','failed','cancelled')),
        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0), telegram_message_id INTEGER NULL CHECK (telegram_message_id IS NULL OR telegram_message_id > 0),
        last_error_code TEXT NULL CHECK (last_error_code IS NULL OR (trim(last_error_code) <> '' AND length(last_error_code) <= 100)),
        last_error_text TEXT NULL CHECK (last_error_text IS NULL OR length(last_error_text) <= 500), claimed_at_utc TEXT NULL, sent_at_utc TEXT NULL,
        created_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL, UNIQUE(shift_id, sequence_number),
        CHECK ((status='pending' AND attempt_count=0 AND telegram_message_id IS NULL AND last_error_code IS NULL AND last_error_text IS NULL AND claimed_at_utc IS NULL AND sent_at_utc IS NULL)
          OR (status='sending' AND attempt_count>0 AND telegram_message_id IS NULL AND last_error_code IS NULL AND last_error_text IS NULL AND claimed_at_utc IS NOT NULL AND sent_at_utc IS NULL)
          OR (status='failed' AND attempt_count>0 AND telegram_message_id IS NULL AND last_error_code IS NOT NULL AND claimed_at_utc IS NOT NULL AND sent_at_utc IS NULL)
          OR (status='sent' AND attempt_count>0 AND telegram_message_id IS NOT NULL AND last_error_code IS NULL AND last_error_text IS NULL AND claimed_at_utc IS NOT NULL AND sent_at_utc IS NOT NULL)
          OR (status='cancelled' AND telegram_message_id IS NULL AND sent_at_utc IS NULL)),
        FOREIGN KEY (shift_id) REFERENCES warehouse_shifts(id) ON UPDATE RESTRICT ON DELETE RESTRICT
    )""",
    "CREATE INDEX ix_warehouse_reminder_events_due ON warehouse_reminder_events(status, scheduled_at_utc, id)",
    "CREATE INDEX ix_warehouse_reminder_events_shift ON warehouse_reminder_events(shift_id, sequence_number)",
)
class WarehouseReminderSchemaError(RuntimeError): pass
def _sql(c,t):
    r=c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",(t,)).fetchone(); return r[0] if r else None
def create_warehouse_reminder_schema(c):
    for statement in WAREHOUSE_REMINDER_SCHEMA: c.execute(statement)
def warehouse_reminder_schema_is_absent(c): return all(_sql(c,t) is None for t in WAREHOUSE_REMINDER_TABLES)
def validate_warehouse_reminder_schema(c):
    expected={"warehouse_reminder_settings":("source_location_id","enabled","target_chat_id","target_title","timezone","first_reminder_minutes_before_close","repeat_interval_minutes","max_reminders","revision","created_at_utc","updated_at_utc"),"warehouse_reminder_events":("id","shift_id","sequence_number","scheduled_at_utc","status","attempt_count","telegram_message_id","last_error_code","last_error_text","claimed_at_utc","sent_at_utc","created_at_utc","updated_at_utc")}
    indexes={"warehouse_reminder_settings":{"ix_warehouse_reminder_settings_enabled"},"warehouse_reminder_events":{"ix_warehouse_reminder_events_due","ix_warehouse_reminder_events_shift"}}
    for table in WAREHOUSE_REMINDER_TABLES:
        sql=_sql(c,table)
        if sql is None: raise WarehouseReminderSchemaError(f"Missing v9 table: {table}")
        cols=tuple(r[1] for r in c.execute(f'PRAGMA table_info("{table}")'))
        if cols != expected[table]: raise WarehouseReminderSchemaError(f"Invalid columns: {table}")
        found={r[1] for r in c.execute(f'PRAGMA index_list("{table}")') if not r[1].startswith('sqlite_autoindex')}
        if found != indexes[table]: raise WarehouseReminderSchemaError(f"Invalid indexes: {table}")
        compact=re.sub(r'\s+','',sql.casefold())
        if (table.endswith("settings") and "revision>0" not in compact) or (table.endswith("events") and "statusin('pending','sending','sent','failed','cancelled')" not in compact): raise WarehouseReminderSchemaError(f"Invalid CHECK clauses: {table}")
    if c.execute("PRAGMA foreign_key_check").fetchone() is not None: raise WarehouseReminderSchemaError("Foreign key check failed for schema v9")
