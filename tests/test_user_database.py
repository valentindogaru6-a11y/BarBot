"""Tests for the SQLite user repository and service."""

from pathlib import Path

import pytest
from aiogram.types import User

from app.models.user import Role
from app.services.user_service import UserService
from app.storage import user_database as database_module
from app.storage.user_database import UserDatabase, UserDatabaseError


def _database(tmp_path: Path) -> UserDatabase:
    database = UserDatabase(tmp_path / "barbot.db")
    database.initialize()
    return database


def test_creates_and_reinitializes_database(tmp_path: Path) -> None:
    database = _database(tmp_path)
    database.initialize()

    assert database.path.exists()
    assert database.settings() == {
        "journal_mode": "wal",
        "busy_timeout": 5000,
        "foreign_keys": 1,
        "user_version": 10,
    }


def test_registers_and_updates_profile_without_resetting_role(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    service = UserService(database)
    first = User(id=100, is_bot=False, first_name="Иван", username="old")
    service.sync_telegram_user(first)
    database.set_role(100, Role.BARTENDER, actor_user_id=1)
    second = User(id=100, is_bot=False, first_name="Иван", username="new")

    updated = service.sync_telegram_user(second)

    assert updated.username == "new"
    assert updated.role is Role.BARTENDER
    assert updated.has_started


def test_updates_last_seen_at(tmp_path: Path, monkeypatch) -> None:
    values = iter(["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"])
    monkeypatch.setattr(database_module, "_utc_now", lambda: next(values))
    database = _database(tmp_path)
    user = User(id=101, is_bot=False, first_name="Анна")

    first = database.sync_user(
        user.id,
        username=None,
        first_name=user.first_name,
        last_name=None,
    )
    second = database.sync_user(
        user.id,
        username=None,
        first_name=user.first_name,
        last_name=None,
    )

    assert first.first_seen_at == "2026-01-01T00:00:00Z"
    assert second.last_seen_at == "2026-01-02T00:00:00Z"


def test_searches_by_id_username_and_name(tmp_path: Path) -> None:
    database = _database(tmp_path)
    database.sync_user(
        200,
        username="LocalUser",
        first_name="Мария",
        last_name="Бармен",
    )

    assert database.search_users("200")[0].telegram_user_id == 200
    assert database.search_users("@localuser")[0].telegram_user_id == 200
    assert database.search_users("мария")[0].telegram_user_id == 200
    assert database.search_users("барм")[0].telegram_user_id == 200


def test_search_returns_multiple_local_matches(tmp_path: Path) -> None:
    database = _database(tmp_path)
    for user_id, username in ((201, "anna_one"), (202, "anna_two")):
        database.sync_user(
            user_id,
            username=username,
            first_name="Анна",
            last_name=None,
        )

    assert {
        user.telegram_user_id for user in database.search_users("anna")
    } == {201, 202}


def test_sql_injection_text_is_only_search_data(tmp_path: Path) -> None:
    database = _database(tmp_path)
    database.sync_user(
        300,
        username="safe",
        first_name="Safe",
        last_name=None,
    )

    assert database.search_users("'; DROP TABLE users; --") == []
    assert database.get_user(300) is not None
    assert database.count_users() == 1


def test_user_service_paginates_and_counts_roles(tmp_path: Path) -> None:
    database = _database(tmp_path)
    service = UserService(database)
    for user_id in range(1, 12):
        database.ensure_user(user_id)
    database.set_role(1, Role.ADMIN, actor_user_id=99)
    database.set_role(2, Role.BARTENDER, actor_user_id=99)

    users, page, pages = service.list_users(page=1, page_size=5)
    counts = service.count_by_role()

    assert len(users) == 5
    assert (page, pages) == (1, 3)
    assert counts[Role.ADMIN] == 1
    assert counts[Role.BARTENDER] == 1
    assert counts[Role.ACCOUNTANT] == 0
    assert counts[Role.USER] == 9


def test_super_admin_cannot_be_stored_as_sqlite_role(tmp_path: Path) -> None:
    database = _database(tmp_path)
    database.ensure_user(12)

    with pytest.raises(UserDatabaseError, match="Недопустимая"):
        database.set_role(12, Role.SUPER_ADMIN, actor_user_id=1)
