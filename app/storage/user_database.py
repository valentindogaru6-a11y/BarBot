"""SQLite persistence for users and local roles without an ORM."""

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from app.models.user import DATABASE_ROLES, Role, UserRecord

SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5_000


class UserDatabaseError(RuntimeError):
    """Raised when the user database cannot complete an operation."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class UserDatabase:
    """Small connection-per-operation SQLite repository."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.path, timeout=5.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            yield connection
            connection.commit()
        except sqlite3.Error as exc:
            if connection is not None:
                connection.rollback()
            raise UserDatabaseError("Не удалось выполнить операцию с базой пользователей") from exc
        finally:
            if connection is not None:
                connection.close()

    def initialize(self) -> None:
        """Create or safely reuse the current schema."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
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
            )
            connection.execute("PRAGMA user_version = 1")

    @staticmethod
    def _record(row: sqlite3.Row | None) -> UserRecord | None:
        if row is None:
            return None
        return UserRecord.model_validate(dict(row))

    def sync_user(
        self,
        telegram_user_id: int,
        *,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
    ) -> UserRecord:
        """Create/update Telegram profile data without changing the role."""

        now = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO users (
                    telegram_user_id, username, first_name, last_name,
                    role, is_active, first_seen_at, last_seen_at,
                    created_by, updated_at, has_started
                ) VALUES (?, ?, ?, ?, 'user', 1, ?, ?, NULL, ?, 1)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    last_name = excluded.last_name,
                    last_seen_at = excluded.last_seen_at,
                    updated_at = excluded.updated_at,
                    has_started = 1
                """,
                (
                    telegram_user_id,
                    username,
                    first_name,
                    last_name,
                    now,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM users WHERE telegram_user_id = ?",
                (telegram_user_id,),
            ).fetchone()
        record = self._record(row)
        if record is None:
            raise UserDatabaseError("Пользователь не был сохранён")
        return record

    def ensure_user(
        self,
        telegram_user_id: int,
        *,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        created_by: int | None = None,
    ) -> UserRecord:
        """Persist a selected user who may not have started the bot yet."""

        now = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO users (
                    telegram_user_id, username, first_name, last_name,
                    role, is_active, first_seen_at, last_seen_at,
                    created_by, updated_at, has_started
                ) VALUES (?, ?, ?, ?, 'user', 1, ?, ?, ?, ?, 0)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    username = COALESCE(excluded.username, users.username),
                    first_name = COALESCE(excluded.first_name, users.first_name),
                    last_name = COALESCE(excluded.last_name, users.last_name),
                    updated_at = excluded.updated_at
                """,
                (
                    telegram_user_id,
                    username,
                    first_name,
                    last_name,
                    now,
                    now,
                    created_by,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM users WHERE telegram_user_id = ?",
                (telegram_user_id,),
            ).fetchone()
        record = self._record(row)
        if record is None:
            raise UserDatabaseError("Пользователь не был сохранён")
        return record

    def get_user(self, telegram_user_id: int) -> UserRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE telegram_user_id = ?",
                (telegram_user_id,),
            ).fetchone()
        return self._record(row)

    def list_users(
        self,
        *,
        role: Role | None = None,
        limit: int = 8,
        offset: int = 0,
    ) -> list[UserRecord]:
        if role is not None and role not in DATABASE_ROLES:
            return []
        with self._connect() as connection:
            if role is None:
                rows = connection.execute(
                    "SELECT * FROM users ORDER BY last_seen_at DESC, telegram_user_id LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM users WHERE role = ? ORDER BY last_seen_at DESC, telegram_user_id LIMIT ? OFFSET ?",
                    (role.value, limit, offset),
                ).fetchall()
        return [record for row in rows if (record := self._record(row))]

    def count_users(self, role: Role | None = None) -> int:
        with self._connect() as connection:
            if role is None:
                row = connection.execute("SELECT COUNT(*) AS count FROM users").fetchone()
            else:
                row = connection.execute(
                    "SELECT COUNT(*) AS count FROM users WHERE role = ?",
                    (role.value,),
                ).fetchone()
        return int(row["count"] if row else 0)

    def search_users(self, query: str, limit: int = 20) -> list[UserRecord]:
        normalized = query.strip().lstrip("@").casefold()
        if not normalized:
            return []
        if normalized.isdecimal():
            user = self.get_user(int(normalized))
            return [user] if user else []
        users = self.list_users(limit=100_000)
        matches = []
        for user in users:
            values = (
                user.username or "",
                user.first_name or "",
                user.last_name or "",
                user.display_name,
            )
            if any(normalized in value.casefold() for value in values):
                matches.append(user)
        return matches[:limit]

    def set_role(
        self,
        telegram_user_id: int,
        role: Role,
        *,
        actor_user_id: int,
    ) -> tuple[Role, UserRecord]:
        if role not in DATABASE_ROLES:
            raise UserDatabaseError("Недопустимая сохраняемая роль")
        current = self.get_user(telegram_user_id)
        if current is None:
            current = self.ensure_user(
                telegram_user_id,
                created_by=actor_user_id,
            )
        now = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE users
                SET role = ?, is_active = ?, created_by = ?, updated_at = ?
                WHERE telegram_user_id = ?
                """,
                (
                    role.value,
                    int(role is not Role.BLOCKED),
                    actor_user_id,
                    now,
                    telegram_user_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM users WHERE telegram_user_id = ?",
                (telegram_user_id,),
            ).fetchone()
        updated = self._record(row)
        if updated is None:
            raise UserDatabaseError("Роль пользователя не была сохранена")
        return current.role, updated

    def settings(self) -> dict[str, int | str]:
        """Expose non-sensitive SQLite settings for diagnostics/tests."""

        with self._connect() as connection:
            journal = connection.execute("PRAGMA journal_mode").fetchone()[0]
            timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
            foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        return {
            "journal_mode": str(journal),
            "busy_timeout": int(timeout),
            "foreign_keys": int(foreign_keys),
            "user_version": int(version),
        }
