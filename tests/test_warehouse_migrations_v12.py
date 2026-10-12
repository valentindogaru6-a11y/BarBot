"""Schema-v12 attendance migration and constraints."""

import sqlite3
from pathlib import Path

import pytest

from app.storage import migrations
from app.storage.user_database import UserDatabase
from app.storage.warehouse_attendance_schema import validate_warehouse_attendance_schema


def test_fresh_v12_repeat_and_schema(tmp_path: Path) -> None:
    path = tmp_path / "fresh.db"
    UserDatabase(path).initialize(); UserDatabase(path).initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 13
        validate_warehouse_attendance_schema(connection)
        foreign = {row[2] for row in connection.execute("PRAGMA foreign_key_list(warehouse_shift_sessions)")}
        assert {"users", "warehouse_shifts", "warehouse_locations"} <= foreign


def test_v11_to_v12_and_rollback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "upgrade.db"
    UserDatabase(path).initialize()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE assistant_conversation_turns")
        connection.execute("DROP TABLE assistant_conversation_sessions")
        connection.execute("DROP TABLE warehouse_reminder_event_deliveries")
        connection.execute("DROP TABLE warehouse_shift_sessions")
        connection.execute("PRAGMA user_version=11")
        connection.commit()
    UserDatabase(path).initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 13

    rollback = tmp_path / "rollback.db"
    UserDatabase(rollback).initialize()
    with sqlite3.connect(rollback) as connection:
        connection.execute("DROP TABLE assistant_conversation_turns")
        connection.execute("DROP TABLE assistant_conversation_sessions")
        connection.execute("DROP TABLE warehouse_reminder_event_deliveries")
        connection.execute("DROP TABLE warehouse_shift_sessions")
        connection.execute("PRAGMA user_version=11")
        connection.commit()
    def fail(connection: sqlite3.Connection) -> None:
        connection.execute("CREATE TABLE partial_v12(id INTEGER PRIMARY KEY)")
        raise RuntimeError("simulated v12 failure")
    monkeypatch.setattr(migrations, "create_warehouse_attendance_schema", fail)
    with pytest.raises(migrations.DatabaseMigrationError, match="прервана и отменена"):
        UserDatabase(rollback).initialize()
    with sqlite3.connect(rollback) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 11
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE name='partial_v12'").fetchone() is None


def test_only_one_open_session_per_user(tmp_path: Path) -> None:
    path=tmp_path/"constraint.db"; UserDatabase(path).initialize()
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO users(telegram_user_id,username,first_name,last_name,role,is_active,first_seen_at,last_seen_at,created_by,updated_at,has_started) VALUES(1,NULL,'A',NULL,'bartender',1,'x','x',NULL,'x',1)")
        location=connection.execute("SELECT id FROM warehouse_locations LIMIT 1").fetchone()[0]
        shift=connection.execute("INSERT INTO warehouse_shifts(source_location_id,business_date,timezone_snapshot,cutoff_snapshot,status,started_at_utc,scheduled_close_at_utc,revision,created_at_utc,updated_at_utc) VALUES(?,'2026-09-24','Europe/Kyiv','23:00','open','x','y',1,'x','x')",(location,)).lastrowid
        stamp='2026-09-24T10:00:00Z'
        values=(shift,location,'2026-09-24',1,stamp,1,'A',stamp,stamp)
        sql="INSERT INTO warehouse_shift_sessions(warehouse_shift_id,source_location_id,business_date,user_id,opened_at_utc,opened_by_user_id,status,display_name_snapshot,created_at_utc,updated_at_utc,revision) VALUES(?,?,?,?,?,?,'open',?,?,?,1)"
        connection.execute(sql,values)
        with pytest.raises(sqlite3.IntegrityError): connection.execute(sql,values)
