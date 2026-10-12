"""Schema-v5 migration coverage for required write-offs and closing."""

import sqlite3
from pathlib import Path

import pytest

from app.storage import migrations
from app.storage.migrations import SchemaValidationError
from app.storage.user_database import UserDatabase
from app.storage.warehouse_closing_schema import WAREHOUSE_CLOSING_TABLES


def _version(path: Path) -> int:
    with sqlite3.connect(path) as connection:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])


def _make_v4(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("BEGIN")
        migrations._create_fresh_v6(connection)
        connection.execute("DROP TABLE warehouse_report_lines")
        connection.execute("DROP TABLE warehouse_reports")
        connection.execute("DROP TRIGGER trg_warehouse_products_required_rule_unit")
        connection.execute("DROP TABLE warehouse_closing_sessions")
        connection.execute("DROP TABLE warehouse_shift_requirements")
        connection.execute("DROP TABLE warehouse_required_rules")
        connection.execute("PRAGMA user_version=4")
        connection.commit()


def test_fresh_database_contains_v5_schema_and_repeat_is_idempotent(
    tmp_path: Path,
) -> None:
    path = tmp_path / "fresh.db"
    database = UserDatabase(path)
    database.initialize()
    database.initialize()

    assert _version(path) == 14
    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert set(WAREHOUSE_CLOSING_TABLES) <= tables


def test_sequential_v4_to_v5_preserves_existing_rows(tmp_path: Path) -> None:
    path = tmp_path / "sequential.db"
    _make_v4(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO users (
                telegram_user_id, role, is_active, first_seen_at,
                last_seen_at, updated_at, has_started
            ) VALUES (42, 'bartender', 1, 'now', 'now', 'now', 1)
            """
        )
        connection.commit()

    UserDatabase(path).initialize()

    assert _version(path) == 14
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT role FROM users WHERE telegram_user_id=42"
        ).fetchone()[0] == "bartender"


def test_v4_to_v5_failure_rolls_back_whole_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "rollback.db"
    _make_v4(path)

    def fail(connection: sqlite3.Connection) -> None:
        connection.execute("CREATE TABLE partial_v5 (id INTEGER PRIMARY KEY)")
        raise RuntimeError("simulated v5 failure")

    monkeypatch.setattr(migrations, "create_warehouse_closing_schema", fail)
    with pytest.raises(migrations.DatabaseMigrationError, match="прервана"):
        UserDatabase(path).initialize()

    assert _version(path) == 4
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='partial_v5'"
        ).fetchone() is None


def test_v5_validation_detects_missing_partial_unique_index(tmp_path: Path) -> None:
    path = tmp_path / "broken.db"
    UserDatabase(path).initialize()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX ux_warehouse_closing_sessions_active")
        connection.commit()

    with pytest.raises(SchemaValidationError, match="Invalid indexes"):
        UserDatabase(path).initialize()


def test_v5_checks_reject_rule_without_product(tmp_path: Path) -> None:
    path = tmp_path / "fk.db"
    UserDatabase(path).initialize()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO warehouse_required_rules (
                    stable_id, product_id, operation_type, quantity_mode,
                    suggested_quantity_scaled, base_unit, weekdays_mask,
                    question_text, sort_order, allow_not_applicable,
                    is_active, created_at_utc, updated_at_utc, revision
                ) VALUES (
                    'missing', 999, 'writeoff', 'recommended', 1, 'kg',
                    127, 'Question', 0, 1, 1, 'now', 'now', 1
                )
                """
            )
        connection.execute(
            """
            INSERT INTO warehouse_products (
                stable_id, official_name, normalized_name, base_unit,
                is_active, created_at_utc, updated_at_utc, revision
            ) VALUES ('unit-owner', 'Unit owner', 'unit owner', 'kg',
                      1, 'now', 'now', 1)
            """
        )
        product_id = connection.execute(
            "SELECT id FROM warehouse_products WHERE stable_id='unit-owner'"
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="unit does not match"):
            connection.execute(
                """
                INSERT INTO warehouse_required_rules (
                    stable_id, product_id, operation_type, quantity_mode,
                    suggested_quantity_scaled, base_unit, weekdays_mask,
                    question_text, sort_order, allow_not_applicable,
                    is_active, created_at_utc, updated_at_utc, revision
                ) VALUES (
                    'wrong-unit', ?, 'writeoff', 'recommended', 1, 'l',
                    127, 'Question', 0, 1, 1, 'now', 'now', 1
                )
                """,
                (product_id,),
            )
