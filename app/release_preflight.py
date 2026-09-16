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
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Mapping, Sequence
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import PROJECT_ROOT
from app.services.warehouse.business_date_service import BusinessDateService
from app.services.warehouse.seed_service import (
    WarehouseSeedValidationError,
    load_warehouse_seed,
)
from app.storage.migrations import CURRENT_SCHEMA_VERSION

EXPECTED_PYTHON = (3, 12)
EXPECTED_TIMEZONE = "Europe/Kyiv"
EXPECTED_CUTOFF = "23:00"
EXPECTED_SEED_PRODUCTS = 414
DEFAULT_SEED_PATH = PROJECT_ROOT / "source" / "warehouse_products_seed.json"
DEFAULT_CATALOG_PATH = PROJECT_ROOT / "data" / "catalog.json"
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "barbot.db"
DEFAULT_REPORT_PATH = PROJECT_ROOT / "reports" / "warehouse"
DEFAULT_AUDIT_PATH = PROJECT_ROOT / "logs" / "admin_audit.jsonl"


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

    @property
    def exit_code(self) -> int:
        return 2 if any(check.status is Status.FAIL for check in self.checks) else 0

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
        return "\n".join(lines)


def _path(value: str | Path, *, root: Path = PROJECT_ROOT) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate.resolve(strict=False)


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


def _check_database(path: Path) -> list[Check]:
    if not path.is_file():
        return [Check("database.read_only", Status.FAIL, "database file is missing")]
    connection: sqlite3.Connection | None = None
    try:
        uri = f"file:{quote(str(path), safe='/')}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5, isolation_level=None)
        connection.execute("PRAGMA query_only=ON")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    except sqlite3.Error:
        return [Check("database.read_only", Status.FAIL, "read-only validation failed")]
    finally:
        if connection is not None:
            connection.close()
    return [
        Check(
            "database.schema",
            Status.PASS if version == CURRENT_SCHEMA_VERSION else Status.FAIL,
            f"v{version}; expected v{CURRENT_SCHEMA_VERSION}",
        ),
        Check(
            "database.integrity",
            Status.PASS if integrity == "ok" else Status.FAIL,
            "integrity_check ok" if integrity == "ok" else "integrity_check failed",
        ),
        Check(
            "database.foreign_keys",
            Status.PASS if not foreign_keys else Status.FAIL,
            "no violations" if not foreign_keys else "foreign key violations found",
        ),
    ]


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
    project_root: Path = PROJECT_ROOT,
) -> PreflightResult:
    """Return deterministic checks without changing any target."""

    root = project_root.resolve()
    database = _path(
        database_path or environment.get("DATABASE_PATH", DEFAULT_DATABASE_PATH),
        root=root,
    )
    reports = _path(
        report_path or environment.get("WAREHOUSE_REPORTS_PATH", DEFAULT_REPORT_PATH),
        root=root,
    )
    audit = _path(audit_path or DEFAULT_AUDIT_PATH, root=root)
    catalog = _path(
        catalog_path or environment.get("CATALOG_PATH", DEFAULT_CATALOG_PATH),
        root=root,
    )
    seed = _path(seed_path, root=root)
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

    paths = (("database", database, False), ("reports", reports, True),
             ("audit", audit, False), ("catalog", catalog, False))
    for name, path, is_directory in paths:
        if _unsafe_broad(path, root):
            checks.append(Check(f"path.{name}", Status.FAIL, "unsafe broad path"))
            continue
        status, detail = _writable_target(path, expect_directory=is_directory)
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
        Status.PASS if CURRENT_SCHEMA_VERSION == 11 else Status.FAIL,
        f"v{CURRENT_SCHEMA_VERSION}; expected v11",
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
        checks.extend(_check_database(database))
    return PreflightResult(tuple(checks))


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
    )
    print(result.render())
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
