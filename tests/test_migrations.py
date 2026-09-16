"""Tests for versioned SQLite initialization and migrations."""

import sqlite3
import stat
from pathlib import Path

import pytest

from app.models.user import Role
from app.storage import migrations
from app.storage.migrations import (
    DatabaseMigrationError,
    SchemaValidationError,
    UnsupportedSchemaVersionError,
)
from app.storage.user_database import UserDatabase
from app.storage.warehouse_database import WarehouseDatabase
from app.storage.warehouse_schema import (
    WAREHOUSE_TABLES,
    seed_warehouse_defaults,
)

V1_SCHEMA = """
CREATE TABLE users (
    telegram_user_id INTEGER PRIMARY KEY,
    username TEXT NULL,
    first_name TEXT NULL,
    last_name TEXT NULL,
    role TEXT NOT NULL DEFAULT 'user'
        CHECK (role IN ('admin', 'bartender', 'user', 'blocked')),
    is_active INTEGER NOT NULL DEFAULT 1
        CHECK (is_active IN (0, 1)),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    created_by INTEGER NULL,
    updated_at TEXT NOT NULL,
    has_started INTEGER NOT NULL DEFAULT 0
        CHECK (has_started IN (0, 1))
)
"""

V2_SCHEMA = V1_SCHEMA.replace(
    "'admin', 'bartender', 'user', 'blocked'",
    "'admin', 'bartender', 'accountant', 'user', 'blocked'",
)

USER_COLUMNS = (
    "telegram_user_id",
    "username",
    "first_name",
    "last_name",
    "role",
    "is_active",
    "first_seen_at",
    "last_seen_at",
    "created_by",
    "updated_at",
    "has_started",
)


def _create_v1(path: Path) -> list[tuple[object, ...]]:
    rows = [
        (
            101,
            "admin_name",
            "Админ",
            "Первый",
            "admin",
            1,
            "2025-01-01T01:02:03Z",
            "2026-02-03T04:05:06Z",
            None,
            "2026-02-03T04:05:06Z",
            1,
        ),
        (
            202,
            None,
            "Бармен",
            None,
            "bartender",
            1,
            "2025-02-01T00:00:00Z",
            "2025-02-02T00:00:00Z",
            101,
            "2025-02-02T00:00:00Z",
            0,
        ),
        (
            303,
            "local_user",
            None,
            "Сотрудник",
            "user",
            1,
            "2025-03-01T00:00:00Z",
            "2025-03-02T00:00:00Z",
            101,
            "2025-03-02T00:00:00Z",
            1,
        ),
        (
            404,
            "blocked_user",
            "Бывший",
            "Сотрудник",
            "blocked",
            0,
            "2025-04-01T00:00:00Z",
            "2025-04-02T00:00:00Z",
            101,
            "2025-04-02T00:00:00Z",
            1,
        ),
    ]
    connection = sqlite3.connect(path)
    try:
        connection.execute(V1_SCHEMA)
        placeholders = ", ".join("?" for _ in USER_COLUMNS)
        connection.executemany(
            f"INSERT INTO users ({', '.join(USER_COLUMNS)}) VALUES ({placeholders})",
            rows,
        )
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
    finally:
        connection.close()
    return rows


def _read_rows(path: Path) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(
            f"SELECT {', '.join(USER_COLUMNS)} "
            "FROM users ORDER BY telegram_user_id"
        ).fetchall()
    finally:
        connection.close()


