"""Atomic schema-v15 long-term-memory migration regressions."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.storage import migrations
from app.storage.user_database import UserDatabase


def _prepare_v14(path: Path) -> None:
    UserDatabase(path).initialize()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX ux_assistant_memory_active_content")
        connection.execute("DROP INDEX ix_assistant_memory_owner_active")
        connection.execute("DROP TABLE assistant_memory_delete_previews")
        connection.execute("DROP TABLE assistant_long_term_memories")
        connection.execute("PRAGMA user_version=14")


def _objects(connection: sqlite3.Connection) -> tuple[tuple[str, str, str], ...]:
    return tuple(connection.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name"
    ))


def test_fresh_v15_and_v14_upgrade_are_valid_and_idempotent(tmp_path: Path) -> None:
    fresh = tmp_path / "fresh.db"
    UserDatabase(fresh).initialize(); UserDatabase(fresh).initialize()
    upgrade = tmp_path / "upgrade.db"
    _prepare_v14(upgrade)
    UserDatabase(upgrade).initialize(); UserDatabase(upgrade).initialize()
    for path in (fresh, upgrade):
        with sqlite3.connect(path) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 15
            migrations._validate_schema(connection, 15)
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_v15_post_create_validation_failure_is_real_transaction_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "rollback.db"
    _prepare_v14(path)
    with sqlite3.connect(path) as connection:
        before = _objects(connection)
    real = migrations.validate_assistant_memory_schema

    def fail_after_validation(connection: sqlite3.Connection) -> None:
        real(connection)
        raise RuntimeError("post-create failure")

    monkeypatch.setattr(migrations, "validate_assistant_memory_schema", fail_after_validation)
    with sqlite3.connect(path, isolation_level=None) as connection:
        with pytest.raises(migrations.DatabaseMigrationError):
            migrations.run_migrations(connection)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 14
        assert _objects(connection) == before
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    monkeypatch.setattr(migrations, "validate_assistant_memory_schema", real)
    UserDatabase(path).initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 15


def test_v15_failure_after_table_creation_rolls_back(tmp_path: Path) -> None:
    path = tmp_path / "first-object.db"
    _prepare_v14(path)
    with sqlite3.connect(path) as connection:
        before = _objects(connection)

    def reject_first_index(action, argument1, _argument2, _database, _trigger):
        if action == sqlite3.SQLITE_CREATE_INDEX and argument1 == "ix_assistant_memory_owner_active":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    with sqlite3.connect(path, isolation_level=None) as connection:
        connection.set_authorizer(reject_first_index)
        with pytest.raises(migrations.DatabaseMigrationError):
            migrations.run_migrations(connection)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 14
        assert _objects(connection) == before


def test_fresh_v15_failure_rolls_back_to_empty_database(tmp_path: Path) -> None:
    path = tmp_path / "fresh-failure.db"

    def reject_memory_index(action, argument1, _argument2, _database, _trigger):
        if action == sqlite3.SQLITE_CREATE_INDEX and argument1 == "ix_assistant_memory_owner_active":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    with sqlite3.connect(path, isolation_level=None) as connection:
        connection.set_authorizer(reject_memory_index)
        with pytest.raises(migrations.DatabaseMigrationError):
            migrations.run_migrations(connection)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert _objects(connection) == ()
    UserDatabase(path).initialize()


def test_v15_rejects_invalid_memory_states(tmp_path: Path) -> None:
    path = tmp_path / "constraints.db"
    UserDatabase(path).initialize()
    with sqlite3.connect(path) as connection:
        base = ("abcdefghijklmnop", 1, 1, "user", "terminology", "x", "explicit_remember", "active", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z")
        sql = """INSERT INTO assistant_long_term_memories(
            opaque_token,owner_user_id,source_chat_id,scope,kind,normalized_content,source,status,
            created_at_utc,updated_at_utc,revision) VALUES(?,?,?,?,?,?,?,?,?,?,?)"""
        for revision in (0, -1):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(sql, (*base, revision))


def test_real_v14_state_survives_upgrade_and_repeat_initialize(tmp_path):
    from datetime import UTC,datetime
    from app.services.assistant.context import PersistentAssistantConversationMemory
    from app.storage.assistant_conversation_database import AssistantConversationDatabase
    from app.storage.assistant_action_database import AssistantActionDatabase
    path=tmp_path/'real-v14.db'
    with sqlite3.connect(path,isolation_level=None) as connection:
        connection.execute('PRAGMA foreign_keys=ON')
        migrations._run_atomic_step(connection,migrations._create_fresh_v14,disable_foreign_keys=True)
        assert connection.execute('PRAGMA user_version').fetchone()[0]==14
    now=datetime(2026,1,1,tzinfo=UTC)
    memory=PersistentAssistantConversationMemory(AssistantConversationDatabase(path),clock=lambda:now)
    memory.remember(1,chat_id=10,last_intent='catalog.get',last_entity_type='catalog',
        last_query='Облепиха',selected_entity_id='preparations/_/sea')
    memory.append_turn(1,chat_id=10,role='user',text='synthetic')
    AssistantActionDatabase(path).create(dict(
        opaque_token='abcdefghijklmnop',actor_user_id=1,source_chat_id=10,source_message_id=1,
        capability_id='attendance.open_own',risk_level=2,arguments={},preview={'action':'open'},
        created_at_utc='2026-01-01T00:00:00Z',expires_at_utc='2026-01-01T00:10:00Z'),
        '2026-01-01T00:00:00Z')
    def snapshot():
        with sqlite3.connect(path) as connection:
            return {table:connection.execute(f'SELECT * FROM {table} ORDER BY id').fetchall()
                for table in ('assistant_conversation_sessions','assistant_conversation_turns','assistant_action_proposals')}
    baseline=snapshot()
    UserDatabase(path).initialize()
    assert snapshot()==baseline
    UserDatabase(path).initialize()
    assert snapshot()==baseline


def test_failure_after_all_objects_before_version_rolls_back(tmp_path):
    path=tmp_path/'all-objects.db'
    _prepare_v14(path)
    with sqlite3.connect(path) as connection:
        baseline=_objects(connection)
    def reject_version(action,argument1,argument2,_database,_trigger):
        if action==sqlite3.SQLITE_PRAGMA and argument1=='user_version' and argument2=='15':
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    with sqlite3.connect(path,isolation_level=None) as connection:
        connection.set_authorizer(reject_version)
        with pytest.raises(migrations.DatabaseMigrationError):
            migrations.run_migrations(connection)
    with sqlite3.connect(path) as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0]==14
        assert _objects(connection)==baseline
        assert connection.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert not connection.execute('PRAGMA foreign_key_check').fetchall()
