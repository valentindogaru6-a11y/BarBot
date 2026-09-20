"""Exact warehouse quantity parsing and formatting without binary floats."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.models.warehouse.nomenclature import BaseUnit

SCALE = 1_000_000
MAX_QUANTITY_SCALED = 1_000_000_000_000_000
_NUMBER_RE = re.compile(r"^(?:\d+(?:[.,]\d+)?|[.,]\d+)$")

_UNITS: dict[str, tuple[str, Decimal]] = {}
for aliases, dimension, factor in (
    (("кг", "kg", "килограмм", "килограмма", "килограммов"), "mass", Decimal(1)),
    (("г", "гр", "g", "грамм", "грамма", "граммов"), "mass", Decimal("0.001")),
    (("л", "l", "литр", "литра", "литров"), "volume", Decimal(1)),
    (("мл", "ml", "миллилитр", "миллилитра", "миллилитров"), "volume", Decimal("0.001")),
    (("шт", "шт.", "pcs", "штука", "штуки", "штук"), "count", Decimal(1)),
):
    for alias in aliases:
        _UNITS[alias] = (dimension, factor)

_BASE_DIMENSION = {
    BaseUnit.KG: "mass",
    BaseUnit.L: "volume",
    BaseUnit.PCS: "count",
}

_UNIT_CHOICES = {
    BaseUnit.KG: (("кг", "кг"), ("г", "г")),
    BaseUnit.L: (("л", "л"), ("мл", "мл")),
    BaseUnit.PCS: (("шт", "шт"),),
}


class QuantityError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ParsedQuantity:
    quantity_scaled: int
    base_unit: BaseUnit
    input_quantity_text: str
    input_unit_text: str | None
    unit_was_defaulted: bool


def compatible_unit_choices(
    base_unit: BaseUnit | str,
) -> tuple[tuple[str, str], ...]:
    """Return explicit input values and user-facing labels for a base unit."""

    return _UNIT_CHOICES[BaseUnit(base_unit)]


def parse_quantity(
    quantity_text: str,
    unit_text: str | None,
    base_unit: BaseUnit | str,
) -> ParsedQuantity:
    if isinstance(quantity_text, float):
        raise TypeError("float quantities are forbidden")
    base = BaseUnit(base_unit)
    raw_number = str(quantity_text).strip()
    if not _NUMBER_RE.fullmatch(raw_number):
        raise QuantityError("invalid_quantity", "Некорректное количество")
    try:
        value = Decimal(raw_number.replace(",", "."))
    except InvalidOperation as exc:
        raise QuantityError("invalid_quantity", "Некорректное количество") from exc
    if value <= 0:
        raise QuantityError("non_positive_quantity", "Количество должно быть больше нуля")

    normalized_unit = unit_text.casefold().strip() if unit_text else None
    if normalized_unit is None:
        dimension, factor = _BASE_DIMENSION[base], Decimal(1)
    else:
        unit = _UNITS.get(normalized_unit)
        if unit is None:
            raise QuantityError("unsupported_unit", "Неизвестная единица измерения")
        dimension, factor = unit
    if dimension != _BASE_DIMENSION[base]:
        raise QuantityError("incompatible_unit", "Единица несовместима с продуктом")

    scaled_decimal = value * factor * SCALE
    if scaled_decimal != scaled_decimal.to_integral_value():
        raise QuantityError("excess_precision", "Слишком высокая точность количества")
    scaled = int(scaled_decimal)
    if scaled > MAX_QUANTITY_SCALED:
        raise QuantityError("quantity_overflow", "Количество превышает допустимый предел")
    return ParsedQuantity(
        quantity_scaled=scaled,
        base_unit=base,
        input_quantity_text=raw_number,
        input_unit_text=unit_text.strip() if unit_text else None,
        unit_was_defaulted=unit_text is None,
    )


def format_quantity(quantity_scaled: int, base_unit: BaseUnit | str) -> str:
    if isinstance(quantity_scaled, bool) or not isinstance(quantity_scaled, int):
        raise TypeError("scaled quantity must be an integer")
    if quantity_scaled <= 0:
        raise QuantityError("non_positive_quantity", "Количество должно быть больше нуля")
    value = Decimal(quantity_scaled) / SCALE
    rendered = format(value, "f").rstrip("0").rstrip(".")
    return f"{rendered} {BaseUnit(base_unit).value}"
