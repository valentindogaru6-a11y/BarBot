"""Unit tests for Telegram-independent full-card templates."""

import pytest

from app.models.catalog_item import CatalogItem, Ingredient
from app.services.card_template import (
    CardTemplateError,
    empty_item_template,
    parse_item_template,
    serialize_item_template,
)


def _item() -> CatalogItem:
    return CatalogItem(
        id="kiss_me",
        name="Kiss Me",
        short_description="Авторский сауэр",
        description="Полное описание",
        photo_file_id="telegram-file-id",
        ingredients=[
            Ingredient(name="Виски Pouges", amount=45, unit="мл"),
            Ingredient(name="Фреш лимона", amount=15.5, unit="мл"),
        ],
        fields={"glassware": "Рокс", "method": "Dry Shake → Shake"},
        steps=["Перебить", "Процедить", "Подать"],
        tags=["авторский", "виски", "цитрусовый"],
        sort_order=10,
        is_active=False,
    )


def test_full_item_serializes_and_round_trips() -> None:
    item = _item()

    template = serialize_item_template(item)
    parsed = parse_item_template(template)

    assert parsed.name == item.name
    assert parsed.short_description == item.short_description
    assert parsed.description == item.description
    assert parsed.ingredients == item.ingredients
    assert parsed.fields == item.fields
    assert parsed.steps == item.steps
    assert parsed.tags == item.tags
    assert parsed.sort_order == 10
    assert parsed.is_active is False
    assert "telegram-file-id" not in template
    assert "kiss_me" not in template


def test_empty_optional_fields_serialize_as_clear_markers() -> None:
    template = serialize_item_template(CatalogItem(id="water", name="Вода"))
    parsed = parse_item_template(template)

    assert template.count("\n-\n") >= 6
    assert parsed.short_description is None
    assert parsed.description is None
    assert parsed.ingredients == []
    assert parsed.fields == {}
    assert parsed.steps == []
    assert parsed.tags == []


def test_parser_accepts_case_insensitive_headings() -> None:
    template = serialize_item_template(_item()).lower()

    parsed = parse_item_template(template)

    assert parsed.name == "kiss me"
    assert parsed.is_active is False


def test_dash_clears_every_optional_section() -> None:
    template = serialize_item_template(_item())
    for value in (
        "Авторский сауэр",
        "Полное описание",
        "Виски Pouges | 45 | мл\nФреш лимона | 15.5 | мл",
        "glassware | Рокс\nmethod | Dry Shake → Shake",
        "Перебить\nПроцедить\nПодать",
        "авторский, виски, цитрусовый",
    ):
        template = template.replace(value, "-")

    parsed = parse_item_template(template)

    assert parsed.short_description is None
    assert parsed.description is None
    assert not parsed.ingredients
    assert not parsed.fields
    assert not parsed.steps
    assert not parsed.tags


def test_parser_reports_bad_ingredient_with_section_and_line() -> None:
    template = serialize_item_template(_item()).replace(
        "Виски Pouges | 45 | мл\nФреш лимона | 15.5 | мл",
        "Виски | 45 | мл\nсломанная строка",
    )

    with pytest.raises(CardTemplateError) as error:
        parse_item_template(template)

    assert any("СОСТАВ, строка 2" in item for item in error.value.errors)


def test_parser_reports_bad_field_with_section_and_line() -> None:
    template = serialize_item_template(_item()).replace(
        "glassware | Рокс\nmethod | Dry Shake → Shake",
        "glassware | Рокс\nBad-Key | value",
    )

    with pytest.raises(CardTemplateError) as error:
        parse_item_template(template)

    assert any("ХАРАКТЕРИСТИКИ, строка 2" in item for item in error.value.errors)


def test_parser_rejects_unknown_and_duplicate_sections() -> None:
    template = serialize_item_template(_item())
    bad = template + "\n\nНеизвестное: значение\n\nНАЗВАНИЕ:\nДубль"

    with pytest.raises(CardTemplateError) as error:
        parse_item_template(bad)

    assert any("Неизвестное" in item for item in error.value.errors)
    assert any("повторная секция «НАЗВАНИЕ»" in item for item in error.value.errors)


@pytest.mark.parametrize("heading", ["ID", "photo_file_id", "category_id", "subcategory_id"])
def test_parser_rejects_protected_sections(heading: str) -> None:
    template = serialize_item_template(_item()) + f"\n\n{heading}:\nforbidden"

    with pytest.raises(CardTemplateError) as error:
        parse_item_template(template)

    assert any("не разрешена" in item for item in error.value.errors)


def test_parser_reports_invalid_sort_order_and_active_together() -> None:
    template = serialize_item_template(_item())
    template = template.replace("ПОРЯДОК:\n10", "ПОРЯДОК:\nдесять")
    template = template.replace("АКТИВНА:\nнет", "АКТИВНА:\nиногда")

    with pytest.raises(CardTemplateError) as error:
        parse_item_template(template)

    assert "ПОРЯДОК: требуется целое число" in error.value.errors
    assert any(item.startswith("АКТИВНА:") for item in error.value.errors)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("да", True), ("true", True), ("1", True), ("нет", False), ("false", False), ("0", False)],
)
def test_active_accepted_values(value: str, expected: bool) -> None:
    template = serialize_item_template(_item()).replace(
        "АКТИВНА:\nнет", f"АКТИВНА:\n{value}"
    )

    assert parse_item_template(template).is_active is expected


def test_failed_parse_cannot_modify_original_item() -> None:
    item = _item()
    original = item.model_copy(deep=True)
    template = serialize_item_template(item).replace("ПОРЯДОК:\n10", "ПОРЯДОК:\nX")

    with pytest.raises(CardTemplateError):
        parse_item_template(template)

    assert item == original


def test_parsed_update_requires_protected_values_to_build_item() -> None:
    parsed = parse_item_template(serialize_item_template(_item()))

    rebuilt = parsed.build_item(
        item_id="protected-id",
        photo_file_id="protected-photo",
    )

    assert rebuilt.id == "protected-id"
    assert rebuilt.photo_file_id == "protected-photo"


def test_empty_creation_template_has_all_sections_without_protected_data() -> None:
    template = empty_item_template()

    assert all(
        heading in template
        for heading in (
            "НАЗВАНИЕ:",
            "КРАТКОЕ ОПИСАНИЕ:",
            "ПОЛНОЕ ОПИСАНИЕ:",
            "СОСТАВ:",
            "ХАРАКТЕРИСТИКИ:",
            "ПРИГОТОВЛЕНИЕ:",
            "ТЕГИ:",
            "ПОРЯДОК:",
            "АКТИВНА:",
        )
    )
    assert "photo_file_id" not in template
    assert "category_id" not in template

    with pytest.raises(CardTemplateError) as error:
        parse_item_template(template)
    assert error.value.errors == ["НАЗВАНИЕ: значение не может быть пустым"]
