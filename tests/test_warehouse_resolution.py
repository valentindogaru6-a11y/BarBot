from app.models.warehouse.nomenclature import BaseUnit
from app.models.warehouse.operations import WarehouseOperationType
from app.services.warehouse.resolution_service import (
    LocationOption,
    ProductOption,
    resolve_destination,
    resolve_product,
)


def _product(
    product_id: int,
    name: str,
    *,
    aliases=(),
    operations=frozenset({"product_writeoff", "transfer"}),
) -> ProductOption:
    return ProductOption(
        id=product_id,
        official_name=name,
        normalized_name=name.casefold(),
        base_unit=BaseUnit.KG,
        aliases=aliases,
        operation_types=operations,
    )


def test_official_then_alias_exact_resolution() -> None:
    products = (
        _product(1, "Апельсин"),
        _product(2, "Остаток молока", aliases=("молоко в питчерах",)),
    )
    assert resolve_product(
        "апельсин", WarehouseOperationType.WRITEOFF, products
    ).exact_id == 1
    assert resolve_product(
        "МОЛОКО В ПИТЧЕРАХ", WarehouseOperationType.TRANSFER, products
    ).exact_id == 2


def test_fuzzy_never_auto_selects_and_unknown_has_no_candidates() -> None:
    products = (_product(1, "Апельсин"), _product(2, "Лайм"))
    fuzzy = resolve_product("апелсин", WarehouseOperationType.WRITEOFF, products)
    assert fuzzy.exact_id is None
    assert fuzzy.candidates[0][0] == 1
    unknown = resolve_product(
        "совершенно посторонний объект",
        WarehouseOperationType.WRITEOFF,
        products,
    )
    assert unknown.exact_id is None
    assert unknown.candidates == ()
    assert unknown.error_code == "unknown_product"


def test_operation_type_filters_products() -> None:
    product = _product(1, "Лайм", operations=frozenset({"transfer"}))
    result = resolve_product("лайм", WarehouseOperationType.WRITEOFF, (product,))
    assert result.exact_id is None


def test_product_normalization_case_yo_spacing_and_safe_punctuation() -> None:
    products = (
        _product(1, "Молоко альтернативное"),
        _product(2, "Ёжевичное пюре", aliases=("пюре ёжевики",)),
    )
    assert resolve_product(
        "  МОЛОКО,   АЛЬТЕРНАТИВНОЕ! ",
        WarehouseOperationType.WRITEOFF,
        products,
    ).exact_id == 1
    assert resolve_product(
        "ПЮРЕ ЕЖЕВИКИ", WarehouseOperationType.WRITEOFF, products
    ).exact_id == 2


def test_token_order_typo_extra_and_missing_token_rank_deterministically() -> None:
    products = (
        _product(10, "Молоко альтернативное"),
        _product(11, "Молоко"),
        _product(12, "Остаток молока в питчерах"),
    )
    for query in (
        "альтернативное молоко",
        "молоко альтернасивное",
        "молоко альтернативное овсяное",
        "альтернативное",
    ):
        result = resolve_product(query, WarehouseOperationType.WRITEOFF, products)
        assert result.exact_id is None
        assert result.candidates[0][0] == 10
        assert result.error_code == "ambiguous_product"
    first = resolve_product(
        "альтернативное молоко", WarehouseOperationType.WRITEOFF, products
    ).candidates
    second = resolve_product(
        "альтернативное молоко", WarehouseOperationType.WRITEOFF, products
    ).candidates
    assert first == second


def test_one_common_generic_token_does_not_create_dangerous_candidates() -> None:
    products = (
        _product(1, "Молоко"),
        _product(2, "Молоко альтернативное"),
        _product(3, "Остаток молока в питчерах"),
    )
    result = resolve_product(
        "ванильное молоко", WarehouseOperationType.WRITEOFF, products
    )
    assert result.exact_id is None
    assert result.candidates == ()
    assert result.error_code == "unknown_product"


def test_candidate_order_is_stable_when_scores_are_equal() -> None:
    products = (
        _product(2, "Сироп ягодный Б"),
        _product(1, "Сироп ягодный А"),
    )
    result = resolve_product(
        "ягодный сироп", WarehouseOperationType.WRITEOFF, products
    )
    assert [candidate[1] for candidate in result.candidates] == [
        "Сироп ягодный А", "Сироп ягодный Б"
    ]


def test_destination_exact_fuzzy_and_same_source() -> None:
    locations = (
        LocationOption(1, "Бар",
                       "бар", True, True),
        LocationOption(2, "Кухня", "кухня", False, True),
        LocationOption(3, "Кондитерка", "кондитерка", False, True),
    )
    assert resolve_destination("кухня", locations, source_location_id=1).exact_id == 2
    assert resolve_destination("кондитерка", locations, source_location_id=1).exact_id == 3
    assert resolve_destination(
        "кухн", locations, source_location_id=1
    ).exact_id is None
    assert resolve_destination(
        "бар", locations, source_location_id=1
    ).error_code == "same_source_destination"
