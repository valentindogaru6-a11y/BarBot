"""Tests for warehouse seed generation and validation."""

import copy
import json
from pathlib import Path

import pytest

from app.services.warehouse.seed_service import (
    WarehouseSeedValidationError,
    build_warehouse_seed,
    validate_warehouse_seed,
    write_warehouse_seed,
)
from app.services.warehouse.normalization import normalize_warehouse_name

ROOT = Path(__file__).resolve().parents[1]
CANDIDATES = ROOT / "source" / "warehouse_product_candidates.json"
CATALOG = ROOT / "data" / "catalog.json"


@pytest.fixture(scope="module")
def seed():
    return build_warehouse_seed(CANDIDATES)


def _products(seed) -> dict[str, object]:
    return {product.stable_id: product for product in seed.products}


def test_seed_contains_all_candidates_and_owner_products(seed) -> None:
    source = json.loads(CANDIDATES.read_text(encoding="utf-8"))
    source_ids = {entry["candidate_id"] for entry in source["candidates"]}
    products = _products(seed)

    assert len(source_ids) == 412
    assert source_ids <= set(products)
    assert len(seed.products) == 414
    assert len(products) == 414
    assert sum(product.is_active for product in seed.products) == 396
    assert sum(not product.is_active for product in seed.products) == 18


def test_policy_units_and_separate_products(seed) -> None:
    products = _products(seed)

    assert products["laym"].base_unit.value == "kg"
    assert products["med"].base_unit.value == "kg"
    assert products["angostura"].base_unit.value == "l"
    assert products["easy_foam"].base_unit.value == "l"
    assert products["izi_foam"].base_unit.value == "kg"
    assert products["voda"].base_unit.value == "l"
    assert "dash" in " ".join(products["angostura"].warnings)
    assert "кулера" in " ".join(products["voda"].warnings)
    lime_fresh = [
        product
        for product in seed.products
        if "фреш лайм" in product.normalized_name
        or "лайм фреш" in product.normalized_name
    ]
    assert lime_fresh
    assert {product.base_unit.value for product in lime_fresh} == {"l"}
    assert all(product.stable_id != "laym" for product in lime_fresh)


def test_review_and_clean_activation_policy(seed) -> None:
    review = [p for p in seed.products if p.review_status == "review_required"]
    clean = [p for p in seed.products if p.review_status == "approved"]

    assert len(review) == 18
    assert all(not product.is_active for product in review)
    assert len(clean) == 394
    assert all(product.is_active for product in clean)


def test_owner_products_are_active_separate_and_valid(seed) -> None:
    owners = [p for p in seed.products if p.review_status == "owner_required"]
    assert len(owners) == 2
    assert all(product.is_active for product in owners)
    assert all(len(product.aliases) == 4 for product in owners)
    by_name = {product.official_name: product for product in owners}
    coffee = by_name["Молотый кофе из гриндера"]
    milk = by_name["Остаток молока в питчерах"]
    assert coffee.owner_metadata.required_rule.quantity == "0.06"
    assert coffee.owner_metadata.required_rule.unit.value == "kg"
    assert milk.owner_metadata.required_rule.mode == "manual"
    assert milk.owner_metadata.required_rule.unit.value == "l"
    assert any(p.official_name == "Молоко" for p in seed.products)
    assert any(p.official_name == "Кофе зерновой" for p in seed.products)


def test_namespace_ids_and_operations_are_complete(seed) -> None:
    assert len({p.stable_id for p in seed.products}) == len(seed.products)
    namespace: set[str] = set()
    for product in seed.products:
        assert product.normalized_name not in namespace
        namespace.add(product.normalized_name)
        assert {value.value for value in product.allowed_operation_types} == {
            "writeoff",
            "transfer",
        }
        for alias in product.aliases:
            normalized = normalize_warehouse_name(alias)
            assert normalized not in namespace
            namespace.add(normalized)


def test_generation_is_byte_identical_and_sources_unchanged(
    seed,
    tmp_path: Path,
) -> None:
    candidate_before = CANDIDATES.read_bytes()
    catalog_before = CATALOG.read_bytes()
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"

    write_warehouse_seed(seed, first)
    write_warehouse_seed(build_warehouse_seed(CANDIDATES), second)

    assert first.read_bytes() == second.read_bytes()
    assert CANDIDATES.read_bytes() == candidate_before
    assert CATALOG.read_bytes() == catalog_before
    assert "generated_at" not in first.read_text(encoding="utf-8")


def test_generation_does_not_read_catalog(monkeypatch) -> None:
    original = Path.read_text

    def guarded_read(path: Path, *args, **kwargs):
        if path.resolve() == CATALOG.resolve():
            raise AssertionError("seed generation must not read catalog")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read)
    assert len(build_warehouse_seed(CANDIDATES).products) == 414


def _payload(seed) -> dict:
    return copy.deepcopy(seed.model_dump(mode="json"))


@pytest.mark.parametrize(
    "case",
    [
        "duplicate_stable_id",
        "duplicate_official_name",
        "alias_conflict",
        "alias_official_conflict",
        "invalid_base_unit",
        "empty_operations",
        "unknown_operation",
        "invalid_active",
        "missing_field",
        "malformed_owner_metadata",
        "incompatible_required_unit",
    ],
)
def test_seed_validation_rejects_invalid_documents(seed, case: str) -> None:
    payload = _payload(seed)
    first, second = payload["products"][:2]
    if case == "duplicate_stable_id":
        second["stable_id"] = first["stable_id"]
    elif case == "duplicate_official_name":
        second["official_name"] = first["official_name"]
        second["normalized_name"] = first["normalized_name"]
    elif case == "alias_conflict":
        first["aliases"] = ["общий конфликт"]
        second["aliases"] = ["Общий конфликт"]
    elif case == "alias_official_conflict":
        second["aliases"] = [first["official_name"]]
    elif case == "invalid_base_unit":
        first["base_unit"] = "ml"
    elif case == "empty_operations":
        first["allowed_operation_types"] = []
    elif case == "unknown_operation":
        first["allowed_operation_types"] = ["writeoff", "consume"]
    elif case == "invalid_active":
        first["is_active"] = "yes"
    elif case == "missing_field":
        del first["official_name"]
    else:
        owner = next(p for p in payload["products"] if p["owner_metadata"])
        if case == "malformed_owner_metadata":
            del owner["owner_metadata"]["required_rule"]["schedule"]
        else:
            owner["owner_metadata"]["required_rule"]["unit"] = "pcs"

    with pytest.raises(WarehouseSeedValidationError):
        validate_warehouse_seed(payload)
