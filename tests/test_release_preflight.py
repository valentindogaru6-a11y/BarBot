"""Read-only Phase 10A release-preflight tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.release_preflight import Status, run_preflight
from app.storage.user_database import UserDatabase


SECRET = "123456789:do-not-print-this-secret-value"


def _setup(tmp_path: Path) -> tuple[dict[str, str], dict[str, Path]]:
    root = tmp_path / "project"
    data = root / "data"
    reports = root / "reports" / "warehouse"
    logs = root / "logs"
    for directory in (data, reports, logs):
        directory.mkdir(parents=True, exist_ok=True)
    database = data / "test.db"
    UserDatabase(database).initialize()
    catalog = data / "catalog.json"
    catalog.write_text("{}\n", encoding="utf-8")
    audit = logs / "admin_audit.jsonl"
    environment = {
        "BOT_TOKEN": SECRET,
        "SUPER_ADMIN_IDS": "[100]",
        "CATALOG_PATH": str(catalog),
        "DATABASE_PATH": str(database),
        "WAREHOUSE_REPORTS_PATH": str(reports),
        "WAREHOUSE_ENABLED": "true",
    }
    return environment, {
        "root": root, "database": database, "catalog": catalog,
        "reports": reports, "audit": audit,
    }


def _run(tmp_path: Path, **changes):
    environment, paths = _setup(tmp_path)
    environment.update(changes.pop("environment", {}))
    result = run_preflight(
        environment,
        database_path=changes.pop("database_path", paths["database"]),
        report_path=changes.pop("report_path", paths["reports"]),
        audit_path=changes.pop("audit_path", paths["audit"]),
        catalog_path=changes.pop("catalog_path", paths["catalog"]),
        polling_owner_count=changes.pop("polling_owner_count", 1),
        allow_temporary_paths=changes.pop("allow_temporary_paths", True),
        project_root=paths["root"],
        **changes,
    )
    return result, paths


def _check(result, name: str):
    return next(check for check in result.checks if check.name == name)


def test_valid_temporary_configuration_and_database_pass(tmp_path: Path) -> None:
    result, _ = _run(tmp_path)

    assert result.exit_code == 0
    assert _check(result, "schema.target").detail == "v13; expected v13"
    assert _check(result, "database.schema").status is Status.PASS
    assert _check(result, "seed.validation").detail == (
        "products=414 active=414 owner_required=2"
    )


def test_config_check_only_is_read_only_and_deterministic(tmp_path: Path) -> None:
    environment, paths = _setup(tmp_path)
    before = paths["database"].read_bytes()
    arguments = dict(
        database_path=paths["database"], report_path=paths["reports"],
        audit_path=paths["audit"], catalog_path=paths["catalog"],
        polling_owner_count=1, allow_temporary_paths=True,
        project_root=paths["root"], config_check_only=True,
    )

    first = run_preflight(environment, **arguments)
    second = run_preflight(environment, **arguments)

    assert first.exit_code == 0
    assert first.render() == second.render()
    assert _check(first, "database.read_only").status is Status.SKIP
    assert paths["database"].read_bytes() == before
    assert not paths["audit"].exists()


@pytest.mark.parametrize("missing", ["BOT_TOKEN", "SUPER_ADMIN_IDS"])
def test_missing_required_key_fails_without_secret_output(
    tmp_path: Path, missing: str,
) -> None:
    environment, paths = _setup(tmp_path)
    environment.pop(missing)
    result = run_preflight(
        environment, database_path=paths["database"], report_path=paths["reports"],
        audit_path=paths["audit"], catalog_path=paths["catalog"],
        polling_owner_count=1, allow_temporary_paths=True,
        project_root=paths["root"], config_check_only=True,
    )

    assert result.exit_code != 0
    assert SECRET not in result.render()
    assert "do-not-print" not in result.render()


@pytest.mark.parametrize(
    ("timezone", "cutoff"),
    [("Invalid/Zone", "23:00"), ("Europe/Kyiv", "25:99"),
     ("UTC", "23:00"), ("Europe/Kyiv", "22:00")],
)
def test_invalid_timezone_or_cutoff_fails(
    tmp_path: Path, timezone: str, cutoff: str,
) -> None:
    result, _ = _run(
        tmp_path, timezone=timezone, cutoff=cutoff, config_check_only=True
    )
    assert result.exit_code != 0
    assert _check(result, "warehouse.timezone_cutoff").status is Status.FAIL


def test_unsafe_overlapping_and_temporary_production_paths_fail(tmp_path: Path) -> None:
    environment, paths = _setup(tmp_path)
    overlap = run_preflight(
        environment, database_path=paths["reports"] / "same.db",
        report_path=paths["reports"], audit_path=paths["reports"] / "audit.jsonl",
        catalog_path=paths["catalog"], polling_owner_count=1,
        allow_temporary_paths=True, project_root=paths["root"],
        config_check_only=True,
    )
    production_temp = run_preflight(
        environment, database_path=paths["database"], report_path=paths["reports"],
        audit_path=paths["audit"], catalog_path=paths["catalog"],
        polling_owner_count=1, allow_temporary_paths=False,
        project_root=paths["root"], config_check_only=True,
    )

    assert _check(overlap, "path.separation").status is Status.FAIL
    assert _check(production_temp, "path.production_reports").status is Status.FAIL


def test_runtime_report_and_audit_paths_must_match_application_policy(
    tmp_path: Path,
) -> None:
    environment, paths = _setup(tmp_path)
    outside = tmp_path / "outside-reports"
    outside.mkdir()
    wrong_audit = paths["root"] / "logs" / "other.jsonl"

    result = run_preflight(
        environment, database_path=paths["database"], report_path=outside,
        audit_path=wrong_audit, catalog_path=paths["catalog"],
        polling_owner_count=1, allow_temporary_paths=True,
        project_root=paths["root"], config_check_only=True,
    )

    assert _check(result, "path.production_reports").status is Status.FAIL
    assert _check(result, "path.audit_runtime").status is Status.FAIL


def test_deprecated_admin_ids_fallback_is_explicit_warning(tmp_path: Path) -> None:
    environment, paths = _setup(tmp_path)
    environment.pop("SUPER_ADMIN_IDS")
    environment["ADMIN_IDS"] = "100"

    result = run_preflight(
        environment, database_path=paths["database"], report_path=paths["reports"],
        audit_path=paths["audit"], catalog_path=paths["catalog"],
        polling_owner_count=1, allow_temporary_paths=True,
        project_root=paths["root"], config_check_only=True,
    )

    check = _check(result, "config.SUPER_ADMIN_IDS")
    assert check.status is Status.WARN
    assert check.detail == "deprecated ADMIN_IDS fallback configured"
    assert result.exit_code == 0


def test_symlink_to_broad_path_and_missing_parent_fail(tmp_path: Path) -> None:
    environment, paths = _setup(tmp_path)
    broad = tmp_path / "project-link"
    broad.symlink_to(paths["root"], target_is_directory=True)
    linked = run_preflight(
        environment, database_path=paths["database"], report_path=broad,
        audit_path=paths["audit"], catalog_path=paths["catalog"],
        polling_owner_count=1, allow_temporary_paths=True,
        project_root=paths["root"], config_check_only=True,
    )
    missing = run_preflight(
        environment, database_path=paths["database"], report_path=paths["reports"],
        audit_path=tmp_path / "missing" / "nested" / "audit.jsonl",
        catalog_path=paths["catalog"], polling_owner_count=1,
        allow_temporary_paths=True, project_root=paths["root"],
        config_check_only=True,
    )

    assert _check(linked, "path.reports").status is Status.FAIL
    assert _check(missing, "path.audit").status is Status.FAIL


def test_multiple_polling_owners_and_future_database_fail(tmp_path: Path) -> None:
    result, _ = _run(tmp_path, polling_owner_count=2, config_check_only=True)
    assert _check(result, "polling.owner").status is Status.FAIL

    environment, paths = _setup(tmp_path / "future")
    import sqlite3
    with sqlite3.connect(paths["database"]) as connection:
            connection.execute("PRAGMA user_version=14")
    future = run_preflight(
        environment, database_path=paths["database"], report_path=paths["reports"],
        audit_path=paths["audit"], catalog_path=paths["catalog"],
        polling_owner_count=1, allow_temporary_paths=True,
        project_root=paths["root"],
    )
    assert _check(future, "database.schema").status is Status.FAIL


def test_preflight_never_creates_missing_database(tmp_path: Path) -> None:
    environment, paths = _setup(tmp_path)
    missing = paths["database"].with_name("missing.db")
    result = run_preflight(
        environment, database_path=missing, report_path=paths["reports"],
        audit_path=paths["audit"], catalog_path=paths["catalog"],
        polling_owner_count=1, allow_temporary_paths=True,
        project_root=paths["root"],
    )
    assert result.exit_code != 0
    assert not missing.exists()


def test_missing_catalog_is_a_blocking_failure(tmp_path: Path) -> None:
    environment, paths = _setup(tmp_path)
    paths["catalog"].unlink()

    result = run_preflight(
        environment, database_path=paths["database"], report_path=paths["reports"],
        audit_path=paths["audit"], catalog_path=paths["catalog"],
        polling_owner_count=1, allow_temporary_paths=True,
        project_root=paths["root"], config_check_only=True,
    )

    assert result.exit_code != 0
    assert _check(result, "path.catalog_file").status is Status.FAIL
    assert not paths["catalog"].exists()
