"""Tests for dry-run and atomic warehouse seed import."""

import json
import sqlite3
from pathlib import Path

import pytest

from app.services.warehouse.import_service import WarehouseSeedImportService
from app.services.warehouse.seed_service import build_warehouse_seed, write_warehouse_seed
from app.storage.user_database import UserDatabase
from app.storage.warehouse_seed_database import (
    WarehouseSeedDatabase,
    WarehouseSeedImportError,
)
from scripts import import_warehouse_products

ROOT = Path(__file__).resolve().parents[1]
CANDIDATES = ROOT / "source" / "warehouse_product_candidates.json"


@pytest.fixture
def prepared(tmp_path: Path):
    database_path = tmp_path / "warehouse.db"
    UserDatabase(database_path).initialize()
    seed_path = tmp_path / "seed.json"
    seed = build_warehouse_seed(CANDIDATES)
    write_warehouse_seed(seed, seed_path)
    return database_path, seed_path, seed


def _count(path: Path, table: str) -> int:
    connection = sqlite3.connect(path)
    try:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        connection.close()


def test_dry_run_reports_creates_without_writes(prepared) -> None:
    database_path, seed_path, _ = prepared
    seed_before = seed_path.read_bytes()

    result = WarehouseSeedImportService(database_path).run(seed_path)

    assert len(result.create) == 414
    assert result.rule_create == ("daily_ground_coffee", "daily_pitcher_milk")
    assert not result.unchanged
    assert not result.conflicts
    assert not result.applied
    assert _count(database_path, "warehouse_products") == 0
    assert seed_path.read_bytes() == seed_before


def test_apply_imports_every_product_alias_and_operation(prepared) -> None:
    database_path, seed_path, seed = prepared

    result = WarehouseSeedImportService(database_path).run(seed_path, apply=True)

    assert len(result.create) == 414
    assert result.rule_create == ("daily_ground_coffee", "daily_pitcher_milk")
    assert result.applied
    assert _count(database_path, "warehouse_products") == 414
    assert _count(database_path, "warehouse_product_aliases") == 8
    assert _count(database_path, "warehouse_product_operation_types") == 1242
    assert _count(database_path, "warehouse_required_rules") == 2
    connection = sqlite3.connect(database_path)
    try:
        counts = dict(
            connection.execute(
                "SELECT is_active, COUNT(*) FROM warehouse_products GROUP BY is_active"
            ).fetchall()
        )
        stable_ids = {
            row[0]
            for row in connection.execute(
                "SELECT stable_id FROM warehouse_products"
            ).fetchall()
        }
    finally:
        connection.close()
    assert counts == {1: 414}
    assert stable_ids == {product.stable_id for product in seed.products}


def test_second_apply_is_fully_unchanged(prepared) -> None:
    database_path, seed_path, _ = prepared
    service = WarehouseSeedImportService(database_path)
    service.run(seed_path, apply=True)

    second = service.run(seed_path, apply=True)

    assert not second.create
    assert len(second.unchanged) == 414
    assert not second.rule_create
    assert second.rule_unchanged == (
        "daily_ground_coffee",
        "daily_pitcher_milk",
    )
    assert second.applied
    assert _count(database_path, "warehouse_products") == 414


def test_existing_stable_id_difference_conflicts_without_changes(prepared) -> None:
    database_path, seed_path, _ = prepared
    service = WarehouseSeedImportService(database_path)
    service.run(seed_path, apply=True)
    payload = json.loads(seed_path.read_text(encoding="utf-8"))
    product = payload["products"][0]
    product["base_unit"] = "l" if product["base_unit"] != "l" else "kg"
    changed_path = seed_path.with_name("changed.json")
    changed_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    result = service.run(changed_path, apply=True)

    assert result.conflicts
    assert not result.applied
    assert _count(database_path, "warehouse_products") == 414


