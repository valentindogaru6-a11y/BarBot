"""Read-only production-readiness checks for an explicitly supplied setup.

The command intentionally reads process environment only.  It never loads the
repository ``.env``, initializes SQLite, creates directories, or contacts
Telegram.  Offline tests should pass explicit temporary paths together with
``--allow-temporary-paths``.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import PROJECT_ROOT
from app.services.warehouse.business_date_service import BusinessDateService
from app.services.warehouse.seed_service import (
    WarehouseSeedValidationError,
    load_warehouse_seed,
)
from app.storage.migrations import CURRENT_SCHEMA_VERSION
from app.storage.release_inspection import (
    SQLiteInspectionError,
    inspect_sqlite_database,
)

EXPECTED_PYTHON = (3, 12)
EXPECTED_TIMEZONE = "Europe/Kyiv"
EXPECTED_CUTOFF = "23:00"
EXPECTED_SEED_PRODUCTS = 414
DEFAULT_SEED_PATH = PROJECT_ROOT / "source" / "warehouse_products_seed.json"
DEFAULT_CATALOG_PATH = PROJECT_ROOT / "data" / "catalog.json"
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "barbot.db"
DEFAULT_REPORT_PATH = PROJECT_ROOT / "reports" / "warehouse"
DEFAULT_AUDIT_PATH = PROJECT_ROOT / "logs" / "admin_audit.jsonl"
DEFAULT_BACKUP_ROOT = Path("/opt/barbot-backups/production")
BACKUP_STAMP_PATTERN = re.compile(r"^\d{8}T\d{6}Z$")


class Status(StrEnum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    SKIP = "SKIP"


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: Status
    detail: str


@dataclass(frozen=True, slots=True)
class PreflightResult:
    checks: tuple[Check, ...]
    controlled_rollout: bool = False

    @property
    def exit_code(self) -> int:
        return 2 if any(check.status is Status.FAIL for check in self.checks) else 0

    @property
    def verdict(self) -> str:
        if self.exit_code:
            return "NO-GO"
        hardening = any(
            check.name.startswith("permissions.") and check.status is Status.WARN
            for check in self.checks
        )
        if self.controlled_rollout and hardening:
            return "READY FOR BACKUP WITH REQUIRED POST-BACKUP HARDENING"
        return "READY FOR BACKUP" if self.controlled_rollout else "PASS"

    def render(self) -> str:
        lines = [
            f"[{check.status.value}] {check.name}: {check.detail}"
            for check in self.checks
        ]
        counts = {
            status: sum(check.status is status for check in self.checks)
            for status in Status
        }
        lines.append(
            "SUMMARY "
            + " ".join(f"{status.value}={counts[status]}" for status in Status)
        )
        if self.controlled_rollout:
            lines.append(f"VERDICT {self.verdict}")
        return "\n".join(lines)


def _path(value: str | Path, *, root: Path = PROJECT_ROOT) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate.resolve(strict=False)


def _lexical_path(value: str | Path, *, root: Path) -> tuple[Path, bool]:
    """Return a normalized absolute path and whether input contained traversal."""

    source = Path(value).expanduser()
    traversal = ".." in source.parts
    if not source.is_absolute():
        source = root / source
    return Path(os.path.abspath(source)), traversal


def _has_symlink_component(path: Path) -> bool:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                return True
        except FileNotFoundError:
            continue
    return False


def _nearest_existing_parent(path: Path) -> Path | None:
    current = path
    while not current.exists():
        parent = current.parent
        if parent == current:
            return None
        current = parent
    return current if current.is_dir() else None


def _configured(environment: Mapping[str, str], key: str) -> bool:
    return bool(environment.get(key, "").strip())


def _valid_owner_ids(value: str) -> bool:
    try:
        parsed = json.loads(value) if value.lstrip().startswith("[") else value.split(",")
        if not isinstance(parsed, list) or not parsed:
            return False
        return all(not isinstance(item, bool) and int(item) > 0 for item in parsed)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False


def _bool(value: str, default: bool) -> bool:
    normalized = value.strip().casefold()
    if not normalized:
        return default
    return normalized in {"1", "true", "yes", "on"}


def _unsafe_broad(path: Path, root: Path) -> bool:
    home = Path.home().resolve()
    return path in {Path("/"), home, root.resolve()}


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _temporary(path: Path) -> bool:
    temporary_roots = {
        Path(tempfile.gettempdir()).resolve(),
        Path("/var/tmp").resolve(),
    }
    return any(_inside(path, root) for root in temporary_roots)


def _writable_target(path: Path, *, expect_directory: bool) -> tuple[Status, str]:
    if expect_directory and path.exists() and not path.is_dir():
        return Status.FAIL, "path points to a file"
    if not expect_directory and path.exists() and path.is_dir():
        return Status.FAIL, "path points to a directory"
    probe = path if expect_directory and path.exists() else path.parent
    if not probe.exists() or not probe.is_dir():
        return Status.FAIL, "parent directory is missing"
    if not os.access(probe, os.R_OK | os.W_OK | os.X_OK):
        return Status.FAIL, "parent directory is not readable/writable"
    if path.exists() and not os.access(path, os.R_OK | os.W_OK):
        return Status.FAIL, "target is not readable/writable"
    return Status.PASS, "existing target is accessible" if path.exists() else "parent can prepare target"


def _check_database(path: Path, *, allow_supported_upgrade: bool) -> list[Check]:
    if not path.is_file():
        return [Check("database.read_only", Status.FAIL, "database file is missing")]
    try:
        inspection = inspect_sqlite_database(path)
    except SQLiteInspectionError:
        return [Check("database.read_only", Status.FAIL, "read-only validation failed")]
    version = inspection.version
    supported_upgrade = (
        allow_supported_upgrade
        and 1 <= version < CURRENT_SCHEMA_VERSION
        and inspection.schema_compatible
        and inspection.migration_chain_complete
    )
    schema_ok = version == CURRENT_SCHEMA_VERSION and inspection.schema_compatible
    schema_status = (
        Status.PASS if schema_ok
        else Status.WARN if supported_upgrade
        else Status.FAIL
    )
    schema_detail = (
        f"v{version}; expected v{CURRENT_SCHEMA_VERSION}"
        if schema_ok
        else f"supported migration required: v{version} → v{CURRENT_SCHEMA_VERSION}"
        if supported_upgrade
        else (
            f"v{version}; expected a valid schema with complete migration "
            f"chain to v{CURRENT_SCHEMA_VERSION}"
        )
    )
    return [
        Check("database.schema", schema_status, schema_detail),
        Check(
            "database.integrity",
            Status.PASS if inspection.integrity_ok else Status.FAIL,
            "integrity_check ok" if inspection.integrity_ok else "integrity_check failed",
        ),
        Check(
            "database.foreign_keys",
            Status.PASS if not inspection.foreign_key_violations else Status.FAIL,
            "no violations" if not inspection.foreign_key_violations
            else f"foreign key violations found: {inspection.foreign_key_violations}",
        ),
    ]


def _permission_check(
    name: str,
    path: Path,
    *,
    directory: bool,
    owner_only_directory: bool = False,
) -> Check:
    if not path.exists():
        return Check(f"permissions.{name}", Status.SKIP, "path does not exist yet")
    mode = stat.S_IMODE(path.stat().st_mode)
    allowed = (
        (mode & 0o077) == 0
        if owner_only_directory or not directory
        else (mode & 0o027) == 0
    )
    policy = (
        "0700 or stricter" if owner_only_directory
        else "0750 or stricter" if directory
        else "0600 or stricter"
    )
    return Check(
        f"permissions.{name}",
        Status.PASS if allowed else Status.WARN,
        f"mode {mode:04o}; {policy} required"
        + (" after verified backup while service is stopped" if not allowed else ""),
    )


def _report_file_permission_check(path: Path) -> Check:
    if not path.is_dir():
        return Check(
            "permissions.report_files",
            Status.SKIP,
            "report directory does not exist yet",
        )
    report_files = tuple(item for item in path.rglob("*.xlsx") if item.is_file())
    broad = sum(
        stat.S_IMODE(item.stat().st_mode) & 0o077 != 0
        for item in report_files
    )
    return Check(
        "permissions.report_files",
        Status.WARN if broad else Status.PASS,
        f"files={len(report_files)} broad_modes={broad}; 0600 or stricter required"
        + (" after verified backup while service is stopped" if broad else ""),
    )


def _check_backup_policy(
    backup_root: Path,
    *,
    backup_target: Path | None,
    project_root: Path,
    database: Path,
    reports: Path,
    audit: Path,
    catalog: Path,
    allow_temporary_paths: bool,
) -> list[Check]:
    resolved = backup_root.resolve(strict=False)
    forbidden = {
        Path("/"),
        Path.home().resolve(),
        project_root.resolve(),
        database.parent.resolve(),
    }
    collision = (
        resolved in forbidden
        or any(
            resolved == path.resolve(strict=False)
            or _inside(resolved, path.resolve(strict=False))
            or _inside(path.resolve(strict=False), resolved)
            for path in (database, reports, audit, catalog)
        )
    )
    unsafe = (
        _has_symlink_component(backup_root)
        or collision
        or backup_root == Path("/opt")
        or (not allow_temporary_paths and backup_root != DEFAULT_BACKUP_ROOT)
    )
    if backup_root.exists() and not backup_root.is_dir():
        root_check = Check(
            "backup.root", Status.FAIL, "backup root is not a directory"
        )
    elif unsafe:
        root_check = Check(
            "backup.root",
            Status.FAIL,
            "unsafe, symlinked, broad, or colliding backup root",
        )
    else:
        parent = _nearest_existing_parent(backup_root)
        root_check = Check(
            "backup.root",
            Status.PASS if parent is not None else Status.FAIL,
            "approved root exists"
            if backup_root.exists()
            else "approved root can be prepared at Stage 1",
        )
    checks = [
        root_check,
        _permission_check(
            "backup_root",
            backup_root,
            directory=True,
            owner_only_directory=True,
        ),
    ]
    if backup_target is None:
        checks.append(Check(
            "backup.timestamp_target",
            Status.SKIP,
            "Stage 1 timestamp target not supplied",
        ))
    else:
        valid_name = bool(BACKUP_STAMP_PATTERN.fullmatch(backup_target.name))
        valid_parent = backup_target.parent == backup_root
        valid = (
            valid_name
            and valid_parent
            and not backup_target.exists()
            and not backup_target.is_symlink()
        )
        checks.append(Check(
            "backup.timestamp_target", Status.PASS if valid else Status.FAIL,
            "new non-existing timestamp target" if valid
            else "target must be a new direct YYYYMMDDTHHMMSSZ child of backup root",
        ))
    return checks


def run_preflight(
    environment: Mapping[str, str],
    *,
    database_path: str | Path | None = None,
    report_path: str | Path | None = None,
    audit_path: str | Path | None = None,
    catalog_path: str | Path | None = None,
    seed_path: str | Path = DEFAULT_SEED_PATH,
    timezone: str = EXPECTED_TIMEZONE,
    cutoff: str = EXPECTED_CUTOFF,
    polling_owner_count: int | None = None,
    config_check_only: bool = False,
    allow_temporary_paths: bool = False,
    allow_supported_upgrade: bool = False,
    allow_missing_runtime_paths: bool = False,
    backup_root: str | Path = DEFAULT_BACKUP_ROOT,
    backup_target: str | Path | None = None,
    project_root: Path = PROJECT_ROOT,
) -> PreflightResult:
    """Return deterministic checks without changing any target."""

    root = project_root.resolve()
    database_raw = database_path or environment.get(
        "DATABASE_PATH", DEFAULT_DATABASE_PATH
    )
    report_raw = report_path or environment.get(
        "WAREHOUSE_REPORTS_PATH", DEFAULT_REPORT_PATH
    )
    audit_raw = audit_path or DEFAULT_AUDIT_PATH
    catalog_raw = catalog_path or environment.get(
        "CATALOG_PATH", DEFAULT_CATALOG_PATH
    )
    database = _path(
        database_raw,
        root=root,
    )
    reports = _path(
        report_raw,
        root=root,
    )
    audit = _path(audit_raw, root=root)
    catalog = _path(
        catalog_raw,
        root=root,
    )
    seed = _path(seed_path, root=root)
    backup_root_path, backup_root_traversal = _lexical_path(backup_root, root=root)
    backup_target_path, backup_target_traversal = (
        _lexical_path(backup_target, root=root)
        if backup_target is not None else (None, False)
    )
    controlled = allow_supported_upgrade or allow_missing_runtime_paths
    checks: list[Check] = []

    checks.append(Check(
        "python.version",
        Status.PASS if sys.version_info[:2] == EXPECTED_PYTHON else Status.FAIL,
        f"{sys.version_info.major}.{sys.version_info.minor}; expected 3.12",
    ))
    for module in (
        "aiogram", "pydantic", "pydantic_settings", "sqlite3",
        "xlsxwriter", "xlrd",
    ):
        try:
            importlib.import_module(module)
        except Exception:
            checks.append(Check(f"dependency.{module}", Status.FAIL, "import failed"))
        else:
            checks.append(Check(f"dependency.{module}", Status.PASS, "importable"))

    token_present = _configured(environment, "BOT_TOKEN")
    checks.append(Check(
        "config.BOT_TOKEN",
        Status.PASS if token_present else Status.FAIL,
        "configured" if token_present else "missing",
    ))
    super_admin_present = _configured(environment, "SUPER_ADMIN_IDS")
    legacy_admin_present = _configured(environment, "ADMIN_IDS")
    owner_value = (
        environment["SUPER_ADMIN_IDS"] if super_admin_present
        else environment.get("ADMIN_IDS", "")
    )
    owner_valid = (super_admin_present or legacy_admin_present) and _valid_owner_ids(
        owner_value
    )
    owner_status = (
        Status.PASS if owner_valid and super_admin_present
        else Status.WARN if owner_valid
        else Status.FAIL
    )
    checks.append(Check(
        "config.SUPER_ADMIN_IDS",
        owner_status,
        "configured" if owner_valid and super_admin_present
        else "deprecated ADMIN_IDS fallback configured" if owner_valid
        else "missing or invalid",
    ))
    for key in ("CATALOG_PATH", "DATABASE_PATH", "WAREHOUSE_REPORTS_PATH"):
        checks.append(Check(
            f"config.{key}", Status.PASS,
            "configured" if _configured(environment, key) else "default",
        ))
    warehouse_enabled = _bool(environment.get("WAREHOUSE_ENABLED", ""), False)
    checks.append(Check(
        "config.WAREHOUSE_ENABLED",
        Status.PASS if warehouse_enabled else Status.WARN,
        "enabled" if warehouse_enabled else "disabled; enable only at rollout stop point",
    ))

    try:
        ZoneInfo(timezone)
        service = BusinessDateService(timezone=timezone, cutoff=cutoff)
    except (ZoneInfoNotFoundError, ValueError):
        checks.append(Check("warehouse.timezone_cutoff", Status.FAIL, "invalid timezone or cutoff"))
    else:
        valid = service.timezone == EXPECTED_TIMEZONE and service.cutoff == EXPECTED_CUTOFF
        checks.append(Check(
            "warehouse.timezone_cutoff",
            Status.PASS if valid else Status.FAIL,
            f"{service.timezone} {service.cutoff}; expected Europe/Kyiv 23:00",
        ))

    paths = (("database", database, database_raw, False),
             ("reports", reports, report_raw, True),
             ("audit", audit, audit_raw, False),
             ("catalog", catalog, catalog_raw, False))
    for name, path, raw_path, is_directory in paths:
        lexical, traversal = _lexical_path(raw_path, root=root)
        if traversal or _has_symlink_component(lexical):
            checks.append(Check(f"path.{name}", Status.FAIL, "path contains traversal or symlink"))
            continue
        if _unsafe_broad(path, root):
            checks.append(Check(f"path.{name}", Status.FAIL, "unsafe broad path"))
            continue
        status, detail = _writable_target(path, expect_directory=is_directory)
        if (
            name == "reports" and status is Status.FAIL
            and not path.exists() and allow_missing_runtime_paths
            and not warehouse_enabled and _inside(path, root)
        ):
            parent = _nearest_existing_parent(path)
            if parent is not None and os.access(parent, os.W_OK | os.X_OK):
                status = Status.WARN
                detail = "missing; safely preparable at Stage 1 with mode 0750"
        checks.append(Check(f"path.{name}", status, detail))
    overlap = (
        database == audit
        or database == reports
        or audit == reports
        or _inside(database, reports)
        or _inside(audit, reports)
    )
    checks.append(Check(
        "path.separation", Status.FAIL if overlap else Status.PASS,
        "unsafe overlap" if overlap else "database, reports, and audit are separated",
    ))
    temporary_report = _temporary(reports)
    checks.append(Check(
        "path.production_reports",
        Status.FAIL if not _inside(reports, root)
        else Status.PASS if allow_temporary_paths or not temporary_report
        else Status.FAIL,
        "report directory escapes repository" if not _inside(reports, root)
        else "temporary path explicitly allowed"
        if temporary_report and allow_temporary_paths
        else "inside repository and not inside a temporary directory"
        if not temporary_report
        else "temporary report path is unsafe for production",
    ))
    expected_audit = (root / "logs" / "admin_audit.jsonl").resolve(strict=False)
    checks.append(Check(
        "path.audit_runtime",
        Status.PASS if audit == expected_audit else Status.FAIL,
        "matches application audit path" if audit == expected_audit
        else "does not match fixed application audit path",
    ))
    checks.append(Check(
        "path.catalog_file",
        Status.PASS if catalog.is_file() else Status.FAIL,
        "catalog file exists" if catalog.is_file() else "catalog file is missing",
    ))

    checks.extend((
        _permission_check("database", database, directory=False),
        _permission_check("database_wal", Path(f"{database}-wal"), directory=False),
        _permission_check("database_shm", Path(f"{database}-shm"), directory=False),
        _permission_check("catalog", catalog, directory=False),
        _permission_check("audit", audit, directory=False),
        _permission_check("data_directory", database.parent, directory=True),
        _permission_check("reports_directory", reports, directory=True),
        _permission_check("audit_directory", audit.parent, directory=True),
        _report_file_permission_check(reports),
    ))
    checks.extend(_check_backup_policy(
        backup_root_path,
        backup_target=backup_target_path,
        project_root=root,
        database=database,
        reports=reports,
        audit=audit,
        catalog=catalog,
        allow_temporary_paths=allow_temporary_paths,
    ))
    if backup_root_traversal:
        checks.append(Check("backup.path_input", Status.FAIL, "backup root contains traversal"))
    if backup_target_traversal:
        checks.append(Check("backup.target_input", Status.FAIL, "backup target contains traversal"))

    if polling_owner_count is None:
        checks.append(Check("polling.owner", Status.SKIP, "operator count not supplied"))
    elif polling_owner_count > 1:
        checks.append(Check("polling.owner", Status.FAIL, "more than one owner declared"))
    elif polling_owner_count == 1:
        checks.append(Check("polling.owner", Status.PASS, "exactly one owner declared"))
    else:
        checks.append(Check("polling.owner", Status.WARN, "polling is stopped"))

    try:
        from app.bot import build_dispatcher, main, router_names  # noqa: F401
        from app.keyboards.warehouse_callbacks import WarehouseCallback

        callback = WarehouseCallback(
            action="kZ", object_id=9_223_372_036_854_775_807,
            aux=9_223_372_036_854_775_807,
        ).pack()
        routers_ok = "app.handlers.warehouse_corrections" in router_names(
            warehouse_enabled=True
        )
        callbacks_ok = len(callback.encode()) <= 64
    except Exception:
        checks.append(Check("application.imports", Status.FAIL, "entrypoint/router import failed"))
    else:
        checks.append(Check("application.imports", Status.PASS, "entrypoint and routers importable"))
        checks.append(Check(
            "application.routers", Status.PASS if routers_ok and callbacks_ok else Status.FAIL,
            "warehouse routers registered; callback payload within 64 bytes"
            if routers_ok and callbacks_ok else "router or callback validation failed",
        ))
    checks.append(Check(
        "schema.target",
        Status.PASS if CURRENT_SCHEMA_VERSION == 12 else Status.FAIL,
        f"v{CURRENT_SCHEMA_VERSION}; expected v12",
    ))

    try:
        seed_model = load_warehouse_seed(seed)
    except WarehouseSeedValidationError:
        checks.append(Check("seed.validation", Status.FAIL, "seed is missing or invalid"))
    else:
        active = sum(product.is_active for product in seed_model.products)
        owners = sum(product.review_status == "owner_required" for product in seed_model.products)
        valid_seed = len(seed_model.products) == EXPECTED_SEED_PRODUCTS and active == 414 and owners == 2
        checks.append(Check(
            "seed.validation", Status.PASS if valid_seed else Status.FAIL,
            f"products={len(seed_model.products)} active={active} owner_required={owners}",
        ))

    try:
        from app.bot import main as application_main
        from app.services.warehouse.telegram_delivery import TelegramReportSender

        adapter_ok = (
            TelegramReportSender.__module__
            == "app.services.warehouse.telegram_delivery"
            and "TelegramReportSender" in application_main.__code__.co_names
        )
    except Exception:
        adapter_ok = False
    checks.append(Check(
        "delivery.adapter", Status.PASS if adapter_ok else Status.FAIL,
        "TelegramReportSender configured in production entrypoint" if adapter_ok
        else "production delivery adapter validation failed",
    ))

    if config_check_only:
        checks.append(Check("database.read_only", Status.SKIP, "config-check-only mode"))
    else:
        checks.extend(_check_database(
            database, allow_supported_upgrade=allow_supported_upgrade
        ))
    return PreflightResult(tuple(checks), controlled_rollout=controlled)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only BarBot release preflight")
    parser.add_argument("--config-check-only", action="store_true")
    parser.add_argument("--db")
    parser.add_argument("--report-dir")
    parser.add_argument("--audit-log")
    parser.add_argument("--catalog")
    parser.add_argument("--seed", default=str(DEFAULT_SEED_PATH))
    parser.add_argument("--timezone", default=EXPECTED_TIMEZONE)
    parser.add_argument("--cutoff", default=EXPECTED_CUTOFF)
    parser.add_argument("--polling-owner-count", type=int)
    parser.add_argument("--allow-temporary-paths", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--allow-supported-upgrade", action="store_true")
    parser.add_argument("--allow-missing-runtime-paths", action="store_true")
    parser.add_argument("--backup-root", default=str(DEFAULT_BACKUP_ROOT))
    parser.add_argument("--backup-target")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    result = run_preflight(
        os.environ,
        database_path=arguments.db,
        report_path=arguments.report_dir,
        audit_path=arguments.audit_log,
        catalog_path=arguments.catalog,
        seed_path=arguments.seed,
        timezone=arguments.timezone,
        cutoff=arguments.cutoff,
        polling_owner_count=arguments.polling_owner_count,
        config_check_only=arguments.config_check_only,
        allow_temporary_paths=arguments.allow_temporary_paths,
        allow_supported_upgrade=arguments.allow_supported_upgrade,
        allow_missing_runtime_paths=arguments.allow_missing_runtime_paths,
        backup_root=arguments.backup_root,
        backup_target=arguments.backup_target,
    )
    print(result.render())
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
