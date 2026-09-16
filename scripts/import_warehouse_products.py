#!/usr/bin/env python3
"""Dry-run or explicitly apply a warehouse seed to a prepared SQLite DB."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.warehouse.seed_service import WarehouseSeedValidationError
from app.services.warehouse.import_service import WarehouseSeedImportService
from app.storage.warehouse_seed_database import WarehouseSeedImportError

PRODUCTION_DATABASE = (PROJECT_ROOT / "data" / "barbot.db").resolve()
PRODUCTION_CONFIRMATION = "APPLY-WAREHOUSE-SEED"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import warehouse products")
    parser.add_argument("--database", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--allow-production", action="store_true")
    parser.add_argument("--confirm-production-import")
    arguments = parser.parse_args(argv)
    database = Path(arguments.database)
    if database.resolve() == PRODUCTION_DATABASE:
        if not arguments.allow_production:
            parser.error("production dry-run requires --allow-production")
        if arguments.apply and (
            arguments.confirm_production_import != PRODUCTION_CONFIRMATION
        ):
            parser.error(
                "production apply requires --allow-production and "
                f"--confirm-production-import {PRODUCTION_CONFIRMATION}"
            )
    if not database.is_file():
        parser.error("database must already exist")
    try:
        result = WarehouseSeedImportService(database).run(
            arguments.input,
            apply=arguments.apply,
        )
    except (WarehouseSeedValidationError, WarehouseSeedImportError) as exc:
        print(
            "create=0 unchanged=0 conflicts=0 "
            "rule_create=0 rule_unchanged=0 rule_conflicts=0 errors=1\n"
            f"{exc}"
        )
        return 2
    print(
        f"create={len(result.create)} unchanged={len(result.unchanged)} "
        f"conflicts={len(result.conflicts)} "
        f"rule_create={len(result.rule_create)} "
        f"rule_unchanged={len(result.rule_unchanged)} "
        f"rule_conflicts={len(result.rule_conflicts)} "
        f"errors={len(result.errors)}"
    )
    for conflict in result.conflicts:
        print(f"CONFLICT: {conflict}")
    for conflict in result.rule_conflicts:
        print(f"RULE CONFLICT: {conflict}")
    print("APPLIED" if result.applied else "DRY-RUN")
    return 0 if result.is_valid else 2


if __name__ == "__main__":
    raise SystemExit(main())