def test_failure_after_inserts_rolls_back_entire_batch(
    prepared,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path, _, seed = prepared
    repository = WarehouseSeedDatabase(database_path)

    def fail_validation(_connection) -> None:
        raise WarehouseSeedImportError("simulated final failure")

    monkeypatch.setattr(repository, "_validate_final_namespace", fail_validation)
    with pytest.raises(WarehouseSeedImportError, match="simulated"):
        repository.import_seed(seed, apply=True)
    assert _count(database_path, "warehouse_products") == 0
    assert _count(database_path, "warehouse_required_rules") == 0


def test_required_rule_conflict_rolls_back_new_products(prepared) -> None:
    database_path, _, seed = prepared
    owner = next(
        product for product in seed.products
        if product.official_name == "Молотый кофе из гриндера"
    )
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            """
            INSERT INTO warehouse_products (
                stable_id, official_name, normalized_name, base_unit,
                is_active, created_at_utc, updated_at_utc, revision
            ) VALUES (?, ?, ?, 'kg', 1, 'now', 'now', 1)
            """,
            (owner.stable_id, owner.official_name, owner.normalized_name),
        )
        product_id = connection.execute(
            "SELECT id FROM warehouse_products WHERE stable_id=?",
            (owner.stable_id,),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO warehouse_required_rules (
                stable_id, product_id, operation_type, quantity_mode,
                suggested_quantity_scaled, base_unit, weekdays_mask,
                question_text, sort_order, allow_not_applicable,
                minimum_scaled, maximum_scaled, is_active, created_at_utc,
                created_by, updated_at_utc, updated_by, revision
            ) VALUES (
                'daily_ground_coffee', ?, 'writeoff', 'recommended',
                70000, 'kg', 127, 'Конфликт', 10, 1,
                NULL, NULL, 1, 'now', NULL, 'now', NULL, 1
            )
            """,
            (product_id,),
        )
        connection.commit()
    finally:
        connection.close()

    result = WarehouseSeedDatabase(database_path).import_seed(seed, apply=True)

    assert result.rule_conflicts
    assert not result.applied
    assert _count(database_path, "warehouse_products") == 1
    assert _count(database_path, "warehouse_required_rules") == 1


def test_wrong_schema_version_is_rejected(tmp_path: Path) -> None:
    database_path = tmp_path / "v2.db"
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA user_version = 2")
    connection.close()
    seed = build_warehouse_seed(CANDIDATES)

    with pytest.raises(
        WarehouseSeedImportError,
        match="v3 through v11 required",
    ):
        WarehouseSeedDatabase(database_path).import_seed(seed)


def test_existing_namespace_conflict_rejects_whole_seed(prepared) -> None:
    database_path, _, seed = prepared
    first = seed.products[0]
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            """
            INSERT INTO warehouse_products (
                stable_id, official_name, normalized_name, base_unit,
                is_active, created_at_utc, updated_at_utc, revision
            ) VALUES ('preexisting', ?, ?, 'kg', 1, 'now', 'now', 1)
            """,
            (first.official_name, first.normalized_name),
        )
        connection.commit()
    finally:
        connection.close()

    result = WarehouseSeedDatabase(database_path).import_seed(seed, apply=True)

    assert result.conflicts
    assert not result.applied
    assert _count(database_path, "warehouse_products") == 1


def test_cli_requires_database_rejects_production_and_prints_dry_run(
    prepared,
    capsys,
) -> None:
    database_path, seed_path, _ = prepared
    with pytest.raises(SystemExit):
        import_warehouse_products.main([])
    with pytest.raises(SystemExit):
        import_warehouse_products.main(
            [
                "--database",
                str(ROOT / "data" / "barbot.db"),
                "--input",
                str(seed_path),
            ]
        )

    assert import_warehouse_products.main(
        ["--database", str(database_path), "--input", str(seed_path)]
    ) == 0
    output = capsys.readouterr().out
    assert "create=414" in output
    assert "DRY-RUN" in output
    assert _count(database_path, "warehouse_products") == 0


def test_cli_production_override_requires_all_explicit_guards(
    prepared, monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path, seed_path, _ = prepared
    monkeypatch.setattr(
        import_warehouse_products, "PRODUCTION_DATABASE", database_path.resolve()
    )
    base = ["--database", str(database_path), "--input", str(seed_path)]
    assert import_warehouse_products.main(base + ["--allow-production"]) == 0
    assert _count(database_path, "warehouse_products") == 0
    with pytest.raises(SystemExit):
        import_warehouse_products.main(
            base + ["--apply", "--allow-production",
                    "--confirm-production-import", "WRONG"]
        )

    assert import_warehouse_products.main(
        base + ["--apply", "--allow-production",
                "--confirm-production-import", "APPLY-WAREHOUSE-SEED"]
    ) == 0
    assert _count(database_path, "warehouse_products") == 414
