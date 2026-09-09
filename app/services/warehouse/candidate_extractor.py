"""Deterministic, read-only warehouse candidate extraction from the catalog."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from app.models.catalog import Catalog
from app.models.catalog_item import MAX_STABLE_ID_LENGTH, CatalogItem, Ingredient
from app.services.stable_id import generate_stable_id
from app.services.warehouse.normalization import normalize_warehouse_name
from app.storage.json_storage import JsonCatalogStorage

FORMAT_VERSION = 1

_UNIT_DIMENSIONS = {
    "г": "mass",
    "гр": "mass",
    "грамм": "mass",
    "грамма": "mass",
    "граммов": "mass",
    "кг": "mass",
    "килограмм": "mass",
    "килограмма": "mass",
    "килограммов": "mass",
    "мл": "volume",
    "миллилитр": "volume",
    "миллилитра": "volume",
    "миллилитров": "volume",
    "л": "volume",
    "литр": "volume",
    "литра": "volume",
    "литров": "volume",
    "шт": "count",
    "штука": "count",
    "штуки": "count",
    "штук": "count",
}
_BASE_UNITS = {"mass": "kg", "volume": "l", "count": "pcs"}
_GARNISH_RE = re.compile(
    r"(?:гарниш|декор|украшен|по вкусу|цедр|дольк|веточк|листик)"
)
_PERISHABLE_PATTERNS = {
    "citrus": re.compile(r"апельс|лимон|лайм|грейп|мандар|цитрус"),
    "juices": re.compile(r"\bсок\b|\bфреш"),
    "milk": re.compile(r"молок|сливк"),
    "coffee": re.compile(r"кофе|эспресс|колд.?брю"),
    "greens": re.compile(r"мят|базилик|розмарин|зелень|тимьян"),
    "other_perishables": re.compile(
        r"ягод|клубник|малин|ежевик|облепих|ананас|яблок|груш|"
        r"виноград|банан|персик|киви|маракуй|пюре"
    ),
}


def _normalize_unit(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized.strip(" .,:;()[]{}")


def _amount_text(ingredient: Ingredient) -> str:
    return str(ingredient.amount).strip()


def _item_observations(catalog: Catalog) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    for category in catalog.categories:
        for item in category.items:
            observations.extend(
                _ingredient_observations(
                    item,
                    category_id=category.id,
                    subcategory_id=None,
                    path_active=category.is_active and item.is_active,
                )
            )
        for subcategory in category.subcategories:
            for item in subcategory.items:
                observations.extend(
                    _ingredient_observations(
                        item,
                        category_id=category.id,
                        subcategory_id=subcategory.id,
                        path_active=(
                            category.is_active
                            and subcategory.is_active
                            and item.is_active
                        ),
                    )
                )
    return observations


def _ingredient_observations(
    item: CatalogItem,
    *,
    category_id: str,
    subcategory_id: str | None,
    path_active: bool,
) -> list[dict[str, Any]]:
    return [
        {
            "raw_name": ingredient.name.strip(),
            "unit": ingredient.unit.strip(),
            "amount": _amount_text(ingredient),
            "source_reference": {
                "source": "catalog",
                "category_id": category_id,
                "subcategory_id": subcategory_id,
                "item_id": item.id,
                "item_active": item.is_active,
                "path_active": path_active,
            },
        }
        for ingredient in item.ingredients
    ]


def _choose_name(observations: list[dict[str, Any]]) -> str:
    counts = Counter(str(entry["raw_name"]) for entry in observations)
    return sorted(
        counts,
        key=lambda value: (-counts[value], value.casefold(), value),
    )[0]


def _unit_analysis(
    observations: list[dict[str, Any]],
) -> tuple[str | None, str, list[str], set[str]]:
    normalized_units = [_normalize_unit(str(entry["unit"])) for entry in observations]
    dimensions = {
        _UNIT_DIMENSIONS[unit]
        for unit in normalized_units
        if unit in _UNIT_DIMENSIONS
    }
    unknown = sorted(
        {unit for unit in normalized_units if unit and unit not in _UNIT_DIMENSIONS}
    )
    has_blank = any(not unit for unit in normalized_units)
    warnings: list[str] = []
    if has_blank:
        warnings.append("Есть наблюдение с пустой единицей")
    if unknown:
        warnings.append("Неизвестные единицы: " + ", ".join(unknown))
    if len(dimensions) > 1:
        warnings.append("Конфликт размерностей единиц")
        return None, "unresolved", warnings, dimensions
    if not dimensions:
        warnings.append("Недостаточно данных для определения base unit")
        return None, "unresolved", warnings, dimensions

    dimension = next(iter(dimensions))
    base_unit = _BASE_UNITS[dimension]
    if has_blank or unknown:
        confidence = "low"
    elif len(observations) == 1:
        confidence = "medium"
    else:
        confidence = "high"
    return base_unit, confidence, warnings, dimensions


def _review_warnings(
    normalized_name: str,
    observations: list[dict[str, Any]],
) -> list[str]:
    warnings: list[str] = []
    amounts = [str(entry["amount"]).casefold() for entry in observations]
    if _GARNISH_RE.search(normalized_name) or any(
        "по вкусу" in amount for amount in amounts
    ):
        warnings.append("Возможный garnish/decor или количество «по вкусу»")
    for entry in observations:
        unit = _normalize_unit(str(entry["unit"]))
        amount = str(entry["amount"]).replace(",", ".")
        try:
            numeric = Decimal(amount)
        except InvalidOperation:
            continue
        if _UNIT_DIMENSIONS.get(unit) == "count" and 0 < numeric < 1:
            warnings.append("Наблюдается дробная часть штуки")
            break
    if not any(bool(entry["source_reference"]["path_active"]) for entry in observations):
        warnings.append("Ингредиент встречается только в скрытых карточках")
    return warnings


def _source_references(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    references = [
        {
            **entry["source_reference"],
            "observed_name": entry["raw_name"],
            "observed_unit": entry["unit"],
            "observed_amount": entry["amount"],
        }
        for entry in observations
    ]
    return sorted(
        references,
        key=lambda value: (
            value["category_id"],
            value["subcategory_id"] or "",
            value["item_id"],
            value["observed_name"].casefold(),
            value["observed_unit"].casefold(),
            value["observed_amount"],
        ),
    )


def _fuzzy_review_groups(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for index, left in enumerate(candidates):
        left_name = str(left["normalized_name"])
        if len(left_name) < 5:
            continue
        for right in candidates[index + 1 :]:
            right_name = str(right["normalized_name"])
            if len(right_name) < 5:
                continue
            similarity = SequenceMatcher(None, left_name, right_name).ratio()
            if similarity < 0.92:
                continue
            groups.append(
                {
                    "candidate_ids": [left["candidate_id"], right["candidate_id"]],
                    "normalized_names": [left_name, right_name],
                    "similarity": round(similarity, 3),
                    "warning": "Похожие названия не объединены автоматически",
                }
            )
    return groups


def _owner_required(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_name = {entry["normalized_name"]: entry["candidate_id"] for entry in candidates}
    required = (
        {
            "candidate_id": "owner_ground_coffee",
            "suggested_official_name": "Молотый кофе из гриндера",
            "suggested_base_unit": "kg",
            "daily_quantity": {"mode": "fixed", "value": "0.06", "unit": "kg"},
        },
        {
            "candidate_id": "owner_pitcher_milk",
            "suggested_official_name": "Остаток молока в питчерах",
            "suggested_base_unit": "l",
            "daily_quantity": {"mode": "manual"},
        },
    )
    result = []
    for entry in required:
        normalized = normalize_warehouse_name(entry["suggested_official_name"])
        result.append(
            {
                **entry,
                "normalized_name": normalized,
                "matching_extracted_candidate_id": by_name.get(normalized),
                "source": "owner_requirement",
                "review_status": "pending",
            }
        )
    return result


def _perishable_review(candidates: list[dict[str, Any]]) -> dict[str, list[dict[str, str]]]:
    return {
        group: [
            {
                "candidate_id": str(candidate["candidate_id"]),
                "name": str(candidate["suggested_official_name"]),
            }
            for candidate in candidates
            if pattern.search(str(candidate["normalized_name"]))
        ]
        for group, pattern in _PERISHABLE_PATTERNS.items()
    }


def build_candidate_report(catalog_path: str | Path) -> dict[str, Any]:
    """Load a validated catalog and return a deterministic review artifact."""

    path = Path(catalog_path)
    catalog_bytes = path.read_bytes()
    catalog = JsonCatalogStorage(path).load()
    observations = _item_observations(catalog)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    raw_unresolved: list[dict[str, Any]] = []
    for observation in observations:
        normalized = normalize_warehouse_name(str(observation["raw_name"]))
        if normalized:
            grouped[normalized].append(observation)
        else:
            raw_unresolved.append(
                {
                    "raw_name": observation["raw_name"],
                    "reason": "Название пусто после нормализации",
                    "source_reference": observation["source_reference"],
                }
            )

    candidates: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = list(raw_unresolved)
    occupied_ids: set[str] = set()
    for normalized_name in sorted(grouped):
        entries = grouped[normalized_name]
        official_name = _choose_name(entries)
        candidate_id = generate_stable_id(
            official_name,
            occupied_ids,
            MAX_STABLE_ID_LENGTH,
        )
        occupied_ids.add(candidate_id)
        base_unit, confidence, warnings, dimensions = _unit_analysis(entries)
        warnings.extend(_review_warnings(normalized_name, entries))
        warnings = sorted(set(warnings))
        observed_names = sorted(
            {str(entry["raw_name"]) for entry in entries},
            key=lambda value: (value.casefold(), value),
        )
        observed_units = sorted(
            {str(entry["unit"]) for entry in entries},
            key=lambda value: (value.casefold(), value),
        )
        candidate = {
            "candidate_id": candidate_id,
            "suggested_official_name": official_name,
            "normalized_name": normalized_name,
            "suggested_base_unit": base_unit,
            "base_unit_confidence": confidence,
            "observed_names": observed_names,
            "observed_units": observed_units,
            "source_references": _source_references(entries),
            "occurrence_count": len(entries),
            "suggested_aliases": [
                name for name in observed_names if name != official_name
            ],
            "warnings": warnings,
            "review_status": "pending",
        }
        candidates.append(candidate)
        if len(dimensions) > 1:
            conflicts.append(
                {
                    "candidate_id": candidate_id,
                    "name": official_name,
                    "dimensions": sorted(dimensions),
                    "observed_units": observed_units,
                    "reason": "Конфликт размерностей единиц",
                }
            )
        if confidence == "unresolved" or warnings:
            unresolved.append(
                {
                    "candidate_id": candidate_id,
                    "name": official_name,
                    "reasons": warnings,
                }
            )

    fuzzy_groups = _fuzzy_review_groups(candidates)
    confidence_counts = Counter(
        str(candidate["base_unit_confidence"]) for candidate in candidates
    )
    active_occurrences = sum(
        bool(entry["source_reference"]["path_active"]) for entry in observations
    )
    report = {
        "format_version": FORMAT_VERSION,
        "catalog_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
        "generated_from": [
            {
                "source": "validated_catalog_json",
                "name": path.name,
                "sha256": hashlib.sha256(catalog_bytes).hexdigest(),
            }
        ],
        "summary": {
            "ingredient_occurrences": len(observations),
            "active_path_occurrences": active_occurrences,
            "hidden_path_occurrences": len(observations) - active_occurrences,
            "candidate_count": len(candidates),
            "high": confidence_counts["high"],
            "medium": confidence_counts["medium"],
            "low": confidence_counts["low"],
            "unresolved": confidence_counts["unresolved"],
            "unit_conflicts": len(conflicts),
            "fuzzy_review_groups": len(fuzzy_groups),
            "review_required": len(unresolved),
        },
        "candidates": candidates,
        "conflicts": conflicts,
        "unresolved": sorted(
            unresolved,
            key=lambda value: (
                str(value.get("candidate_id", "")),
                str(value.get("raw_name", "")),
            ),
        ),
        "fuzzy_review_groups": fuzzy_groups,
        "owner_required_candidates": _owner_required(candidates),
        "perishable_review": _perishable_review(candidates),
    }
    return report


def write_candidate_report(report: dict[str, Any], output_path: str | Path) -> None:
    """Pretty-print one report atomically after an explicit output request."""

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
            json.dump(report, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
