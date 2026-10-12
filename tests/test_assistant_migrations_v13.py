"""Schema-v13 persistent assistant conversation migration."""

import sqlite3
from pathlib import Path

import pytest

from app.storage import migrations
from app.storage.user_database import UserDatabase


def test_fresh_v13_is_valid_and_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "fresh.db"
    UserDatabase(path).initialize()
    UserDatabase(path).initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 13
        migrations._validate_schema(connection, 13)
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_v12_to_v13_and_atomic_rollback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "upgrade.db"
    UserDatabase(path).initialize()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE assistant_conversation_turns")
        connection.execute("DROP TABLE assistant_conversation_sessions")
        connection.execute("PRAGMA user_version=12")
        connection.commit()
    UserDatabase(path).initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 13

    rollback = tmp_path / "rollback.db"
    UserDatabase(rollback).initialize()
    with sqlite3.connect(rollback) as connection:
        connection.execute("DROP TABLE assistant_conversation_turns")
        connection.execute("DROP TABLE assistant_conversation_sessions")
        connection.execute("PRAGMA user_version=12")
        connection.commit()

    def fail(connection: sqlite3.Connection) -> None:
        connection.execute("CREATE TABLE partial_v13(id INTEGER PRIMARY KEY)")
        raise RuntimeError("simulated v13 failure")

    monkeypatch.setattr(migrations, "create_assistant_conversation_schema", fail)
    with pytest.raises(migrations.DatabaseMigrationError):
        UserDatabase(rollback).initialize()
    with sqlite3.connect(rollback) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 12
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='partial_v13'"
        ).fetchone() is None


def test_v13_validator_rejects_missing_index(tmp_path: Path) -> None:
    path = tmp_path / "broken.db"
    UserDatabase(path).initialize()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX idx_assistant_sessions_expiry")
        connection.commit()
    with pytest.raises(migrations.SchemaValidationError):
        UserDatabase(path).initialize()
