"""Atomic schema-v14 action-proposal migration regressions."""

import sqlite3
from pathlib import Path

import pytest

from app.storage import migrations
from app.storage.user_database import UserDatabase


def _prepare_v13(path: Path) -> None:
    UserDatabase(path).initialize()
    UserDatabase(path).ensure_user(101, first_name="v14 marker")
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE assistant_action_proposals")
        connection.execute("PRAGMA user_version=13")
        connection.commit()


def _snapshot(path: Path) -> tuple[tuple[tuple[object, ...], ...], tuple[tuple[object, ...], ...]]:
    with sqlite3.connect(path) as connection:
        schema = tuple(connection.execute(
            """SELECT type,name,tbl_name,sql FROM sqlite_master
               WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"""
        ))
        users = tuple(connection.execute("SELECT * FROM users ORDER BY telegram_user_id"))
        return schema, users


def _assert_v13(path: Path, baseline: object) -> None:
    assert _snapshot(path) == baseline
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 13
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name LIKE 'assistant_action_%' OR name LIKE '%assistant_action%'"
        ).fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _run(path: Path, authorizer=None) -> None:
    with sqlite3.connect(path, isolation_level=None) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        if authorizer:
            connection.set_authorizer(authorizer)
        migrations.run_migrations(connection)


def test_fresh_v14_and_v13_upgrade_are_valid_and_idempotent(tmp_path: Path) -> None:
    fresh = tmp_path / "fresh.db"
    UserDatabase(fresh).initialize()
    UserDatabase(fresh).initialize()
    with sqlite3.connect(fresh) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 14
        migrations._validate_schema(connection, 14)

    upgrade = tmp_path / "upgrade.db"
    _prepare_v13(upgrade)
    UserDatabase(upgrade).initialize()
    UserDatabase(upgrade).initialize()
    with sqlite3.connect(upgrade) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 14
        assert connection.execute("SELECT COUNT(*) FROM assistant_action_proposals").fetchone()[0] == 0
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_v14_rolls_back_after_table_creation(tmp_path: Path) -> None:
    path = tmp_path / "first-object.db"
    _prepare_v13(path)
    baseline = _snapshot(path)

    def reject_index(action, argument1, _argument2, _database, _trigger):
        if action == sqlite3.SQLITE_CREATE_INDEX and argument1 == "ux_assistant_action_actor_active":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    with pytest.raises(migrations.DatabaseMigrationError):
        _run(path, reject_index)
    _assert_v13(path, baseline)
    _run(path)


def test_v14_rolls_back_after_user_version_and_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "post-validation.db"
    _prepare_v13(path)
    baseline = _snapshot(path)
    real_validate = migrations.validate_assistant_action_schema

    def validate_then_fail(connection: sqlite3.Connection) -> None:
        real_validate(connection)
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 14
        raise RuntimeError("post-create validation failure")

    monkeypatch.setattr(migrations, "validate_assistant_action_schema", validate_then_fail)
    with pytest.raises(migrations.DatabaseMigrationError):
        _run(path)
    _assert_v13(path, baseline)


def test_fresh_v14_failure_rolls_back_to_empty_database(tmp_path: Path) -> None:
    path = tmp_path / "fresh-failure.db"

    def reject_index(action, argument1, _argument2, _database, _trigger):
        if action == sqlite3.SQLITE_CREATE_INDEX and argument1 == "ux_assistant_action_actor_active":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    with pytest.raises(migrations.DatabaseMigrationError):
        _run(path, reject_index)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall() == []
    _run(path)


def test_future_v15_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=15")
    with pytest.raises(migrations.UnsupportedSchemaVersionError):
        _run(path)


@pytest.mark.parametrize(
    "overrides",
    [
        {"arguments_json": "not-json"},
        {"preview_json": "not-json"},
        {"status": "confirmed", "confirmed_at_utc": None},
        {
            "status": "executed",
            "confirmed_at_utc": "2026-10-13T00:00:01Z",
            "executed_at_utc": "2026-10-13T00:00:02Z",
            "result_json": None,
        },
        {
            "status": "failed",
            "confirmed_at_utc": "2026-10-13T00:00:01Z",
            "result_json": None,
        },
        {
            "status": "cancelled",
            "cancelled_at_utc": None,
        },
    ],
)
def test_v14_rejects_impossible_proposal_states(
    tmp_path: Path,
    overrides: dict[str, object],
) -> None:
    path = tmp_path / "constraints.db"
    UserDatabase(path).initialize()
    values: dict[str, object] = {
        "opaque_token": "abcdefghijklmnop",
        "actor_user_id": 1,
        "source_chat_id": 1,
        "source_message_id": 1,
        "capability_id": "attendance.open_own",
        "risk_level": 2,
        "status": "ready",
        "arguments_json": "{}",
        "preview_json": "{}",
        "created_at_utc": "2026-10-13T00:00:00Z",
        "expires_at_utc": "2026-10-13T00:10:00Z",
        "confirmed_at_utc": None,
        "executed_at_utc": None,
        "cancelled_at_utc": None,
        "result_json": None,
        "revision": 1,
    }
    values.update(overrides)
    columns = ",".join(values)
    placeholders = ",".join("?" for _ in values)
    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"INSERT INTO assistant_action_proposals ({columns}) VALUES ({placeholders})",
                tuple(values.values()),
            )
