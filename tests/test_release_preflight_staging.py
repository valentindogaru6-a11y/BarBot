"""Controlled release-staging path, permission, and backup policy tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.release_preflight import Status, run_preflight


def _check(result, name: str):
    return next(check for check in result.checks if check.name == name)


def _fixture(tmp_path: Path, *, warehouse_enabled: bool = False, reports: bool = True):
    root = tmp_path / "project"
    data = root / "data"
    logs = root / "logs"
    data.mkdir(parents=True)
    logs.mkdir()
    report_path = root / "reports" / "warehouse"
    if reports:
        report_path.mkdir(parents=True)
    database = data / "barbot.db"
    database.write_bytes(b"")
    catalog = data / "catalog.json"
    catalog.write_text("{}\n", encoding="utf-8")
    audit = logs / "admin_audit.jsonl"
    environment = {
        "BOT_TOKEN": "test-only-secret",
        "SUPER_ADMIN_IDS": "[1]",
        "DATABASE_PATH": str(database),
        "CATALOG_PATH": str(catalog),
        "WAREHOUSE_REPORTS_PATH": str(report_path),
        "WAREHOUSE_ENABLED": "true" if warehouse_enabled else "false",
    }
    return root, environment, database, catalog, audit, report_path


def _run(tmp_path: Path, *, warehouse_enabled: bool = False, reports: bool = True, **kwargs):
    root, environment, database, catalog, audit, report_path = _fixture(
        tmp_path, warehouse_enabled=warehouse_enabled, reports=reports
    )
    backup_root = kwargs.pop("backup_root", tmp_path / "dedicated-backups")
    result = run_preflight(
        environment,
        database_path=database,
        report_path=kwargs.pop("report_path", report_path),
        audit_path=audit,
        catalog_path=catalog,
        config_check_only=True,
        polling_owner_count=1,
        allow_temporary_paths=True,
        backup_root=backup_root,
        project_root=root,
        **kwargs,
    )
    return result, {
        "root": root, "database": database, "catalog": catalog,
        "audit": audit, "reports": report_path, "backup_root": backup_root,
    }


def test_missing_reports_warn_only_for_disabled_controlled_rollout(tmp_path: Path) -> None:
    controlled, paths = _run(
        tmp_path / "controlled", reports=False, allow_missing_runtime_paths=True
    )
    strict, _ = _run(tmp_path / "strict", reports=False)
    enabled, _ = _run(
        tmp_path / "enabled", warehouse_enabled=True, reports=False,
        allow_missing_runtime_paths=True,
    )

    assert _check(controlled, "path.reports").status is Status.WARN
    assert not paths["reports"].exists()
    assert _check(strict, "path.reports").status is Status.FAIL
    assert _check(enabled, "path.reports").status is Status.FAIL


@pytest.mark.parametrize("kind", ["file", "symlink", "collision", "traversal", "broad"])
def test_unsafe_report_path_is_never_staged(tmp_path: Path, kind: str) -> None:
    base = tmp_path / kind
    root, environment, database, catalog, audit, expected = _fixture(base, reports=False)
    if kind == "file":
        candidate = root / "reports-file"
        candidate.write_text("not a directory", encoding="utf-8")
    elif kind == "symlink":
        candidate = root / "reports-link"
        candidate.symlink_to(root, target_is_directory=True)
    elif kind == "collision":
        candidate = database
    elif kind == "traversal":
        candidate = Path("reports") / ".." / "warehouse"
    else:
        candidate = root
    result = run_preflight(
        environment, database_path=database, report_path=candidate,
        audit_path=audit, catalog_path=catalog, config_check_only=True,
        polling_owner_count=1, allow_temporary_paths=True,
        allow_missing_runtime_paths=True, backup_root=base / "backups",
        project_root=root,
    )

    assert _check(result, "path.reports").status is Status.FAIL


def test_broad_file_modes_are_visible_warnings_without_chmod(tmp_path: Path) -> None:
    _, paths = _run(tmp_path, allow_missing_runtime_paths=True)
    report = paths["reports"] / "report.xlsx"
    report.write_bytes(b"test")
    result = run_preflight(
        {
            "BOT_TOKEN": "test-only-secret", "SUPER_ADMIN_IDS": "[1]",
            "WAREHOUSE_ENABLED": "false",
        },
        database_path=paths["database"], report_path=paths["reports"],
        audit_path=paths["audit"], catalog_path=paths["catalog"],
        config_check_only=True, polling_owner_count=1,
        allow_temporary_paths=True, allow_missing_runtime_paths=True,
        backup_root=paths["backup_root"], project_root=paths["root"],
    )
    before = {path: path.stat().st_mode for path in (paths["database"], paths["catalog"], paths["root"] / "data")}

    assert _check(result, "permissions.database").status is Status.WARN
    assert _check(result, "permissions.catalog").status is Status.WARN
    assert _check(result, "permissions.report_files").status is Status.WARN
    assert result.verdict == "READY FOR BACKUP WITH REQUIRED POST-BACKUP HARDENING"
    assert {path: path.stat().st_mode for path in before} == before


def test_restrictive_modes_need_no_hardening(tmp_path: Path) -> None:
    root, environment, database, catalog, audit, reports = _fixture(tmp_path)
    for file_path in (database, catalog):
        file_path.chmod(0o600)
    for directory in (database.parent, reports, audit.parent):
        directory.chmod(0o750)
    backup_root = tmp_path / "safe-backups"
    backup_root.mkdir(mode=0o700)

    result = run_preflight(
        environment, database_path=database, report_path=reports,
        audit_path=audit, catalog_path=catalog, config_check_only=True,
        polling_owner_count=1, allow_temporary_paths=True,
        allow_missing_runtime_paths=True, backup_root=backup_root,
        project_root=root,
    )

    assert result.exit_code == 0
    assert result.verdict == "READY FOR BACKUP"


def test_approved_backup_root_and_new_timestamp_target_pass(tmp_path: Path) -> None:
    backup_root = tmp_path / "backups"
    result, _ = _run(
        tmp_path / "case", backup_root=backup_root,
        backup_target=backup_root / "20260916T120000Z",
        allow_missing_runtime_paths=True,
    )
    assert _check(result, "backup.root").status is Status.PASS
    assert _check(result, "backup.timestamp_target").status is Status.PASS
    assert not backup_root.exists()


@pytest.mark.parametrize("target", ["repo", "data", "broad", "symlink"])
def test_unsafe_backup_roots_are_rejected(tmp_path: Path, target: str) -> None:
    root, environment, database, catalog, audit, reports = _fixture(tmp_path)
    if target == "repo":
        backup_root = root
    elif target == "data":
        backup_root = database.parent
    elif target == "broad":
        backup_root = tmp_path
    else:
        actual = tmp_path / "actual"
        actual.mkdir()
        backup_root = tmp_path / "backup-link"
        backup_root.symlink_to(actual, target_is_directory=True)
    result = run_preflight(
        environment, database_path=database, report_path=reports,
        audit_path=audit, catalog_path=catalog, config_check_only=True,
        polling_owner_count=1, allow_temporary_paths=True,
        allow_missing_runtime_paths=True, backup_root=backup_root,
        project_root=root,
    )
    assert _check(result, "backup.root").status is Status.FAIL


def test_existing_or_malformed_backup_timestamp_target_fails(tmp_path: Path) -> None:
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    existing = backup_root / "20260916T120000Z"
    existing.mkdir()
    existing_result, _ = _run(
        tmp_path / "existing", backup_root=backup_root,
        backup_target=existing, allow_missing_runtime_paths=True,
    )
    malformed_result, _ = _run(
        tmp_path / "malformed", backup_root=backup_root,
        backup_target=backup_root / "latest", allow_missing_runtime_paths=True,
    )
    assert _check(existing_result, "backup.timestamp_target").status is Status.FAIL
    assert _check(malformed_result, "backup.timestamp_target").status is Status.FAIL
