"""Tests for structured administrator text input."""

import pytest

from app.services.admin_service import (
    AdminInputError,
    parse_fields,
    parse_ingredients,
    parse_steps,
    parse_tags,
)


def test_parse_ingredients_replaces_full_list() -> None:
    ingredients = parse_ingredients(
        "Джин Finsbury | 45 | мл\n"
        "Rancho Lime | 20,5 | мл\n"
        "Easy Foam | 1 | dash"
    )

    assert [item.name for item in ingredients] == [
        "Джин Finsbury",
        "Rancho Lime",
        "Easy Foam",
    ]
    assert ingredients[1].amount == 20.5


def test_parse_ingredients_reports_all_bad_line_numbers() -> None:
    with pytest.raises(AdminInputError) as error:
        parse_ingredients("Нет разделителей\n| 20 | мл\nA | B")

    assert "Строка 1" in str(error.value)
    assert "Строка 2" in str(error.value)
    assert "Строка 3" in str(error.value)


def test_parse_fields() -> None:
    assert parse_fields(
        "glassware | Купе\nice | Без льда"
    ) == {"glassware": "Купе", "ice": "Без льда"}


@pytest.mark.parametrize(
    "text",
    [
        "Bad-Key | value",
        "glassware |",
        "ice | one\nice | two",
    ],
)
def test_parse_fields_rejects_invalid_input(text: str) -> None:
    with pytest.raises(AdminInputError):
        parse_fields(text)


def test_parse_steps_and_clear() -> None:
    assert parse_steps("Смешать\n\nПодать") == ["Смешать", "Подать"]
    assert parse_steps("-") == []


def test_parse_tags_trims_and_removes_casefold_duplicates() -> None:
    assert parse_tags(" джин, Цитрус, ДЖИН, , цитрус ") == [
        "джин",
        "Цитрус",
    ]
    assert parse_tags("-") == []
