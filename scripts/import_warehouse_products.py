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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import warehouse products")
    parser.add_argument("--database", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--apply", action="store_true")
    arguments = parser.parse_args(argv)
    database = Path(arguments.database)
    if database.resolve() == PRODUCTION_DATABASE:
        parser.error("production database path is prohibited")
    if not database.is_file():
        parser.error("database must already exist")
    try:
        result = WarehouseSeedImportService(database).run(
            arguments.input,
            apply=arguments.apply,
        )
    except (WarehouseSeedValidationError, WarehouseSeedImportError) as exc:
        print(f"create=0 unchanged=0 conflicts=0 errors=1\n{exc}")
        return 2
    print(
        f"create={len(result.create)} unchanged={len(result.unchanged)} "
        f"conflicts={len(result.conflicts)} errors={len(result.errors)}"
    )
    for conflict in result.conflicts:
        print(f"CONFLICT: {conflict}")
    print("APPLIED" if result.applied else "DRY-RUN")
    return 0 if result.is_valid else 2


if __name__ == "__main__":
    raise SystemExit(main())
