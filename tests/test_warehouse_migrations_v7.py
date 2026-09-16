import sqlite3
from pathlib import Path

import pytest

from app.storage import migrations
from app.storage.user_database import UserDatabase
from app.storage.warehouse_v7_schema import validate_warehouse_v7_schema


def _make_v6(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("BEGIN")
        migrations._create_fresh_v6(connection)
        connection.commit()


def _version(path: Path) -> int:
    with sqlite3.connect(path) as connection:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])


def test_fresh_v7_repeat_and_three_operation_checks(tmp_path: Path) -> None:
    path = tmp_path / "fresh.db"
    database = UserDatabase(path)
    database.initialize()
    database.initialize()
    assert _version(path) == 9
    with sqlite3.connect(path) as connection:
        validate_warehouse_v7_schema(connection)
        source = connection.execute(
            "SELECT id FROM warehouse_locations WHERE stable_id='bar'"
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO warehouse_shifts (
                source_location_id, business_date, timezone_snapshot,
                cutoff_snapshot, status, started_at_utc,
                scheduled_close_at_utc, revision, created_at_utc, updated_at_utc
            ) VALUES (?, '2026-09-10', 'Europe/Kyiv', '23:00', 'open',
                      'now', 'later', 1, 'now', 'now')
            """,
            (source,),
        )
        shift_id = connection.execute(
            "SELECT id FROM warehouse_shifts"
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO warehouse_intake_batches (
                    shift_id, actor_user_id, source_chat_id, source_message_id,
                    operation_type, development_title, raw_text, status,
                    created_at_utc, updated_at_utc
                ) VALUES (?, 1, 1, 1, 'development', NULL, 'raw', 'ready',
                          'now', 'now')
                """,
                (shift_id,),
            )


