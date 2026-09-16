"""Schema-v11 migration, validation, rollback, and compatibility tests."""

import sqlite3
from pathlib import Path

import pytest

from app.storage import migrations
from app.storage.migrations import SchemaValidationError, UnsupportedSchemaVersionError
from app.storage.user_database import UserDatabase
from app.storage.warehouse_correction_schema import validate_warehouse_correction_schema


def _version(path: Path) -> int:
    with sqlite3.connect(path) as connection:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])


def _v10(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("BEGIN")
        migrations._create_fresh_v10(connection)
        connection.commit()


def test_v11_fresh_repeat_and_full_chain(tmp_path: Path) -> None:
    fresh = tmp_path / "fresh.db"
    UserDatabase(fresh).initialize(); UserDatabase(fresh).initialize()
    assert _version(fresh) == 11
    with sqlite3.connect(fresh) as connection:
        validate_warehouse_correction_schema(connection)

    chain = tmp_path / "chain.db"
    with sqlite3.connect(chain) as connection:
        connection.execute(migrations._USERS_V2_SQL)
        connection.execute("PRAGMA user_version=2")
        connection.commit()
    UserDatabase(chain).initialize()
    assert _version(chain) == 11


def test_v10_to_v11_preserves_existing_ready_report(tmp_path: Path) -> None:
    path = tmp_path / "upgrade.db"; _v10(path)
    with sqlite3.connect(path) as connection:
        source = connection.execute("SELECT id FROM warehouse_locations WHERE stable_id='bar'").fetchone()[0]
        shift = connection.execute("INSERT INTO warehouse_shifts(source_location_id,business_date,timezone_snapshot,cutoff_snapshot,status,started_at_utc,scheduled_close_at_utc,closed_at_utc,closed_by,revision,created_at_utc,updated_at_utc) VALUES(?,'2026-09-10','Europe/Kyiv','23:00','closed','2026-09-09T21:00:00Z','2026-09-10T20:00:00Z','2026-09-10T20:01:00Z',1,2,'2026-09-09T21:00:00Z','2026-09-10T20:01:00Z')",(source,)).lastrowid
        report = connection.execute("INSERT INTO warehouse_reports(shift_id,version,report_kind,status,relative_file_path,file_sha256,file_size_bytes,created_at_utc,created_by,ready_at_utc,failure_code,supersedes_report_id,correction_reason) VALUES(?,1,'final','ready','reports/v001.xlsx',?,1,'2026-09-10T20:00:00Z',1,'2026-09-10T20:01:00Z',NULL,NULL,NULL)",(shift,'a'*64)).lastrowid
        connection.commit()
    UserDatabase(path).initialize()
    with sqlite3.connect(path) as connection:
        row = connection.execute("SELECT status,file_sha256,correction_session_id FROM warehouse_reports WHERE id=?",(report,)).fetchone()
        assert row == ('ready','a'*64,None)
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 11


def test_v11_schema_constraints_and_validation(tmp_path: Path) -> None:
    path = tmp_path / "constraints.db"; UserDatabase(path).initialize()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO warehouse_correction_sessions(shift_id,base_report_id,status,reason_text,opened_by,opened_at_utc,revision,created_at_utc,updated_at_utc) VALUES(999,999,'open','',1,'bad',0,'bad','bad')")
        connection.execute("DROP INDEX ux_warehouse_correction_sessions_active")
        connection.commit()
    with pytest.raises(SchemaValidationError, match="Invalid correction indexes"):
        UserDatabase(path).initialize()


def test_v11_migration_failure_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "rollback.db"; _v10(path)
    def fail(connection):
        connection.execute("CREATE TABLE partial_v11(id INTEGER PRIMARY KEY)")
        raise RuntimeError("simulated v11 failure")
    monkeypatch.setattr(migrations,"create_warehouse_correction_schema",fail)
    with pytest.raises(migrations.DatabaseMigrationError):UserDatabase(path).initialize()
    assert _version(path)==10
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE name='partial_v11'").fetchone() is None


def test_v11_rejects_future_schema_version(tmp_path: Path) -> None:
    path=tmp_path/'future.db'
    with sqlite3.connect(path) as connection:
        connection.execute('PRAGMA user_version=12')
    with pytest.raises(UnsupportedSchemaVersionError):UserDatabase(path).initialize()
