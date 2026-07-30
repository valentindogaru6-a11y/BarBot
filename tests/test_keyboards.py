"""Tests for catalog keyboard builders and pagination."""

from app.keyboards.callbacks import CategoryCallback, ItemCallback
from app.keyboards.catalog import (
    PAGE_SIZE,
    build_categories_keyboard,
    build_items_keyboard,
    paginate,
)
from app.models.catalog_item import CatalogItem
from app.models.category import Category


def _buttons(markup) -> list:
    return [button for row in markup.inline_keyboard for button in row]


def test_pagination_uses_eight_items_per_page() -> None:
    page_items, page, total_pages = paginate(list(range(20)), page=1)

    assert PAGE_SIZE == 8
    assert page_items == list(range(8, 16))
    assert page == 1
    assert total_pages == 3


def test_pagination_clamps_page_below_zero() -> None:
    page_items, page, total_pages = paginate(list(range(10)), page=-100)

    assert page_items == list(range(8))
    assert page == 0
    assert total_pages == 2


def test_pagination_clamps_page_above_last() -> None:
    page_items, page, total_pages = paginate(list(range(10)), page=100)

    assert page_items == [8, 9]
    assert page == 1
    assert total_pages == 2


def test_categories_keyboard_is_built_from_category_data() -> None:
    categories = [
        Category(id="coffee", name="Кофе", emoji="☕"),
        Category(id="cocktails", name="Коктейли"),
    ]

    markup = build_categories_keyboard(categories)
    category_buttons = [
        button
        for button in _buttons(markup)
        if button.callback_data
        and button.callback_data.startswith("c:")
    ]

    assert [button.text for button in category_buttons] == [
        "☕ Кофе",
        "Коктейли",
    ]
    assert [
        CategoryCallback.unpack(button.callback_data).category_id
        for button in category_buttons
    ] == ["coffee", "cocktails"]


def test_items_keyboard_uses_stable_ids() -> None:
    category = Category(id="coffee", name="Кофе")
    items = [
        CatalogItem(id="flat-white", name="Флэт уайт"),
        CatalogItem(id="filter-coffee", name="Фильтр"),
    ]

    markup = build_items_keyboard(category, items)
    item_buttons = [
        button
        for button in _buttons(markup)
        if button.callback_data
        and button.callback_data.startswith("i:")
    ]

    assert [button.text for button in item_buttons] == ["Флэт уайт", "Фильтр"]
    assert [
        ItemCallback.unpack(button.callback_data).item_id
        for button in item_buttons
    ] == ["flat-white", "filter-coffee"]


def test_empty_catalog_keyboard_has_no_category_buttons() -> None:
    markup = build_categories_keyboard([])

    assert not any(
        button.callback_data and button.callback_data.startswith("c:")
        for button in _buttons(markup)
    )
    assert any(button.text == "⬅️ Главное меню" for button in _buttons(markup))