def test_v6_to_v7_preserves_rows_and_adds_development_types(tmp_path: Path) -> None:
    path = tmp_path / "v6.db"
    _make_v6(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO warehouse_products (
                stable_id, official_name, normalized_name, base_unit,
                is_active, created_at_utc, updated_at_utc, revision
            ) VALUES ('one', 'One', 'one', 'kg', 1, 'now', 'now', 1)
            """
        )
        product_id = connection.execute(
            "SELECT id FROM warehouse_products WHERE stable_id='one'"
        ).fetchone()[0]
        connection.executemany(
            "INSERT INTO warehouse_product_operation_types VALUES (?, ?)",
            [(product_id, "product_writeoff"), (product_id, "transfer")],
        )
        source_id = connection.execute(
            "SELECT id FROM warehouse_locations WHERE stable_id='bar'"
        ).fetchone()[0]
        destination_id = connection.execute(
            "SELECT id FROM warehouse_locations WHERE stable_id='kitchen'"
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO warehouse_shifts (
                source_location_id, business_date, timezone_snapshot,
                cutoff_snapshot, status, started_at_utc,
                scheduled_close_at_utc, revision, created_at_utc, updated_at_utc
            ) VALUES (?, '2026-09-10', 'Europe/Kyiv', '23:00', 'open',
                      'now', 'later', 1, 'now', 'now')
            """,
            (source_id,),
        )
        shift_id = connection.execute("SELECT id FROM warehouse_shifts").fetchone()[0]
        connection.execute(
            """
            INSERT INTO warehouse_intake_batches (
                shift_id, actor_user_id, source_chat_id, source_message_id,
                operation_type, raw_text, status, created_at_utc, updated_at_utc
            ) VALUES (?, 20, 777, 1, 'writeoff', 'raw', 'committed', 'now', 'now')
            """,
            (shift_id,),
        )
        batch_id = connection.execute(
            "SELECT id FROM warehouse_intake_batches"
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO warehouse_intake_lines (
                batch_id, line_number, raw_line, status, product_text, product_id,
                input_quantity_text, quantity_scaled, base_unit,
                created_at_utc, updated_at_utc
            ) VALUES (?, 1, 'One 1 kg', 'committed', 'One', ?, '1', 1000000,
                      'kg', 'now', 'now')
            """,
            (batch_id, product_id),
        )
        line_id = connection.execute(
            "SELECT id FROM warehouse_intake_lines"
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO warehouse_operations (
                shift_id, operation_type, product_id, product_name_snapshot,
                quantity_scaled, base_unit, input_quantity_text,
                source_location_id, destination_location_id, raw_text,
                author_user_id, occurred_at_utc, intake_line_id, origin,
                is_deleted, created_at_utc, updated_at_utc, revision
            ) VALUES (?, 'writeoff', ?, 'One', 1000000, 'kg', '1', ?, NULL,
                      'One 1 kg', 20, 'now', ?, 'message', 0, 'now', 'now', 1)
            """,
            (shift_id, product_id, source_id, line_id),
        )
        operation_id = connection.execute(
            "SELECT id FROM warehouse_operations"
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO warehouse_audit_events (
                shift_id, operation_id, actor_kind, actor_user_id, event_type,
                entity_type, entity_id, occurred_at_utc, correlation_id
            ) VALUES (?, ?, 'user', 20, 'operation_created', 'operation', ?,
                      'now', 'old')
            """,
            (shift_id, operation_id, operation_id),
        )
        connection.execute(
            """
            INSERT INTO warehouse_reports (
                shift_id, version, report_kind, status, created_at_utc, created_by
            ) VALUES (?, 1, 'final', 'building', 'now', 20)
            """,
            (shift_id,),
        )
        report_id = connection.execute("SELECT id FROM warehouse_reports").fetchone()[0]
        connection.execute(
            """
            INSERT INTO warehouse_report_lines (
                report_id, sheet_type, row_number, product_id, product_name,
                quantity_scaled, base_unit, source_location_name,
                destination_location_name, business_date
            ) VALUES (?, 'transfer', 1, ?, 'One', 1000000, 'kg', 'Бар',
                      'Кухня', '2026-09-10')
            """,
            (report_id, product_id),
        )
        connection.commit()

    UserDatabase(path).initialize()
    assert _version(path) == 9
    with sqlite3.connect(path) as connection:
        validate_warehouse_v7_schema(connection)
        assert connection.execute(
            "SELECT operation_type FROM warehouse_product_operation_types "
            "WHERE product_id=? ORDER BY operation_type",
            (product_id,),
        ).fetchall() == [
            ("development",),
            ("product_writeoff",),
            ("transfer",),
        ]
        assert connection.execute(
            "SELECT operation_type, product_name_snapshot, development_title "
            "FROM warehouse_operations"
        ).fetchall() == [("writeoff", "One", None)]
        assert connection.execute(
            "SELECT sheet_type, destination_location_name, development_title "
            "FROM warehouse_report_lines"
        ).fetchall() == [("transfer", "Кухня", None)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_v7_replacement_failure_rolls_back_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "rollback.db"
    _make_v6(path)
    original = migrations.migrate_warehouse_v6_to_v7

    def fail_after_replacement(connection: sqlite3.Connection) -> None:
        original(connection)
        connection.execute("CREATE TABLE partial_v7 (id INTEGER PRIMARY KEY)")
        raise RuntimeError("simulated v7 failure")

    monkeypatch.setattr(migrations, "migrate_warehouse_v6_to_v7", fail_after_replacement)
    with pytest.raises(migrations.DatabaseMigrationError, match="прервана"):
        UserDatabase(path).initialize()
    assert _version(path) == 6
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='partial_v7'"
        ).fetchone() is None
        assert connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE name='warehouse_development_title_drafts'"
        ).fetchone() is None


def test_future_schema_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    UserDatabase(path).initialize()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=10")
    with pytest.raises(migrations.UnsupportedSchemaVersionError):
        UserDatabase(path).initialize()
