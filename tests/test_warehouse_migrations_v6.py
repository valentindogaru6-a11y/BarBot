"""Schema-v6 migration coverage for immutable warehouse reports."""

import sqlite3
from pathlib import Path

import pytest

from app.storage import migrations
from app.storage.migrations import SchemaValidationError
from app.storage.user_database import UserDatabase
from app.storage.warehouse_reports_schema import WAREHOUSE_REPORT_TABLES


def _version(path: Path) -> int:
    with sqlite3.connect(path) as connection:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])


def _make_v5(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("BEGIN")
        migrations._create_fresh_v6(connection)
        connection.execute("DROP TABLE warehouse_report_lines")
        connection.execute("DROP TABLE warehouse_reports")
        connection.execute("PRAGMA user_version=5")
        connection.commit()


def test_fresh_v6_and_repeated_initialization(tmp_path: Path) -> None:
    path = tmp_path / "fresh.db"
    database = UserDatabase(path)
    database.initialize()
    database.initialize()

    assert _version(path) == 12
    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert set(WAREHOUSE_REPORT_TABLES) <= tables


def test_v5_to_v6_preserves_existing_rows(tmp_path: Path) -> None:
    path = tmp_path / "sequential.db"
    _make_v5(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO users (
                telegram_user_id, role, is_active, first_seen_at,
                last_seen_at, updated_at, has_started
            ) VALUES (42, 'accountant', 1, 'now', 'now', 'now', 1)
            """
        )
        connection.commit()

    UserDatabase(path).initialize()

    assert _version(path) == 12
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT role FROM users WHERE telegram_user_id=42"
        ).fetchone()[0] == "accountant"


def test_v5_to_v6_failure_rolls_back_whole_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "rollback.db"
    _make_v5(path)

    def fail(connection: sqlite3.Connection) -> None:
        connection.execute("CREATE TABLE partial_v6 (id INTEGER PRIMARY KEY)")
        raise RuntimeError("simulated v6 failure")

    monkeypatch.setattr(migrations, "create_warehouse_reports_schema", fail)
    with pytest.raises(migrations.DatabaseMigrationError, match="прервана"):
        UserDatabase(path).initialize()

    assert _version(path) == 5
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='partial_v6'"
        ).fetchone() is None


def test_v6_validation_detects_missing_index(tmp_path: Path) -> None:
    path = tmp_path / "broken.db"
    UserDatabase(path).initialize()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX ux_warehouse_reports_building")
        connection.commit()

    with pytest.raises(SchemaValidationError, match="Invalid indexes"):
        UserDatabase(path).initialize()


def test_v6_constraints_reject_duplicate_building_and_invalid_ready(
    tmp_path: Path,
) -> None:
    path = tmp_path / "constraints.db"
    UserDatabase(path).initialize()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        source = connection.execute(
            "SELECT id FROM warehouse_locations WHERE stable_id='bar'"
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO warehouse_shifts (
                source_location_id, business_date, timezone_snapshot,
                cutoff_snapshot, status, started_at_utc,
                scheduled_close_at_utc, revision, created_at_utc,
                updated_at_utc
            ) VALUES (?, '2026-09-10', 'Europe/Kyiv', '23:00', 'closing',
                      'now', 'later', 1, 'now', 'now')
            """,
            (source,),
        )
        shift_id = connection.execute(
            "SELECT id FROM warehouse_shifts"
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO warehouse_reports (
                shift_id, version, report_kind, status, created_at_utc,
                created_by
            ) VALUES (?, 1, 'final', 'building', 'now', 1)
            """,
            (shift_id,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO warehouse_reports (
                    shift_id, version, report_kind, status, created_at_utc,
                    created_by
                ) VALUES (?, 2, 'final', 'building', 'now', 1)
                """,
                (shift_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO warehouse_reports (
                    shift_id, version, report_kind, status, created_at_utc,
                    created_by
                ) VALUES (?, 2, 'final', 'ready', 'now', 1)
                """,
                (shift_id,),
            )
