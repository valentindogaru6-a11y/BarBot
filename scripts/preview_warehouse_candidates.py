#!/usr/bin/env python3
"""Preview warehouse product candidates without touching SQLite or catalog."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.warehouse.candidate_extractor import (
    build_candidate_report,
    write_candidate_report,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only preview of warehouse candidates from catalog JSON"
    )
    parser.add_argument("--catalog", default="data/catalog.json")
    parser.add_argument("--output")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    report = build_candidate_report(Path(arguments.catalog))
    summary = report["summary"]
    print(
        "Warehouse candidates: "
        f"{summary['candidate_count']} "
        f"(high={summary['high']}, medium={summary['medium']}, "
        f"low={summary['low']}, unresolved={summary['unresolved']})"
    )
    print(
        f"Unit conflicts: {summary['unit_conflicts']}; "
        f"fuzzy review groups: {summary['fuzzy_review_groups']}"
    )
    if arguments.output:
        write_candidate_report(report, Path(arguments.output))
        print(f"Review artifact written: {Path(arguments.output).name}")
    else:
        print("Read-only preview: no output file was written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
