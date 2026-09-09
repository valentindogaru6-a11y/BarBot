"""Deterministic product and destination resolution."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

from app.models.warehouse.nomenclature import BaseUnit
from app.models.warehouse.operations import WarehouseOperationType
from app.utils.warehouse import normalize_warehouse_name


@dataclass(frozen=True, slots=True)
class ProductOption:
    id: int
    official_name: str
    normalized_name: str
    base_unit: BaseUnit
    aliases: tuple[str, ...]
    operation_types: frozenset[str]


@dataclass(frozen=True, slots=True)
class LocationOption:
    id: int
    name: str
    normalized_name: str
    can_be_source: bool
    can_be_destination: bool


@dataclass(frozen=True, slots=True)
class Resolution:
    exact_id: int | None
    candidates: tuple[tuple[int, str, int], ...]
    error_code: str | None


def _score(left: str, right: str) -> int:
    return round(SequenceMatcher(None, left, right).ratio() * 10_000)


def resolve_product(
    text: str,
    operation_type: WarehouseOperationType,
    products: tuple[ProductOption, ...],
    *,
    limit: int = 5,
) -> Resolution:
    normalized = normalize_warehouse_name(text)
    required_operation = (
        "product_writeoff"
        if operation_type is WarehouseOperationType.WRITEOFF
        else "transfer"
    )
    eligible = tuple(
        product for product in products if required_operation in product.operation_types
    )
    official = [p for p in eligible if p.normalized_name == normalized]
    if len(official) == 1:
        return Resolution(official[0].id, (), None)
    aliases = [
        product
        for product in eligible
        if normalized in {
            normalize_warehouse_name(alias) for alias in product.aliases
        }
    ]
    if len(aliases) == 1:
        return Resolution(aliases[0].id, (), None)

    ranked: list[tuple[int, str, int]] = []
    for product in eligible:
        variants = (product.normalized_name,) + tuple(
            normalize_warehouse_name(alias) for alias in product.aliases
        )
        score = max(_score(normalized, variant) for variant in variants)
        if score >= 3_500:
            ranked.append((product.id, product.official_name, score))
    ranked.sort(key=lambda item: (-item[2], item[1].casefold(), item[0]))
    candidates = tuple(ranked[:limit])
    return Resolution(
        None,
        candidates,
        "ambiguous_product" if candidates else "unknown_product",
    )


def resolve_destination(
    text: str,
    locations: tuple[LocationOption, ...],
    *,
    source_location_id: int,
    limit: int = 5,
) -> Resolution:
    normalized = normalize_warehouse_name(text)
    eligible = tuple(location for location in locations if location.can_be_destination)
    exact = [location for location in eligible if location.normalized_name == normalized]
    if len(exact) == 1:
        if exact[0].id == source_location_id:
            return Resolution(None, (), "same_source_destination")
        return Resolution(exact[0].id, (), None)
    ranked = sorted(
        (
            (location.id, location.name, _score(normalized, location.normalized_name))
            for location in eligible
            if location.id != source_location_id
        ),
        key=lambda item: (-item[2], item[1].casefold(), item[0]),
    )
    candidates = tuple(item for item in ranked[:limit] if item[2])
    return Resolution(
        None,
        candidates,
        "ambiguous_destination" if candidates else "unknown_destination",
    )
