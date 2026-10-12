import sqlite3
from pathlib import Path

import pytest

from app.storage import migrations
from app.storage.migrations import DatabaseMigrationError, SchemaValidationError
from app.storage.user_database import UserDatabase
from app.storage.warehouse_operations_schema import WAREHOUSE_OPERATION_TABLES
from app.storage.warehouse_schema import create_warehouse_schema, seed_warehouse_defaults


def _create_v3(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(migrations._USERS_V2_SQL)
        create_warehouse_schema(connection)
        seed_warehouse_defaults(connection)
        connection.execute("PRAGMA user_version = 3")
        connection.commit()
    finally:
        connection.close()


def test_fresh_v6_has_operations_schema_and_locations(tmp_path: Path) -> None:
    path = tmp_path / "fresh.db"
    UserDatabase(path).initialize()
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 14
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert set(WAREHOUSE_OPERATION_TABLES) <= tables
        locations = connection.execute(
            "SELECT stable_id, can_be_source, can_be_destination "
            "FROM warehouse_locations ORDER BY stable_id"
        ).fetchall()
        assert locations == [
            ("bar", 1, 1),
            ("confectionery", 0, 1),
            ("kitchen", 0, 1),
        ]
    finally:
        connection.close()


def test_v3_to_v4_and_repeat_preserve_data(tmp_path: Path) -> None:
    path = tmp_path / "v3.db"
    _create_v3(path)
    UserDatabase(path).initialize()
    connection = sqlite3.connect(path)
    connection.execute(
        """
        INSERT INTO warehouse_shifts (
            source_location_id, business_date, timezone_snapshot, cutoff_snapshot,
            status, started_at_utc, scheduled_close_at_utc, revision,
            created_at_utc, updated_at_utc
        ) SELECT id, '2026-09-09', 'Europe/Kyiv', '23:00', 'open',
                 '2026-09-08T20:00:00Z', '2026-09-09T20:00:00Z', 1,
                 '2026-09-08T20:00:00Z', '2026-09-08T20:00:00Z'
          FROM warehouse_locations WHERE stable_id='bar'
        """
    )
    connection.commit()
    connection.close()

    UserDatabase(path).initialize()

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM warehouse_shifts").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM warehouse_locations").fetchone()[0] == 3
    finally:
        connection.close()


def test_v3_migration_rolls_back_completely(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "rollback.db"
    _create_v3(path)

    def fail(connection):
        connection.execute("CREATE TABLE should_rollback (id INTEGER)")
        raise RuntimeError("boom")

    monkeypatch.setattr(migrations, "create_warehouse_operations_schema", fail)
    with pytest.raises(DatabaseMigrationError, match="отменена"):
        UserDatabase(path).initialize()
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='should_rollback'"
        ).fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM warehouse_locations").fetchone()[0] == 1
    finally:
        connection.close()


def test_v4_schema_validation_detects_missing_index(tmp_path: Path) -> None:
    path = tmp_path / "broken.db"
    UserDatabase(path).initialize()
    connection = sqlite3.connect(path)
    connection.execute("DROP INDEX ix_warehouse_operations_product")
    connection.commit()
    connection.close()

    with pytest.raises(SchemaValidationError, match="Invalid indexes"):
        UserDatabase(path).initialize()
