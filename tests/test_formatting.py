"""Tests for safe universal catalog formatting."""

from app.models.catalog_item import CatalogItem
from app.utils.formatting import format_item_card, humanize_field_name


def test_formats_complete_item_card() -> None:
    item = CatalogItem.model_validate(
        {
            "id": "complete-item",
            "name": "Тестовый напиток",
            "short_description": "Короткое описание",
            "description": "Полное описание",
            "ingredients": [
                {"name": "Вода", "amount": 100, "unit": "мл"}
            ],
            "fields": {"glassware": "Хайбол"},
            "steps": ["Смешать", "Подать"],
            "tags": ["тест", "безалкогольный"],
        }
    )

    card = format_item_card(item)

    assert "<b>Тестовый напиток</b>" in card
    assert "• Вода — 100 мл" in card
    assert "• Бокал: Хайбол" in card
    assert "1. Смешать" in card
    assert "<b>Теги:</b> тест, безалкогольный" in card


def test_omits_empty_sections() -> None:
    card = format_item_card(CatalogItem(id="empty-item", name="Пустая"))

    assert card == "<b>Пустая</b>"
    assert "Состав:" not in card
    assert "Характеристики:" not in card
    assert "Приготовление:" not in card
    assert "Теги:" not in card


def test_escapes_all_user_managed_html() -> None:
    item = CatalogItem.model_validate(
        {
            "id": "unsafe-item",
            "name": "<Название>",
            "description": "<script>test</script>",
            "ingredients": [
                {"name": "A&B", "amount": "<40>", "unit": "мл"}
            ],
            "fields": {"custom_<field>": "<value>"},
            "steps": ["<b>Не HTML</b>"],
            "tags": ["<tag>"],
        }
    )

    card = format_item_card(item)

    assert "<Название>" not in card
    assert "&lt;Название&gt;" in card
    assert "&lt;script&gt;test&lt;/script&gt;" in card
    assert "A&amp;B" in card
    assert "&lt;40&gt;" in card
    assert "&lt;value&gt;" in card
    assert "&lt;b&gt;Не HTML&lt;/b&gt;" in card
    assert "&lt;tag&gt;" in card


def test_formats_known_and_unknown_fields_universally() -> None:
    item = CatalogItem(
        id="fields-item",
        name="Поля",
        fields={
            "serving_temperature": "16–18 °C",
            "tech_card_number": "00720",
            "tech_card_date": "07.12.2023",
            "yield": "190 мл",
            "roast_profile": "Светлая",
            "is_decaf": False,
            "details": {"origin": "Эфиопия"},
        },
    )

    card = format_item_card(item)

    assert "• Температура подачи: 16–18 °C" in card
    assert "• № техкарты: 00720" in card
    assert "• Дата техкарты: 07.12.2023" in card
    assert "• Выход: 190 мл" in card
    assert "• Roast profile: Светлая" in card
    assert "• Is decaf: Нет" in card
    assert '• Details: {&quot;origin&quot;: &quot;Эфиопия&quot;}' in card
    assert humanize_field_name("coffee_weight") == "Вес кофе"
