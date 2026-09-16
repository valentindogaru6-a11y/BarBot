"""Logical read-only SQLite inspection for controlled release checks.

This path intentionally does not use :class:`UserDatabase`: it never
initializes or migrates a schema, changes journal mode, checkpoints WAL, or
changes filesystem permissions.  SQLite may still create or update runtime
``-wal``/``-shm`` sidecars while opening a live WAL database in ``mode=ro``;
those files are not part of the logical database contents.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from app.storage.migrations import (
    CURRENT_SCHEMA_VERSION,
    DatabaseMigrationError,
    MIGRATIONS,
    _validate_schema,
)


class SQLiteInspectionError(RuntimeError):
    """Raised when the configured database cannot be inspected read-only."""


@dataclass(frozen=True, slots=True)
class SQLiteInspection:
    version: int
    integrity_ok: bool
    foreign_key_violations: int
    schema_compatible: bool
    migration_chain_complete: bool


Connect = Callable[..., sqlite3.Connection]


@contextmanager
def open_release_inspection(
    path: str | Path,
    *,
    connect: Connect | None = None,
) -> Iterator[sqlite3.Connection]:
    """Open a short-lived logical read-only connection to an existing DB."""

    database = Path(path)
    connector = connect or sqlite3.connect
    uri = f"file:{quote(str(database), safe='/')}?mode=ro"
    connection: sqlite3.Connection | None = None
    try:
        connection = connector(
            uri,
            uri=True,
            timeout=5,
            isolation_level=None,
        )
        connection.execute("PRAGMA query_only=ON")
        query_only = int(connection.execute("PRAGMA query_only").fetchone()[0])
        if query_only != 1:
            raise SQLiteInspectionError("SQLite query_only could not be enabled")
        yield connection
    except SQLiteInspectionError:
        raise
    except sqlite3.Error as exc:
        raise SQLiteInspectionError("read-only SQLite inspection failed") from exc
    finally:
        if connection is not None:
            connection.close()


def inspect_sqlite_database(
    path: str | Path,
    *,
    connect: Connect | None = None,
    migration_versions: Collection[int] | None = None,
) -> SQLiteInspection:
    """Inspect logical state without writes, migrations, or recovery."""

    available_migrations = (
        set(MIGRATIONS) if migration_versions is None else set(migration_versions)
    )
    try:
        with open_release_inspection(path, connect=connect) as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            integrity_rows = tuple(
                str(row[0]) for row in connection.execute("PRAGMA integrity_check")
            )
            foreign_key_violations = len(
                connection.execute("PRAGMA foreign_key_check").fetchall()
            )
            schema_compatible = False
            if 1 <= version <= CURRENT_SCHEMA_VERSION:
                try:
                    _validate_schema(connection, version)
                except DatabaseMigrationError:
                    schema_compatible = False
                else:
                    schema_compatible = True
    except (OSError, SQLiteInspectionError) as exc:
        if isinstance(exc, SQLiteInspectionError):
            raise
        raise SQLiteInspectionError("read-only SQLite inspection failed") from exc

    migration_chain_complete = (
        version == CURRENT_SCHEMA_VERSION
        or (
            1 <= version < CURRENT_SCHEMA_VERSION
            and all(
                step in available_migrations
                for step in range(version, CURRENT_SCHEMA_VERSION)
            )
        )
    )
    return SQLiteInspection(
        version=version,
        integrity_ok=integrity_rows == ("ok",),
        foreign_key_violations=foreign_key_violations,
        schema_compatible=schema_compatible,
        migration_chain_complete=migration_chain_complete,
    )


__all__ = [
    "SQLiteInspection",
    "SQLiteInspectionError",
    "inspect_sqlite_database",
    "open_release_inspection",
]
