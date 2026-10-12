"""Schema-v8 migration coverage for report recipients and deliveries."""

import sqlite3
from pathlib import Path

import pytest

from app.storage import migrations
from app.storage.user_database import UserDatabase
from app.storage.warehouse_delivery_schema import (
    validate_warehouse_delivery_schema,
)


def _make_v7(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("BEGIN")
        migrations._create_fresh_v7(connection)
        connection.commit()


def _version(path: Path) -> int:
    with sqlite3.connect(path) as connection:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])


def test_fresh_v8_repeat_and_schema_validation(tmp_path: Path) -> None:
    path = tmp_path / "fresh.db"
    database = UserDatabase(path)
    database.initialize()
    database.initialize()
    assert _version(path) == 14
    with sqlite3.connect(path) as connection:
        validate_warehouse_delivery_schema(connection)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_v7_to_v8_preserves_existing_rows(tmp_path: Path) -> None:
    path = tmp_path / "v7.db"
    _make_v7(path)
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
    assert _version(path) == 14
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT telegram_user_id, role FROM users WHERE telegram_user_id=42"
        ).fetchone() == (42, "accountant")
        validate_warehouse_delivery_schema(connection)


def test_v8_constraints_reject_invalid_recipient_and_delivery(tmp_path: Path) -> None:
    path = tmp_path / "checks.db"
    UserDatabase(path).initialize()
    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO warehouse_report_recipients (
                    chat_id, title, chat_type, created_by,
                    created_at_utc, updated_at_utc, revision
                ) VALUES (1, ' bad ', 'group', 1, 'now', 'now', 1)
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO warehouse_report_recipients (
                    chat_id, title, chat_type, created_by,
                    created_at_utc, updated_at_utc, revision
                ) VALUES (1, 'Valid', 'username', 1, 'now', 'now', 1)
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO warehouse_report_deliveries (
                    report_id, recipient_id, status, attempt_count,
                    created_at_utc, updated_at_utc
                ) VALUES (999, 999, 'delivered', 0, 'now', 'now')
                """
            )


def test_v7_to_v8_failure_rolls_back_completely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "rollback.db"
    _make_v7(path)

    def fail(connection: sqlite3.Connection) -> None:
        connection.execute("CREATE TABLE partial_v8 (id INTEGER PRIMARY KEY)")
        raise RuntimeError("simulated v8 failure")

    monkeypatch.setattr(migrations, "create_warehouse_delivery_schema", fail)
    with pytest.raises(migrations.DatabaseMigrationError, match="прервана"):
        UserDatabase(path).initialize()
    assert _version(path) == 7
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='partial_v8'"
        ).fetchone() is None
        assert connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE name='warehouse_report_recipients'"
        ).fetchone() is None


def test_v8_validation_detects_missing_index(tmp_path: Path) -> None:
    path = tmp_path / "broken.db"
    UserDatabase(path).initialize()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX ix_warehouse_report_deliveries_recipient")
        connection.commit()
    with pytest.raises(migrations.SchemaValidationError, match="Invalid indexes"):
        UserDatabase(path).initialize()
