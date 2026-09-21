"""Deterministic product and destination resolution."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher

from app.models.warehouse.nomenclature import BaseUnit
from app.models.warehouse.operations import WarehouseOperationType
from app.utils.warehouse import normalize_warehouse_name

_SEARCH_PUNCTUATION_RE = re.compile(r"[^\w\s]+", re.UNICODE)


def _search_normalize(value: str) -> str:
    normalized = normalize_warehouse_name(value).replace("_", " ")
    return " ".join(_SEARCH_PUNCTUATION_RE.sub(" ", normalized).split())


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


def _ratio(left: str, right: str) -> float:
    return SequenceMatcher(None, left, right).ratio()


def _product_score(
    query: str,
    variant: str,
    token_frequency: Counter[str],
) -> int:
    query_tokens = tuple(query.split())
    variant_tokens = tuple(variant.split())
    if not query_tokens or not variant_tokens:
        return 0

    pairs = sorted(
        (
            (_ratio(query_token, variant_token), query_index, variant_index)
            for query_index, query_token in enumerate(query_tokens)
            for variant_index, variant_token in enumerate(variant_tokens)
        ),
        reverse=True,
    )
    matched_query: set[int] = set()
    matched_variant: set[int] = set()
    for score, query_index, variant_index in pairs:
        if score < 0.72:
            break
        if query_index in matched_query or variant_index in matched_variant:
            continue
        matched_query.add(query_index)
        matched_variant.add(variant_index)
    matched = len(matched_query)
    shorter = min(len(query_tokens), len(variant_tokens))
    if matched < shorter:
        return 0
    if shorter == 1:
        matching_query_tokens = [
            token for index, token in enumerate(query_tokens)
            if index in matched_query
        ]
        distinctive = any(token_frequency[token] <= 1 for token in matching_query_tokens)
        if not distinctive and max(_ratio(query, variant), _ratio(
            " ".join(sorted(query_tokens)),
            " ".join(sorted(variant_tokens)),
        )) < 0.75:
            return 0

    precision = matched / len(query_tokens)
    recall = matched / len(variant_tokens)
    token_f1 = 2 * precision * recall / (precision + recall)
    ordered = _ratio(query, variant)
    orderless = _ratio(
        " ".join(sorted(query_tokens)),
        " ".join(sorted(variant_tokens)),
    )
    score = round((0.15 * ordered + 0.45 * orderless + 0.40 * token_f1) * 10_000)
    return score if score >= 5_800 else 0


def _score(left: str, right: str) -> int:
    return round(_ratio(left, right) * 10_000)


def resolve_product(
    text: str,
    operation_type: WarehouseOperationType,
    products: tuple[ProductOption, ...],
    *,
    limit: int = 5,
) -> Resolution:
    search_normalized = _search_normalize(text)
    required_operation = (
        "product_writeoff"
        if operation_type is WarehouseOperationType.WRITEOFF
        else operation_type.value
    )
    eligible = tuple(
        product for product in products if required_operation in product.operation_types
    )
    official = [
        p for p in eligible
        if _search_normalize(p.normalized_name) == search_normalized
    ]
    if len(official) == 1:
        return Resolution(official[0].id, (), None)
    aliases = [
        product
        for product in eligible
        if search_normalized in {
            _search_normalize(alias) for alias in product.aliases
        }
    ]
    if len(aliases) == 1:
        return Resolution(aliases[0].id, (), None)

    variants_by_product = {
        product.id: tuple(dict.fromkeys(
            (_search_normalize(product.normalized_name),)
            + tuple(_search_normalize(alias) for alias in product.aliases)
        ))
        for product in eligible
    }
    token_frequency: Counter[str] = Counter()
    for variants in variants_by_product.values():
        token_frequency.update({token for variant in variants for token in variant.split()})

    ranked: list[tuple[int, str, int]] = []
    for product in eligible:
        score = max(
            _product_score(search_normalized, variant, token_frequency)
            for variant in variants_by_product[product.id]
        )
        if score:
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
    def variants(location: LocationOption) -> frozenset[str]:
        base = location.normalized_name
        inflected = (
            base[:-1] + ("ю" if base.endswith("я") else "у")
            if base.endswith(("а", "я"))
            else base
        )
        return frozenset({base, inflected})

    exact = [location for location in eligible if normalized in variants(location)]
    if len(exact) == 1:
        if exact[0].id == source_location_id:
            return Resolution(None, (), "same_source_destination")
        return Resolution(exact[0].id, (), None)
    ranked = sorted(
        (
            (
                location.id,
                location.name,
                max(_score(normalized, variant) for variant in variants(location)),
            )
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
