"""Regression tests for logical read-only release inspection."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from app.release_preflight import Status, _check_database
from app.storage import migrations, release_inspection
from app.storage.release_inspection import inspect_sqlite_database
from app.storage.user_database import UserDatabase
from app.storage.warehouse_closing_schema import create_warehouse_closing_schema
from app.storage.warehouse_operations_schema import (
    create_warehouse_operations_schema,
    seed_v4_locations,
)
from app.storage.warehouse_schema import create_warehouse_schema, seed_warehouse_defaults


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _v5(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(migrations._USERS_V2_SQL)
        create_warehouse_schema(connection)
        seed_warehouse_defaults(connection)
        seed_v4_locations(connection)
        create_warehouse_operations_schema(connection)
        create_warehouse_closing_schema(connection)
        connection.execute("PRAGMA user_version=5")


def _v10(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        migrations._create_fresh_v10(connection)


@pytest.mark.parametrize(("builder", "version"), [(_v5, 5), (_v10, 10)])
def test_supported_old_schema_is_strict_by_default_and_warns_when_allowed(
    tmp_path: Path, builder, version: int,
) -> None:
    database = tmp_path / f"v{version}.db"
    builder(database)

    strict = _check_database(database, allow_supported_upgrade=False)
    controlled = _check_database(database, allow_supported_upgrade=True)

    assert strict[0].status is Status.FAIL
    assert controlled[0].status is Status.WARN
    assert controlled[0].detail == f"supported migration required: v{version} → v11"


def test_current_and_future_schema_decisions(tmp_path: Path) -> None:
    current = tmp_path / "v11.db"
    UserDatabase(current).initialize()
    assert _check_database(current, allow_supported_upgrade=True)[0].status is Status.PASS

    future = tmp_path / "v12.db"
    UserDatabase(future).initialize()
    with sqlite3.connect(future) as connection:
        connection.execute("PRAGMA user_version=12")
    assert _check_database(future, allow_supported_upgrade=True)[0].status is Status.FAIL


def test_broken_migration_chain_and_invalid_schema_are_rejected(
    tmp_path: Path, monkeypatch,
) -> None:
    database = tmp_path / "v10.db"
    _v10(database)
    broken_chain = inspect_sqlite_database(database, migration_versions=set())
    assert broken_chain.schema_compatible
    assert not broken_chain.migration_chain_complete
    monkeypatch.setattr(
        release_inspection,
        "MIGRATIONS",
        {key: value for key, value in migrations.MIGRATIONS.items() if key != 10},
    )
    assert _check_database(database, allow_supported_upgrade=True)[0].status is Status.FAIL
    monkeypatch.undo()

    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE warehouse_delivery_jobs")
    assert _check_database(database, allow_supported_upgrade=True)[0].status is Status.FAIL


def test_nonempty_unversioned_database_is_rejected(tmp_path: Path) -> None:
    database = tmp_path / "v0.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE unexpected (id INTEGER PRIMARY KEY)")

    assert _check_database(database, allow_supported_upgrade=True)[0].status is Status.FAIL


def test_integrity_and_foreign_key_results_are_blocking(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "v11.db"
    UserDatabase(database).initialize()
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "INSERT INTO warehouse_product_aliases "
            "(product_id, alias, normalized_alias, created_at_utc) "
            "VALUES (999999, 'bad', 'bad', '2026-01-01T00:00:00Z')"
        )
    checks = _check_database(database, allow_supported_upgrade=True)
    assert checks[2].status is Status.FAIL

    real = inspect_sqlite_database(database)
    monkeypatch.setattr(
        "app.release_preflight.inspect_sqlite_database",
        lambda _path: type(real)(
            version=real.version,
            integrity_ok=False,
            foreign_key_violations=0,
            schema_compatible=real.schema_compatible,
            migration_chain_complete=real.migration_chain_complete,
        ),
    )
    assert _check_database(database, allow_supported_upgrade=True)[1].status is Status.FAIL


def test_inspection_does_not_initialize_migrate_or_change_logical_database(
    tmp_path: Path, monkeypatch,
) -> None:
    database = tmp_path / "v11.db"
    UserDatabase(database).initialize()
    with sqlite3.connect(database) as connection:
        before_version = connection.execute("PRAGMA user_version").fetchone()[0]
        before_schema = connection.execute(
            "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
        ).fetchall()
        before_rows = connection.execute("SELECT COUNT(*) FROM warehouse_locations").fetchone()[0]
    before_digest = _digest(database)

    monkeypatch.setattr(UserDatabase, "initialize", lambda self: pytest.fail("initializer called"))
    monkeypatch.setattr(migrations, "run_migrations", lambda _connection: pytest.fail("migration called"))
    result = inspect_sqlite_database(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == before_version
        assert connection.execute(
            "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
        ).fetchall() == before_schema
        assert connection.execute("SELECT COUNT(*) FROM warehouse_locations").fetchone()[0] == before_rows
    assert result.version == 11
    assert _digest(database) == before_digest


def test_inspection_does_not_request_journal_change_or_checkpoint(tmp_path: Path) -> None:
    database = tmp_path / "v11.db"
    UserDatabase(database).initialize()
    statements: list[str] = []

    def traced_connect(*args, **kwargs):
        connection = sqlite3.connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    inspect_sqlite_database(database, connect=traced_connect)
    normalized = "\n".join(statements).casefold()
    assert "journal_mode" not in normalized
    assert "wal_checkpoint" not in normalized
    assert not any(
        statement.lstrip().casefold().startswith(("insert", "update", "delete", "create", "drop", "alter"))
        for statement in statements
    )


def test_existing_wal_sidecars_are_accepted_and_not_deleted(tmp_path: Path) -> None:
    database = tmp_path / "live.db"
    UserDatabase(database).initialize()
    writer = sqlite3.connect(database)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute(
            "INSERT INTO users (telegram_user_id, role, first_seen_at, last_seen_at, updated_at) "
            "VALUES (1, 'user', 'now', 'now', 'now')"
        )
        writer.commit()
        wal = Path(f"{database}-wal")
        shm = Path(f"{database}-shm")
        assert wal.exists() and shm.exists()

        result = inspect_sqlite_database(database)

        assert result.version == 11
        assert wal.exists() and shm.exists()
    finally:
        writer.close()


def test_gitignore_covers_sqlite_runtime_sidecars() -> None:
    rules = (Path(__file__).parents[1] / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert {"*.db-wal", "*.db-shm", "*.sqlite-wal", "*.sqlite-shm", "*.sqlite3-wal", "*.sqlite3-shm"} <= set(rules)