def _create_v2(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(V2_SCHEMA)
        connection.execute("PRAGMA user_version = 2")
        connection.commit()
    finally:
        connection.close()


def _version(path: Path) -> int:
    connection = sqlite3.connect(path)
    try:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()


def test_migrates_v1_through_v2_to_v3_and_preserves_every_user_column(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.db"
    expected = _create_v1(path)

    database = UserDatabase(path)
    database.initialize()

    assert database.settings()["user_version"] == 11
    assert _read_rows(path) == expected
    previous, updated = database.set_role(
        303,
        Role.ACCOUNTANT,
        actor_user_id=101,
    )
    assert previous is Role.USER
    assert updated.role is Role.ACCOUNTANT


def test_repeated_initialize_on_v6_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "repeat.db"
    database = UserDatabase(path)
    database.initialize()
    stored = database.ensure_user(
        505,
        username="unchanged",
        first_name="Пользователь",
        created_by=101,
    )
    database.set_role(505, Role.ACCOUNTANT, actor_user_id=101)

    database.initialize()

    actual = database.get_user(505)
    assert actual is not None
    assert actual.role is Role.ACCOUNTANT
    assert actual.username == stored.username
    assert actual.first_seen_at == stored.first_seen_at
    assert database.count_users() == 1


def test_fresh_database_is_created_directly_as_v6(tmp_path: Path) -> None:
    database = UserDatabase(tmp_path / "fresh.db")

    database.initialize()

    assert database.settings()["user_version"] == 11
    database.ensure_user(606)
    assert database.set_role(
        606,
        Role.ACCOUNTANT,
        actor_user_id=101,
    )[1].role is Role.ACCOUNTANT
    assert stat.S_IMODE((tmp_path / "fresh.db").stat().st_mode) == 0o600


def test_rejects_unknown_future_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA user_version = 99")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(UnsupportedSchemaVersionError, match="новее"):
        UserDatabase(path).initialize()

    assert _version(path) == 99


def test_rejects_incompatible_v1_schema(tmp_path: Path) -> None:
    path = tmp_path / "broken-v1.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE users (telegram_user_id INTEGER PRIMARY KEY, role TEXT)"
        )
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(SchemaValidationError, match="ожидаемой схеме v1"):
        UserDatabase(path).initialize()

    assert _version(path) == 1


def test_rejects_v2_version_with_v1_role_constraint(tmp_path: Path) -> None:
    path = tmp_path / "broken-v2.db"
    _create_v1(path)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA user_version = 2")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(SchemaValidationError, match="ролей.*v2"):
        UserDatabase(path).initialize()

    assert _version(path) == 2


def test_migrates_v2_to_v3_with_default_location_and_settings(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v2.db"
    _create_v2(path)

    UserDatabase(path).initialize()

    assert _version(path) == 11
    warehouse = WarehouseDatabase(path)
    locations = warehouse.list_locations()
    settings = warehouse.get_settings()
    assert {location.stable_id for location in locations} == {
        "bar", "kitchen", "confectionery"
    }
    bar = next(location for location in locations if location.stable_id == "bar")
    assert bar.name == "Бар"
    assert bar.is_active
    assert bar.can_be_source
    assert bar.can_be_destination
    assert all(
        not location.can_be_source and location.can_be_destination
        for location in locations if location.stable_id != "bar"
    )
    assert settings.timezone == "Europe/Kyiv"
    assert settings.cutoff_local_time == "23:00"
    assert settings.default_source_location_id == bar.id


def test_v3_seed_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "seed.db"
    UserDatabase(path).initialize()
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        seed_warehouse_defaults(connection)
        seed_warehouse_defaults(connection)
        connection.commit()
        assert connection.execute(
            "SELECT COUNT(*) FROM warehouse_locations WHERE stable_id = 'bar'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM warehouse_settings WHERE id = 1"
        ).fetchone()[0] == 1
    finally:
        connection.close()


def test_rejects_corrupted_v3_schema(tmp_path: Path) -> None:
    path = tmp_path / "broken-v3.db"
    UserDatabase(path).initialize()
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP INDEX ux_warehouse_products_stable_id")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(SchemaValidationError, match="Индексы"):
        UserDatabase(path).initialize()

    assert _version(path) == 11


def test_rejects_invalid_stored_role_before_migration(tmp_path: Path) -> None:
    path = tmp_path / "invalid-role.db"
    _create_v1(path)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE users SET role = ? WHERE telegram_user_id = ?",
            ("unexpected", 303),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(SchemaValidationError, match="недопустимую роль.*v1"):
        UserDatabase(path).initialize()

    assert _version(path) == 1
    assert _read_rows(path)[2][4] == "unexpected"


def test_rejects_users_table_with_zero_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "unversioned.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute(V1_SCHEMA)
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(SchemaValidationError, match="нулевой версии"):
        UserDatabase(path).initialize()

    assert _version(path) == 0


def test_migration_error_rolls_back_to_valid_v1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "rollback.db"
    expected = _create_v1(path)
    original_validation = migrations.validate_users_schema

    def fail_after_table_replacement(
        connection: sqlite3.Connection,
        version: int,
    ) -> None:
        original_validation(connection, version)
        if version == 2:
            raise RuntimeError("simulated migration failure")

    monkeypatch.setattr(
        migrations,
        "validate_users_schema",
        fail_after_table_replacement,
    )

    with pytest.raises(DatabaseMigrationError, match="прервана и отменена"):
        UserDatabase(path).initialize()

    assert _version(path) == 1
    assert _read_rows(path) == expected
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = '__users_v2_migration'"
        ).fetchone() is None
    finally:
        connection.close()


def test_v2_to_v3_error_rolls_back_only_v3_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "rollback-v3.db"
    _create_v2(path)

    def fail_seed(_connection: sqlite3.Connection) -> None:
        raise RuntimeError("simulated v3 failure")

    monkeypatch.setattr(migrations, "seed_warehouse_defaults", fail_seed)

    with pytest.raises(DatabaseMigrationError, match="прервана и отменена"):
        UserDatabase(path).initialize()

    assert _version(path) == 2
    connection = sqlite3.connect(path)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    finally:
        connection.close()
    assert not set(WAREHOUSE_TABLES) & tables
