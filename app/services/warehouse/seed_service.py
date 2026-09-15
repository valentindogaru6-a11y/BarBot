"""Deterministic warehouse seed generation and validation."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.models.catalog_item import MAX_STABLE_ID_LENGTH
from app.models.warehouse.seed import WarehouseProductsSeed
from app.services.stable_id import generate_stable_id
from app.services.warehouse.normalization import normalize_warehouse_name

_UNIT_OVERRIDES = {
    "laym": "kg",
    "med": "kg",
    "angostura": "l",
    "easy_foam": "l",
    "voda": "l",
    "tsvety_dlya": "pcs",
}


class WarehouseSeedValidationError(ValueError):
    """Raised when candidate input or a warehouse seed is invalid."""


_OWNER_PRODUCTS = (
    {
        "key": "owner_ground_coffee",
        "official_name": "Молотый кофе из гриндера",
        "base_unit": "kg",
        "aliases": [
            "кофе из гриндера",
            "молотый кофе с гриндера",
            "остаток кофе в гриндере",
            "кофе гриндер",
        ],
        "required_rule": {
            "mode": "recommended",
            "quantity": "0.06",
            "unit": "kg",
            "schedule": "daily",
        },
    },
    {
        "key": "owner_pitcher_milk",
        "official_name": "Остаток молока в питчерах",
        "base_unit": "l",
        "aliases": [
            "молоко в питчерах",
            "остаток молока",
            "молоко с питчеров",
            "молоко из питчеров",
        ],
        "required_rule": {
            "mode": "manual",
            "unit": "l",
            "schedule": "daily",
        },
    },
)


def _claim_namespace(
    namespace: dict[str, str],
    normalized: str,
    stable_id: str,
    kind: str,
) -> None:
    owner = namespace.get(normalized)
    if owner is not None:
        raise WarehouseSeedValidationError(
            f"Namespace conflict ({kind}): {normalized} [{owner}, {stable_id}]"
        )
    namespace[normalized] = stable_id


def validate_warehouse_seed(payload: dict[str, Any]) -> WarehouseProductsSeed:
    try:
        seed = WarehouseProductsSeed.model_validate(payload)
    except ValidationError as exc:
        raise WarehouseSeedValidationError(
            f"Warehouse seed validation failed: {exc.error_count()} errors"
        ) from exc

    stable_ids: set[str] = set()
    source_ids: set[str] = set()
    namespace: dict[str, str] = {}
    for product in seed.products:
        if product.stable_id in stable_ids:
            raise WarehouseSeedValidationError(
                f"Duplicate stable ID: {product.stable_id}"
            )
        stable_ids.add(product.stable_id)
        expected_name = normalize_warehouse_name(product.official_name)
        if expected_name != product.normalized_name:
            raise WarehouseSeedValidationError(
                f"Invalid normalized official name: {product.stable_id}"
            )
        _claim_namespace(namespace, expected_name, product.stable_id, "official")
        seen_aliases: set[str] = set()
        for alias in product.aliases:
            normalized_alias = normalize_warehouse_name(alias)
            if not normalized_alias or normalized_alias == expected_name:
                raise WarehouseSeedValidationError(
                    f"Invalid alias for {product.stable_id}: {alias}"
                )
            if normalized_alias in seen_aliases:
                raise WarehouseSeedValidationError(
                    f"Duplicate alias for {product.stable_id}: {alias}"
                )
            seen_aliases.add(normalized_alias)
            _claim_namespace(namespace, normalized_alias, product.stable_id, "alias")
        if len(product.source_candidate_ids) != len(set(product.source_candidate_ids)):
            raise WarehouseSeedValidationError(
                f"Duplicate source candidate ID: {product.stable_id}"
            )
        if any(not value.strip() for value in product.source_candidate_ids):
            raise WarehouseSeedValidationError(
                f"Empty source candidate ID: {product.stable_id}"
            )
        duplicate_sources = source_ids.intersection(product.source_candidate_ids)
        if duplicate_sources:
            raise WarehouseSeedValidationError(
                f"Source candidate reused: {sorted(duplicate_sources)[0]}"
            )
        source_ids.update(product.source_candidate_ids)
    return seed


def load_warehouse_seed(path: str | Path) -> WarehouseProductsSeed:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WarehouseSeedValidationError("Cannot read warehouse seed JSON") from exc
    if not isinstance(payload, dict):
        raise WarehouseSeedValidationError("Warehouse seed root must be an object")
    return validate_warehouse_seed(payload)


def _read_candidate_report(path: str | Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WarehouseSeedValidationError("Cannot read candidate report") from exc
    if not isinstance(payload, dict) or payload.get("format_version") != 1:
        raise WarehouseSeedValidationError("Unsupported candidate report")
    if not isinstance(payload.get("candidates"), list):
        raise WarehouseSeedValidationError("Candidate list is missing")
    if not isinstance(payload.get("unresolved"), list):
        raise WarehouseSeedValidationError("Unresolved list is missing")
    return payload


def _candidate_product(entry: dict[str, Any], review_ids: set[str]) -> dict[str, Any]:
    candidate_id = str(entry.get("candidate_id", ""))
    normalized_name = str(entry.get("normalized_name", ""))
    is_lime_fresh = "фреш лайм" in normalized_name or "лайм фреш" in normalized_name
    base_unit = (
        "l"
        if is_lime_fresh
        else _UNIT_OVERRIDES.get(candidate_id, entry.get("suggested_base_unit"))
    )
    if base_unit not in {"kg", "l", "pcs"}:
        raise WarehouseSeedValidationError(
            f"Base unit remains unresolved: {candidate_id}"
        )
    needs_review = candidate_id in review_ids
    warnings = entry.get("warnings")
    if not isinstance(warnings, list) or not all(isinstance(v, str) for v in warnings):
        raise WarehouseSeedValidationError(f"Invalid warnings: {candidate_id}")
    warnings = list(warnings)
    suggested_unit = entry.get("suggested_base_unit")
    if suggested_unit != base_unit:
        warnings.append(
            f"Base unit approved by owner policy: {suggested_unit or 'null'} → {base_unit}"
        )
    return {
        "stable_id": candidate_id,
        "official_name": entry.get("suggested_official_name"),
        "normalized_name": normalized_name,
        "base_unit": base_unit,
        "is_active": True,
        "allowed_operation_types": ["writeoff", "transfer", "development"],
        "aliases": [],
        "source_candidate_ids": [candidate_id],
        "review_status": "review_required" if needs_review else "approved",
        "warnings": sorted(set(warnings)),
        "owner_metadata": None,
    }


def _owner_product(spec: dict[str, Any], occupied_ids: set[str]) -> dict[str, Any]:
    name = str(spec["official_name"])
    stable_id = generate_stable_id(name, occupied_ids, MAX_STABLE_ID_LENGTH)
    occupied_ids.add(stable_id)
    return {
        "stable_id": stable_id,
        "official_name": name,
        "normalized_name": normalize_warehouse_name(name),
        "base_unit": spec["base_unit"],
        "is_active": True,
        "allowed_operation_types": ["writeoff", "transfer", "development"],
        "aliases": spec["aliases"],
        "source_candidate_ids": [spec["key"]],
        "review_status": "owner_required",
        "warnings": [],
        "owner_metadata": {"required_rule": spec["required_rule"]},
    }


def build_warehouse_seed(candidate_path: str | Path) -> WarehouseProductsSeed:
    source = _read_candidate_report(candidate_path)
    review_ids = {
        entry["candidate_id"]
        for entry in source["unresolved"]
        if isinstance(entry, dict) and isinstance(entry.get("candidate_id"), str)
    }
    products = [_candidate_product(entry, review_ids) for entry in source["candidates"]]
    occupied_ids = {str(product["stable_id"]) for product in products}
    products.extend(_owner_product(spec, occupied_ids) for spec in _OWNER_PRODUCTS)
    products.sort(key=lambda product: str(product["stable_id"]))
    payload = {
        "format_version": 1,
        "source_candidate_format_version": source["format_version"],
        "source_catalog_sha256": source.get("catalog_sha256"),
        "policy": {
            "clean_candidates_active": True,
            "review_required_active": True,
            "owner_required_active": True,
        },
        "products": products,
    }
    return validate_warehouse_seed(payload)


def write_warehouse_seed(seed: WarehouseProductsSeed, output_path: str | Path) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as output:
            temporary = Path(output.name)
            json.dump(seed.model_dump(mode="json"), output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
