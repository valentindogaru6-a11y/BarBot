"""Schema-v13 persistent assistant conversation migration."""

import sqlite3
from pathlib import Path

import pytest

from app.storage import migrations
from app.storage.user_database import UserDatabase


def _prepare_v12(path: Path) -> None:
    database = UserDatabase(path)
    database.initialize()
    database.ensure_user(101, first_name="Migration marker")
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE assistant_conversation_turns")
        connection.execute("DROP TABLE assistant_conversation_sessions")
        connection.execute("PRAGMA user_version=12")
        connection.commit()


def _schema_snapshot(connection: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    return tuple(
        connection.execute(
            """SELECT type, name, tbl_name, sql
               FROM sqlite_master
               WHERE name NOT LIKE 'sqlite_%'
               ORDER BY type, name"""
        ).fetchall()
    )


def _data_snapshot(connection: sqlite3.Connection) -> dict[str, tuple[tuple[object, ...], ...]]:
    tables = connection.execute(
        """SELECT name FROM sqlite_master
           WHERE type='table' AND name NOT LIKE 'sqlite_%'
           ORDER BY name"""
    ).fetchall()
    return {
        str(table[0]): tuple(
            connection.execute(
                f'SELECT * FROM "{str(table[0]).replace(chr(34), chr(34) * 2)}"'
            ).fetchall()
        )
        for table in tables
    }


def _v12_baseline(path: Path) -> tuple[tuple[tuple[object, ...], ...], dict[str, tuple[tuple[object, ...], ...]]]:
    with sqlite3.connect(path) as connection:
        return _schema_snapshot(connection), _data_snapshot(connection)


def _assert_v12_rollback(
    path: Path,
    baseline: tuple[tuple[tuple[object, ...], ...], dict[str, tuple[tuple[object, ...], ...]]],
) -> None:
    expected_schema, expected_data = baseline
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 12
        assert _schema_snapshot(connection) == expected_schema
        assert _data_snapshot(connection) == expected_data
        assert connection.execute(
            """SELECT name FROM sqlite_master
               WHERE name LIKE 'assistant_conversation_%'
                  OR name LIKE 'idx_assistant_%'"""
        ).fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _run_direct(path: Path, *, authorizer=None) -> None:
    with sqlite3.connect(path, isolation_level=None) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        if authorizer is not None:
            connection.set_authorizer(authorizer)
        migrations.run_migrations(connection)


def test_fresh_v13_is_valid_and_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "fresh.db"
    UserDatabase(path).initialize()
    UserDatabase(path).initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 13
        migrations._validate_schema(connection, 13)
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_v12_to_v13_is_valid_and_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "upgrade.db"
    _prepare_v12(path)
    UserDatabase(path).initialize()
    UserDatabase(path).initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 13
        migrations._validate_schema(connection, 13)
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_v12_to_v13_rolls_back_after_first_table(tmp_path: Path) -> None:
    path = tmp_path / "first-table.db"
    _prepare_v12(path)
    baseline = _v12_baseline(path)

    def reject_first_assistant_index(
        action: int,
        argument1: str | None,
        _argument2: str | None,
        _database: str | None,
        _trigger: str | None,
    ) -> int:
        if action == sqlite3.SQLITE_CREATE_INDEX and argument1 == "idx_assistant_sessions_expiry":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    with pytest.raises(migrations.DatabaseMigrationError):
        _run_direct(path, authorizer=reject_first_assistant_index)
    _assert_v12_rollback(path, baseline)


def test_v12_to_v13_rolls_back_after_all_schema_objects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "all-objects.db"
    _prepare_v12(path)
    baseline = _v12_baseline(path)
    create_schema = migrations.create_assistant_conversation_schema

    def create_then_fail(connection: sqlite3.Connection) -> None:
        create_schema(connection)
        raise RuntimeError("simulated failure after v13 DDL")

    monkeypatch.setattr(migrations, "create_assistant_conversation_schema", create_then_fail)
    with pytest.raises(migrations.DatabaseMigrationError):
        _run_direct(path)
    _assert_v12_rollback(path, baseline)

    monkeypatch.setattr(migrations, "create_assistant_conversation_schema", create_schema)
    UserDatabase(path).initialize()
    UserDatabase(path).initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 13


def test_v12_to_v13_rolls_back_after_user_version_before_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "post-validation.db"
    _prepare_v12(path)
    baseline = _v12_baseline(path)
    validate_schema = migrations.validate_assistant_conversation_schema

    def validate_then_fail(connection: sqlite3.Connection) -> None:
        validate_schema(connection)
        raise RuntimeError("simulated post-create validation failure")

    monkeypatch.setattr(
        migrations,
        "validate_assistant_conversation_schema",
        validate_then_fail,
    )
    with pytest.raises(migrations.DatabaseMigrationError):
        _run_direct(path)
    _assert_v12_rollback(path, baseline)

    monkeypatch.setattr(
        migrations,
        "validate_assistant_conversation_schema",
        validate_schema,
    )
    UserDatabase(path).initialize()
    UserDatabase(path).initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 13


def test_fresh_v13_failure_rolls_back_entire_schema_and_can_retry(tmp_path: Path) -> None:
    path = tmp_path / "fresh-rollback.db"

    def reject_assistant_index(
        action: int,
        argument1: str | None,
        _argument2: str | None,
        _database: str | None,
        _trigger: str | None,
    ) -> int:
        if action == sqlite3.SQLITE_CREATE_INDEX and argument1 == "idx_assistant_sessions_expiry":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    with pytest.raises(migrations.DatabaseMigrationError):
        _run_direct(path, authorizer=reject_assistant_index)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert _schema_snapshot(connection) == ()
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    _run_direct(path)
    _run_direct(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 13
        migrations._validate_schema(connection, 13)


def test_v13_validator_rejects_missing_index(tmp_path: Path) -> None:
    path = tmp_path / "broken.db"
    UserDatabase(path).initialize()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX idx_assistant_sessions_expiry")
        connection.commit()
    with pytest.raises(migrations.SchemaValidationError):
        UserDatabase(path).initialize()
