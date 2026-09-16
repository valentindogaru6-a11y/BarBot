"""Schema v10 for scheduled delivery of immutable warehouse reports."""
from __future__ import annotations
import re
import sqlite3

WAREHOUSE_SCHEDULE_TABLES = ("warehouse_delivery_schedules", "warehouse_delivery_schedule_recipients", "warehouse_delivery_jobs")
WAREHOUSE_SCHEDULE_SCHEMA = (
"""CREATE TABLE warehouse_delivery_schedules (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL CHECK(trim(name)<>'' AND name=trim(name) AND length(name)<=200),
 schedule_type TEXT NOT NULL CHECK(schedule_type IN ('daily','once')),
 source_location_id INTEGER NULL, report_id INTEGER NULL, timezone TEXT NOT NULL CHECK(trim(timezone)<>''),
 delay_minutes_after_close INTEGER NULL CHECK(delay_minutes_after_close BETWEEN 0 AND 1440), run_at_utc TEXT NULL,
 max_wait_minutes INTEGER NOT NULL DEFAULT 1440 CHECK(max_wait_minutes BETWEEN 5 AND 1440),
 is_active INTEGER NOT NULL DEFAULT 0 CHECK(is_active IN(0,1)), created_by INTEGER NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>0), created_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL,
 CHECK((schedule_type='daily' AND source_location_id IS NOT NULL AND delay_minutes_after_close IS NOT NULL AND report_id IS NULL AND run_at_utc IS NULL)
    OR (schedule_type='once' AND source_location_id IS NULL AND delay_minutes_after_close IS NULL AND report_id IS NOT NULL AND run_at_utc IS NOT NULL)),
 FOREIGN KEY(source_location_id) REFERENCES warehouse_locations(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 FOREIGN KEY(report_id) REFERENCES warehouse_reports(id) ON UPDATE RESTRICT ON DELETE RESTRICT)""",
"CREATE INDEX ix_warehouse_delivery_schedules_active ON warehouse_delivery_schedules(is_active,schedule_type,id)",
"""CREATE TABLE warehouse_delivery_schedule_recipients (
 schedule_id INTEGER NOT NULL, recipient_id INTEGER NOT NULL, created_at_utc TEXT NOT NULL,
 PRIMARY KEY(schedule_id,recipient_id),
 FOREIGN KEY(schedule_id) REFERENCES warehouse_delivery_schedules(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 FOREIGN KEY(recipient_id) REFERENCES warehouse_report_recipients(id) ON UPDATE RESTRICT ON DELETE RESTRICT)""",
"CREATE INDEX ix_warehouse_delivery_schedule_recipients_recipient ON warehouse_delivery_schedule_recipients(recipient_id,schedule_id)",
"""CREATE TABLE warehouse_delivery_jobs (
 id INTEGER PRIMARY KEY, schedule_id INTEGER NOT NULL, shift_id INTEGER NULL, report_id INTEGER NULL,
 business_date TEXT NOT NULL, scheduled_at_utc TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('scheduled','waiting_report','sending','sent','partial_failed','failed','cancelled')),
 claimed_at_utc TEXT NULL, started_at_utc TEXT NULL, finished_at_utc TEXT NULL,
 last_error_code TEXT NULL CHECK(last_error_code IS NULL OR (trim(last_error_code)<>'' AND length(last_error_code)<=100)),
 last_error_text TEXT NULL CHECK(last_error_text IS NULL OR length(last_error_text)<=500),
 created_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL,
 UNIQUE(schedule_id,shift_id),
 FOREIGN KEY(schedule_id) REFERENCES warehouse_delivery_schedules(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 FOREIGN KEY(shift_id) REFERENCES warehouse_shifts(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 FOREIGN KEY(report_id) REFERENCES warehouse_reports(id) ON UPDATE RESTRICT ON DELETE RESTRICT)""",
"CREATE UNIQUE INDEX ux_warehouse_delivery_jobs_once ON warehouse_delivery_jobs(schedule_id) WHERE shift_id IS NULL",
"CREATE INDEX ix_warehouse_delivery_jobs_due ON warehouse_delivery_jobs(status,scheduled_at_utc,id)",
"CREATE INDEX ix_warehouse_delivery_jobs_report ON warehouse_delivery_jobs(report_id,status,id)",
)
class WarehouseScheduleSchemaError(RuntimeError): pass
def _sql(c,t):
 r=c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",(t,)).fetchone();return r[0] if r else None
def create_warehouse_schedule_schema(c):
 for s in WAREHOUSE_SCHEDULE_SCHEMA:c.execute(s)
def warehouse_schedule_schema_is_absent(c):return all(_sql(c,t) is None for t in WAREHOUSE_SCHEDULE_TABLES)
def validate_warehouse_schedule_schema(c):
 cols={"warehouse_delivery_schedules":("id","name","schedule_type","source_location_id","report_id","timezone","delay_minutes_after_close","run_at_utc","max_wait_minutes","is_active","created_by","revision","created_at_utc","updated_at_utc"),"warehouse_delivery_schedule_recipients":("schedule_id","recipient_id","created_at_utc"),"warehouse_delivery_jobs":("id","schedule_id","shift_id","report_id","business_date","scheduled_at_utc","status","claimed_at_utc","started_at_utc","finished_at_utc","last_error_code","last_error_text","created_at_utc","updated_at_utc")}
 indexes={"warehouse_delivery_schedules":{"ix_warehouse_delivery_schedules_active"},"warehouse_delivery_schedule_recipients":{"ix_warehouse_delivery_schedule_recipients_recipient"},"warehouse_delivery_jobs":{"ux_warehouse_delivery_jobs_once","ix_warehouse_delivery_jobs_due","ix_warehouse_delivery_jobs_report"}}
 for table in WAREHOUSE_SCHEDULE_TABLES:
  sql=_sql(c,table)
  if sql is None:raise WarehouseScheduleSchemaError(f"Missing v10 table: {table}")
  if tuple(r[1] for r in c.execute(f'PRAGMA table_info("{table}")'))!=cols[table]:raise WarehouseScheduleSchemaError(f"Invalid columns: {table}")
  found={r[1] for r in c.execute(f'PRAGMA index_list("{table}")') if not r[1].startswith('sqlite_autoindex')}
  if found!=indexes[table]:raise WarehouseScheduleSchemaError(f"Invalid indexes: {table}")
  compact=re.sub(r'\s+','',sql.casefold())
  if table.endswith('schedules') and "schedule_typein('daily','once')" not in compact:raise WarehouseScheduleSchemaError("Invalid schedule checks")
  if table.endswith('jobs') and "statusin('scheduled','waiting_report','sending','sent','partial_failed','failed','cancelled')" not in compact:raise WarehouseScheduleSchemaError("Invalid job checks")
 if c.execute('PRAGMA foreign_key_check').fetchone() is not None:raise WarehouseScheduleSchemaError("Foreign key check failed for schema v10")
