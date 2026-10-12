"""Focused schema-v9 migration and reminder constraints coverage."""

import sqlite3
from pathlib import Path

import pytest

from app.storage import migrations
from app.storage.migrations import SchemaValidationError
from app.storage.user_database import UserDatabase
from app.storage.warehouse_reminder_schema import WAREHOUSE_REMINDER_TABLES


def _version(path: Path) -> int:
    with sqlite3.connect(path) as connection:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])


def _make_v8(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("BEGIN")
        migrations._create_fresh_v8(connection)
        connection.commit()


def test_fresh_v9_repeat_and_full_chain(tmp_path: Path) -> None:
    path = tmp_path / "fresh.db"
    UserDatabase(path).initialize()
    UserDatabase(path).initialize()
    assert _version(path) == 14
    with sqlite3.connect(path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(WAREHOUSE_REMINDER_TABLES) <= tables


def test_v8_to_v9_preserves_rows_and_adds_reminder_tables(tmp_path: Path) -> None:
    path = tmp_path / "v8.db"
    _make_v8(path)
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO users (telegram_user_id,role,is_active,first_seen_at,last_seen_at,updated_at,has_started) VALUES (42,'bartender',1,'now','now','now',1)")
        connection.commit()
    UserDatabase(path).initialize()
    assert _version(path) == 14
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT role FROM users WHERE telegram_user_id=42").fetchone()[0] == "bartender"
        assert {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")} >= set(WAREHOUSE_REMINDER_TABLES)


def test_v8_to_v9_failure_rolls_back_entire_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "rollback.db"
    _make_v8(path)

    def fail(connection: sqlite3.Connection) -> None:
        connection.execute("CREATE TABLE partial_v9 (id INTEGER PRIMARY KEY)")
        raise RuntimeError("simulated v9 failure")

    monkeypatch.setattr(migrations, "create_warehouse_reminder_schema", fail)
    with pytest.raises(migrations.DatabaseMigrationError, match="прервана"):
        UserDatabase(path).initialize()
    assert _version(path) == 8
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE name='partial_v9'").fetchone() is None


def test_v9_validation_and_constraints(tmp_path: Path) -> None:
    path = tmp_path / "constraints.db"
    UserDatabase(path).initialize()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        source = connection.execute("SELECT id FROM warehouse_locations WHERE stable_id='bar'").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO warehouse_reminder_settings(source_location_id,enabled,timezone,first_reminder_minutes_before_close,repeat_interval_minutes,max_reminders,revision,created_at_utc,updated_at_utc) VALUES (?,1,'Europe/Kyiv',30,30,3,1,'now','now')", (source,))
        connection.execute("DROP INDEX ix_warehouse_reminder_events_due")
        connection.commit()
    with pytest.raises(SchemaValidationError, match="Invalid indexes"):
        UserDatabase(path).initialize()
