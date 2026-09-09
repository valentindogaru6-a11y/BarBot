import pytest

from app.services.warehouse.quantity_service import (
    MAX_QUANTITY_SCALED,
    QuantityError,
    format_quantity,
    parse_quantity,
)


@pytest.mark.parametrize(
    ("quantity", "unit", "base", "scaled"),
    [
        ("0.15", None, "kg", 150_000),
        ("0,15", "кг", "kg", 150_000),
        ("150", "г", "kg", 150_000),
        ("250", "мл", "l", 250_000),
        ("2", "л", "l", 2_000_000),
        ("1", "шт.", "pcs", 1_000_000),
        ("0.5", "pcs", "pcs", 500_000),
    ],
)
def test_exact_quantity_conversion(quantity, unit, base, scaled) -> None:
    result = parse_quantity(quantity, unit, base)
    assert result.quantity_scaled == scaled
    assert result.unit_was_defaulted is (unit is None)


@pytest.mark.parametrize(
    ("quantity", "unit", "base", "code"),
    [
        ("0", None, "kg", "non_positive_quantity"),
        ("-1", None, "kg", "invalid_quantity"),
        ("1e3", None, "kg", "invalid_quantity"),
        ("1 000", None, "kg", "invalid_quantity"),
        ("0.0000001", None, "kg", "excess_precision"),
        ("1", "мл", "kg", "incompatible_unit"),
        ("1", "dash", "l", "unsupported_unit"),
        ("1", "кулера", "l", "unsupported_unit"),
    ],
)
def test_invalid_quantities(quantity, unit, base, code) -> None:
    with pytest.raises(QuantityError) as caught:
        parse_quantity(quantity, unit, base)
    assert caught.value.code == code


def test_overflow_and_float_are_rejected() -> None:
    with pytest.raises(QuantityError, match="предел"):
        parse_quantity(str(MAX_QUANTITY_SCALED), None, "kg")
    with pytest.raises(TypeError, match="float"):
        parse_quantity(0.5, None, "kg")  # type: ignore[arg-type]


def test_formatting_has_no_binary_drift() -> None:
    assert format_quantity(150_000, "kg") == "0.15 kg"
    assert format_quantity(2_000_001, "l") == "2.000001 l"
