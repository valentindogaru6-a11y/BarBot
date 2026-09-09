#!/usr/bin/env python3
"""Generate the deterministic approved warehouse seed artifact."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.warehouse.seed_service import (
    build_warehouse_seed,
    write_warehouse_seed,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate warehouse product seed")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    arguments = parser.parse_args(argv)
    seed = build_warehouse_seed(arguments.input)
    write_warehouse_seed(seed, arguments.output)
    active = sum(product.is_active for product in seed.products)
    print(
        f"Seed products: {len(seed.products)} "
        f"(active={active}, inactive={len(seed.products) - active})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
