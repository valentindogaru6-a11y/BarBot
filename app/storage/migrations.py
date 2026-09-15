"""Versioned, transactional SQLite schema migrations."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable

from app.storage.warehouse_schema import (
    WarehouseSchemaValidationError,
    create_warehouse_schema,
    seed_warehouse_defaults,
    validate_warehouse_schema,
    warehouse_schema_is_absent,
)
from app.storage.warehouse_operations_schema import (
    WarehouseOperationsSchemaError,
    create_warehouse_operations_schema,
    seed_v4_locations,
    validate_warehouse_operations_schema,
    warehouse_operations_schema_is_absent,
)
from app.storage.warehouse_closing_schema import (
    WarehouseClosingSchemaError,
    create_warehouse_closing_schema,
    validate_warehouse_closing_schema,
    warehouse_closing_schema_is_absent,
)
from app.storage.warehouse_reports_schema import (
    WarehouseReportsSchemaError,
    create_warehouse_reports_schema,
    validate_warehouse_reports_schema,
    warehouse_reports_schema_is_absent,
)
from app.storage.warehouse_v7_schema import (
    WarehouseV7SchemaError,
    migrate_warehouse_v6_to_v7,
    validate_warehouse_v7_schema,
    warehouse_v7_schema_is_absent,
)

CURRENT_SCHEMA_VERSION = 7

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

V1_ROLES = ("admin", "bartender", "user", "blocked")
V2_ROLES = ("admin", "bartender", "accountant", "user", "blocked")

_EXPECTED_COLUMNS = (
    ("telegram_user_id", "INTEGER", 0, None, 1),
    ("username", "TEXT", 0, None, 0),
    ("first_name", "TEXT", 0, None, 0),
    ("last_name", "TEXT", 0, None, 0),
    ("role", "TEXT", 1, "'user'", 0),
    ("is_active", "INTEGER", 1, "1", 0),
    ("first_seen_at", "TEXT", 1, None, 0),
    ("last_seen_at", "TEXT", 1, None, 0),
    ("created_by", "INTEGER", 0, None, 0),
    ("updated_at", "TEXT", 1, None, 0),
    ("has_started", "INTEGER", 1, "0", 0),
)

_USERS_V2_SQL = """
CREATE TABLE users (
    telegram_user_id INTEGER PRIMARY KEY,
    username TEXT NULL,
    first_name TEXT NULL,
    last_name TEXT NULL,
    role TEXT NOT NULL DEFAULT 'user'
        CHECK (role IN ('admin', 'bartender', 'accountant', 'user', 'blocked')),
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

_USERS_V2_REPLACEMENT_SQL = _USERS_V2_SQL.replace(
    "CREATE TABLE users", "CREATE TABLE __users_v2_migration", 1
)


class DatabaseMigrationError(RuntimeError):
    """Base error for schema initialization and migration failures."""


class UnsupportedSchemaVersionError(DatabaseMigrationError):
    """Raised when the database was created by a newer application."""


class SchemaValidationError(DatabaseMigrationError):
    """Raised when schema metadata and ``user_version`` disagree."""


def _table_sql(connection: sqlite3.Connection, table: str) -> str | None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return str(row[0]) if row is not None and row[0] is not None else None


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _column_signature(
    connection: sqlite3.Connection,
    table: str,
) -> tuple[tuple[str, str, int, str | None, int], ...]:
    rows = connection.execute(
        f"PRAGMA table_info({_quoted_identifier(table)})"
    ).fetchall()
    return tuple(
        (
            str(row[1]),
            str(row[2]).upper(),
            int(row[3]),
            str(row[4]) if row[4] is not None else None,
            int(row[5]),
        )
        for row in rows
    )


def _role_check_values(table_sql: str) -> tuple[str, ...] | None:
    compact = re.sub(r"\s+", "", table_sql.casefold())
    compact = compact.translate(str.maketrans("", "", '"`[]'))
    matches = re.findall(r"check\(rolein\(([^()]*)\)\)", compact)
    if len(matches) != 1:
        return None
    values = re.findall(r"'([^']+)'", matches[0])
    return tuple(values) if values else None


def _validate_boolean_checks(table_sql: str) -> bool:
    compact = re.sub(r"\s+", "", table_sql.casefold())
    compact = compact.translate(str.maketrans("", "", '"`[]'))
    return (
        "check(is_activein(0,1))" in compact
        and "check(has_startedin(0,1))" in compact
    )


def _validate_stored_roles(
    connection: sqlite3.Connection,
    expected_roles: tuple[str, ...],
    version: int,
) -> None:
    placeholders = ", ".join("?" for _ in expected_roles)
    invalid = connection.execute(
        f"SELECT 1 FROM users WHERE role NOT IN ({placeholders}) LIMIT 1",
        expected_roles,
    ).fetchone()
    if invalid is not None:
        raise SchemaValidationError(
            f"Таблица users содержит недопустимую роль для схемы v{version}"
        )


def _validate_no_dependent_objects(connection: sqlite3.Connection) -> None:
    dependent = connection.execute(
        """
        SELECT type, name
        FROM sqlite_master
        WHERE tbl_name = 'users'
          AND type IN ('index', 'trigger')
          AND sql IS NOT NULL
        LIMIT 1
        """
    ).fetchone()
    if dependent is not None:
        raise SchemaValidationError(
            "Таблица users содержит неподдерживаемый индекс или триггер"
        )

    tables = connection.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
        """
    ).fetchall()
    for row in tables:
        table = str(row[0])
        if table == "users":
            continue
        foreign_keys = connection.execute(
            f"PRAGMA foreign_key_list({_quoted_identifier(table)})"
        ).fetchall()
        if any(str(key[2]) == "users" for key in foreign_keys):
            raise SchemaValidationError(
                "Таблица users имеет неподдерживаемые внешние зависимости"
            )


def validate_users_schema(
    connection: sqlite3.Connection,
    version: int,
) -> None:
    """Validate the concrete invariants of the known users schemas."""

    expected_roles = (
        V1_ROLES
        if version == 1
        else V2_ROLES
        if version == 2
        else None
    )
    if expected_roles is None:
        raise UnsupportedSchemaVersionError(
            f"Неподдерживаемая версия схемы SQLite: {version}"
        )

    table_sql = _table_sql(connection, "users")
    if table_sql is None:
        raise SchemaValidationError(
            f"Версия схемы SQLite равна {version}, но таблица users отсутствует"
        )
    if _column_signature(connection, "users") != _EXPECTED_COLUMNS:
        raise SchemaValidationError(
            f"Таблица users не соответствует ожидаемой схеме v{version}"
        )
    if _role_check_values(table_sql) != expected_roles:
        raise SchemaValidationError(
            f"Ограничение ролей users не соответствует схеме v{version}"
        )
    if not _validate_boolean_checks(table_sql):
        raise SchemaValidationError(
            f"Ограничения статусов users не соответствуют схеме v{version}"
        )
    _validate_stored_roles(connection, expected_roles, version)
    _validate_no_dependent_objects(connection)


def _verify_users_preserved(connection: sqlite3.Connection) -> None:
    columns = ", ".join(USER_COLUMNS)
    old_count = int(
        connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    )
    new_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM __users_v2_migration"
        ).fetchone()[0]
    )
    if old_count != new_count:
        raise DatabaseMigrationError(
            "Миграция users не сохранила количество пользователей"
        )

    missing = connection.execute(
        f"""
        SELECT 1 FROM (
            SELECT {columns} FROM users
            EXCEPT
            SELECT {columns} FROM __users_v2_migration
        ) LIMIT 1
        """
    ).fetchone()
    changed = connection.execute(
        f"""
        SELECT 1 FROM (
            SELECT {columns} FROM __users_v2_migration
            EXCEPT
            SELECT {columns} FROM users
        ) LIMIT 1
        """
    ).fetchone()
    if missing is not None or changed is not None:
        raise DatabaseMigrationError(
            "Миграция users не сохранила значения пользовательских данных"
        )


def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
    _validate_schema(connection, 1)
    if _table_sql(connection, "__users_v2_migration") is not None:
        raise SchemaValidationError(
            "Обнаружена незавершённая служебная таблица миграции"
        )

    placeholders = ", ".join(USER_COLUMNS)
    connection.execute(_USERS_V2_REPLACEMENT_SQL)
    connection.execute(
        f"""
        INSERT INTO __users_v2_migration ({placeholders})
        SELECT {placeholders} FROM users
        """
    )
    _verify_users_preserved(connection)
    connection.execute("DROP TABLE users")
    connection.execute("ALTER TABLE __users_v2_migration RENAME TO users")
    connection.execute("PRAGMA user_version = 2")
    validate_users_schema(connection, 2)


def _validate_schema(connection: sqlite3.Connection, version: int) -> None:
    if version == 1:
        validate_users_schema(connection, 1)
        if (
            not warehouse_schema_is_absent(connection)
            or not warehouse_operations_schema_is_absent(connection)
            or not warehouse_closing_schema_is_absent(connection)
            or not warehouse_reports_schema_is_absent(connection)
            or not warehouse_v7_schema_is_absent(connection)
        ):
            raise SchemaValidationError(
                "Warehouse tables не должны существовать в schema v1"
            )
        return
    if version == 2:
        validate_users_schema(connection, 2)
        if (
            not warehouse_schema_is_absent(connection)
            or not warehouse_operations_schema_is_absent(connection)
            or not warehouse_closing_schema_is_absent(connection)
            or not warehouse_reports_schema_is_absent(connection)
            or not warehouse_v7_schema_is_absent(connection)
        ):
            raise SchemaValidationError(
                "Warehouse tables не должны существовать в schema v2"
            )
        return
    if version == 3:
        validate_users_schema(connection, 2)
        try:
            validate_warehouse_schema(connection)
        except WarehouseSchemaValidationError as exc:
            raise SchemaValidationError(str(exc)) from exc
        if not warehouse_operations_schema_is_absent(connection):
            raise SchemaValidationError(
                "Warehouse operations tables не должны существовать в schema v3"
            )
        if not warehouse_closing_schema_is_absent(connection):
            raise SchemaValidationError(
                "Warehouse closing tables не должны существовать в schema v3"
            )
        if not warehouse_reports_schema_is_absent(connection):
            raise SchemaValidationError(
                "Warehouse report tables не должны существовать в schema v3"
            )
        if not warehouse_v7_schema_is_absent(connection):
            raise SchemaValidationError(
                "Warehouse development drafts не должны существовать в schema v3"
            )
        return
    if version == 4:
        validate_users_schema(connection, 2)
        try:
            validate_warehouse_schema(connection)
            validate_warehouse_operations_schema(connection)
        except (
            WarehouseSchemaValidationError,
            WarehouseOperationsSchemaError,
        ) as exc:
            raise SchemaValidationError(str(exc)) from exc
        if not warehouse_closing_schema_is_absent(connection):
            raise SchemaValidationError(
                "Warehouse closing tables не должны существовать в schema v4"
            )
        if not warehouse_reports_schema_is_absent(connection):
            raise SchemaValidationError(
                "Warehouse report tables не должны существовать в schema v4"
            )
        if not warehouse_v7_schema_is_absent(connection):
            raise SchemaValidationError(
                "Warehouse development drafts не должны существовать в schema v4"
            )
        return
    if version == 5:
        validate_users_schema(connection, 2)
        try:
            validate_warehouse_schema(connection)
            validate_warehouse_operations_schema(connection)
            validate_warehouse_closing_schema(connection)
        except (
            WarehouseSchemaValidationError,
            WarehouseOperationsSchemaError,
            WarehouseClosingSchemaError,
        ) as exc:
            raise SchemaValidationError(str(exc)) from exc
        if not warehouse_reports_schema_is_absent(connection):
            raise SchemaValidationError(
                "Warehouse report tables не должны существовать в schema v5"
            )
        if not warehouse_v7_schema_is_absent(connection):
            raise SchemaValidationError(
                "Warehouse development drafts не должны существовать в schema v5"
            )
        return
    if version == 6:
        validate_users_schema(connection, 2)
        try:
            validate_warehouse_schema(connection)
            validate_warehouse_operations_schema(connection)
            validate_warehouse_closing_schema(connection)
            validate_warehouse_reports_schema(connection)
        except (
            WarehouseSchemaValidationError,
            WarehouseOperationsSchemaError,
            WarehouseClosingSchemaError,
            WarehouseReportsSchemaError,
        ) as exc:
            raise SchemaValidationError(str(exc)) from exc
        if not warehouse_v7_schema_is_absent(connection):
            raise SchemaValidationError(
                "Warehouse development drafts не должны существовать в schema v6"
            )
        return
    if version == 7:
        validate_users_schema(connection, 2)
        try:
            validate_warehouse_closing_schema(connection)
            validate_warehouse_v7_schema(connection)
        except (WarehouseClosingSchemaError, WarehouseV7SchemaError) as exc:
            raise SchemaValidationError(str(exc)) from exc
        return
    raise UnsupportedSchemaVersionError(
        f"Неподдерживаемая версия схемы SQLite: {version}"
    )


def _migrate_v2_to_v3(connection: sqlite3.Connection) -> None:
    _validate_schema(connection, 2)
    create_warehouse_schema(connection)
    seed_warehouse_defaults(connection)
    connection.execute("PRAGMA user_version = 3")
    _validate_schema(connection, 3)


def _migrate_v3_to_v4(connection: sqlite3.Connection) -> None:
    _validate_schema(connection, 3)
    seed_v4_locations(connection)
    create_warehouse_operations_schema(connection)
    connection.execute("PRAGMA user_version = 4")
    _validate_schema(connection, 4)


def _migrate_v4_to_v5(connection: sqlite3.Connection) -> None:
    _validate_schema(connection, 4)
    create_warehouse_closing_schema(connection)
    connection.execute("PRAGMA user_version = 5")
    _validate_schema(connection, 5)


def _migrate_v5_to_v6(connection: sqlite3.Connection) -> None:
    _validate_schema(connection, 5)
    create_warehouse_reports_schema(connection)
    connection.execute("PRAGMA user_version = 6")
    _validate_schema(connection, 6)


def _migrate_v6_to_v7(connection: sqlite3.Connection) -> None:
    _validate_schema(connection, 6)
    migrate_warehouse_v6_to_v7(connection)
    connection.execute("PRAGMA user_version = 7")
    _validate_schema(connection, 7)


def _create_fresh_v6(connection: sqlite3.Connection) -> None:
    if _table_sql(connection, "users") is not None:
        raise SchemaValidationError(
            "Таблица users существует при нулевой версии схемы"
        )
    if _table_sql(connection, "__users_v2_migration") is not None:
        raise SchemaValidationError(
            "Обнаружена незавершённая служебная таблица миграции"
        )
    if (
        not warehouse_schema_is_absent(connection)
        or not warehouse_operations_schema_is_absent(connection)
        or not warehouse_closing_schema_is_absent(connection)
        or not warehouse_reports_schema_is_absent(connection)
        or not warehouse_v7_schema_is_absent(connection)
    ):
        raise SchemaValidationError(
            "Warehouse tables существуют при нулевой версии схемы"
        )
    connection.execute(_USERS_V2_SQL)
    create_warehouse_schema(connection)
    seed_warehouse_defaults(connection)
    seed_v4_locations(connection)
    create_warehouse_operations_schema(connection)
    create_warehouse_closing_schema(connection)
    create_warehouse_reports_schema(connection)
    connection.execute("PRAGMA user_version = 6")
    _validate_schema(connection, 6)


def _create_fresh_v7(connection: sqlite3.Connection) -> None:
    _create_fresh_v6(connection)
    _migrate_v6_to_v7(connection)


Migration = Callable[[sqlite3.Connection], None]
MIGRATIONS: dict[int, Migration] = {
    1: _migrate_v1_to_v2,
    2: _migrate_v2_to_v3,
    3: _migrate_v3_to_v4,
    4: _migrate_v4_to_v5,
    5: _migrate_v5_to_v6,
    6: _migrate_v6_to_v7,
}


def _run_atomic_step(
    connection: sqlite3.Connection,
    operation: Migration,
    *,
    disable_foreign_keys: bool = False,
) -> None:
    previous_foreign_keys = int(
        connection.execute("PRAGMA foreign_keys").fetchone()[0]
    )
    if disable_foreign_keys:
        connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        operation(connection)
        connection.commit()
    except Exception as exc:
        if connection.in_transaction:
            connection.rollback()
        if isinstance(exc, DatabaseMigrationError):
            raise
        if isinstance(exc, sqlite3.Error):
            raise DatabaseMigrationError(
                "Не удалось применить миграцию SQLite"
            ) from exc
        raise DatabaseMigrationError(
            "Миграция SQLite прервана и отменена"
        ) from exc
    finally:
        if disable_foreign_keys and previous_foreign_keys:
            connection.execute("PRAGMA foreign_keys = ON")


def run_migrations(connection: sqlite3.Connection) -> None:
    """Initialize or upgrade one database under an explicit transaction."""

    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version > CURRENT_SCHEMA_VERSION:
        raise UnsupportedSchemaVersionError(
            "Версия SQLite новее поддерживаемой: "
            f"{version} > {CURRENT_SCHEMA_VERSION}"
        )
    if connection.in_transaction:
        raise DatabaseMigrationError(
            "Migration runner требует соединение без активной транзакции"
        )

    if version == 0:
        _run_atomic_step(
            connection,
            _create_fresh_v7,
            disable_foreign_keys=True,
        )
        return
    if version == CURRENT_SCHEMA_VERSION:
        _run_atomic_step(
            connection,
            lambda current: _validate_schema(current, CURRENT_SCHEMA_VERSION),
        )
        return

    while version < CURRENT_SCHEMA_VERSION:
        migration = MIGRATIONS.get(version)
        if migration is None:
            raise UnsupportedSchemaVersionError(
                f"Нет миграции для схемы SQLite v{version}"
            )
        expected_version = version + 1

        def apply_step(current: sqlite3.Connection) -> None:
            migration(current)
            actual_version = int(
                current.execute("PRAGMA user_version").fetchone()[0]
            )
            if actual_version != expected_version:
                raise DatabaseMigrationError(
                    "Миграция SQLite должна повышать версию ровно на один шаг"
                )

        _run_atomic_step(
            connection,
            apply_step,
            disable_foreign_keys=version == 6,
        )
        version = expected_version
    _run_atomic_step(
        connection,
        lambda current: _validate_schema(current, CURRENT_SCHEMA_VERSION),
    )
