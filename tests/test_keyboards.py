"""Tests for catalog keyboard builders and pagination."""

from app.keyboards.admin_callbacks import AdminItemCallback
from app.keyboards.callbacks import (
    CategoryCallback,
    ItemCallback,
    MainCallback,
    PaginationCallback,
    SubcategoryCallback,
)
from app.keyboards.catalog import (
    ITEMS_SINGLE_PAGE_LIMIT,
    PAGE_SIZE,
    build_categories_keyboard,
    build_item_keyboard,
    build_items_keyboard,
    build_subcategories_keyboard,
    paginate,
)
from app.models.catalog import Catalog
from app.models.catalog_item import CatalogItem
from app.models.category import Category
from app.models.subcategory import Subcategory
from app.services.catalog_service import CatalogService


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


def test_subcategories_keyboard_uses_stable_navigation_ids() -> None:
    category = Category(id="cocktails", name="Коктейли")
    subcategories = [
        Subcategory(id="signature", name="Фирменные", emoji="⭐"),
        Subcategory(id="classic", name="Классические", emoji="🍸"),
    ]

    markup = build_subcategories_keyboard(category, subcategories)
    buttons = [
        button
        for button in _buttons(markup)
        if button.callback_data
        and button.callback_data.startswith("u:")
    ]

    assert [button.text for button in buttons] == [
        "⭐ Фирменные",
        "🍸 Классические",
    ]
    assert [
        SubcategoryCallback.unpack(button.callback_data).subcategory_id
        for button in buttons
    ] == ["signature", "classic"]


def test_item_list_returns_to_subcategories() -> None:
    category = Category(id="cocktails", name="Коктейли")
    subcategory = Subcategory(id="classic", name="Классические")
    item = CatalogItem(id="negroni", name="Negroni")

    markup = build_items_keyboard(
        category,
        [item],
        subcategory=subcategory,
    )
    buttons = _buttons(markup)
    back_button = next(
        button for button in buttons if button.text == "⬅️ К подкатегориям"
    )
    item_button = next(
        button
        for button in buttons
        if button.callback_data and button.callback_data.startswith("i:")
    )

    assert CategoryCallback.unpack(back_button.callback_data).category_id == "cocktails"
    callback = ItemCallback.unpack(item_button.callback_data)
    assert callback.subcategory_id == "classic"
    assert callback.item_id == "negroni"


def test_item_card_navigation_returns_to_list_and_home() -> None:
    markup = build_item_keyboard("cocktails", "classic")
    buttons = _buttons(markup)

    list_button = next(button for button in buttons if button.text == "⬅️ К списку")
    home_button = next(
        button for button in buttons if button.text == "🏠 Главное меню"
    )

    list_callback = SubcategoryCallback.unpack(list_button.callback_data)
    assert list_callback.category_id == "cocktails"
    assert list_callback.subcategory_id == "classic"
    assert MainCallback.unpack(home_button.callback_data).action == "home"


def test_regular_user_item_card_has_no_edit_button() -> None:
    markup = build_item_keyboard(
        "cocktails",
        "classic",
        item_id="negroni",
    )

    assert "✏️ Редактировать карточку" not in {
        button.text for button in _buttons(markup)
    }


def test_admin_item_card_has_compact_existing_editor_callback() -> None:
    markup = build_item_keyboard(
        "a" * 18,
        "b" * 18,
        item_id="c" * 18,
        is_admin=True,
    )
    edit_button = next(
        button
        for button in _buttons(markup)
        if button.text == "✏️ Редактировать карточку"
    )
    callback = AdminItemCallback.unpack(edit_button.callback_data)

    assert callback.action == "o"
    assert callback.category_id == "a" * 18
    assert callback.subcategory_id == "b" * 18
    assert callback.item_id == "c" * 18
    assert len(edit_button.callback_data.encode()) <= 64


def test_admin_direct_item_card_uses_category_parent() -> None:
    markup = build_item_keyboard(
        "coffee",
        item_id="flat-white",
        is_admin=True,
    )
    edit_button = next(
        button
        for button in _buttons(markup)
        if button.text == "✏️ Редактировать карточку"
    )
    callback = AdminItemCallback.unpack(edit_button.callback_data)

    assert callback.category_id == "coffee"
    assert callback.subcategory_id == "_"
    assert callback.item_id == "flat-white"


def test_fifteen_subcategory_items_have_no_pagination() -> None:
    category = Category(id="cocktails", name="Коктейли")
    subcategory = Subcategory(id="classic", name="Классические")
    items = [
        CatalogItem(id=f"item-{number:02}", name=f"Позиция {number:02}")
        for number in range(15)
    ]

    markup = build_items_keyboard(
        category,
        items,
        subcategory=subcategory,
    )
    buttons = _buttons(markup)
    item_buttons = [
        button
        for button in buttons
        if button.callback_data
        and button.callback_data.startswith("i:")
    ]

    assert [button.text for button in item_buttons] == [
        item.name for item in items
    ]
    assert not any(
        button.callback_data
        and button.callback_data.startswith("p:items:")
        for button in buttons
    )
    assert not any(button.text == "1/2" for button in buttons)
    assert not any(button.text in {"◀️", "▶️"} for button in buttons)


def test_direct_category_items_are_sorted_filtered_and_not_paginated() -> None:
    catalog = Catalog.model_validate(
        {
            "version": 1,
            "categories": [
                {
                    "id": "coffee",
                    "name": "Кофе",
                    "items": [
                        {
                            "id": "third",
                            "name": "Третий",
                            "sort_order": 30,
                        },
                        {
                            "id": "hidden",
                            "name": "Скрытый",
                            "sort_order": 0,
                            "is_active": False,
                        },
                        {
                            "id": "first",
                            "name": "Первый",
                            "sort_order": 10,
                        },
                        {
                            "id": "second",
                            "name": "Второй",
                            "sort_order": 20,
                        },
                    ],
                }
            ],
        }
    )
    service = CatalogService(catalog)
    category = service.get_category("coffee")
    assert category is not None

    markup = build_items_keyboard(
        category,
        service.get_active_items(category.id),
    )
    buttons = _buttons(markup)
    item_buttons = [
        button
        for button in buttons
        if button.callback_data
        and button.callback_data.startswith("i:")
    ]

    assert [button.text for button in item_buttons] == [
        "Первый",
        "Второй",
        "Третий",
    ]
    assert "Скрытый" not in {button.text for button in buttons}
    assert not any(
        button.callback_data
        and button.callback_data.startswith("p:items:")
        for button in buttons
    )


def test_more_than_ninety_items_use_safe_pagination_fallback() -> None:
    category = Category(id="coffee", name="Кофе")
    items = [
        CatalogItem(id=f"item-{number:03}", name=f"Позиция {number:03}")
        for number in range(ITEMS_SINGLE_PAGE_LIMIT + 1)
    ]

    markup = build_items_keyboard(category, items)
    buttons = _buttons(markup)
    pagination_buttons = [
        button
        for button in buttons
        if button.callback_data
        and button.callback_data.startswith("p:items:")
    ]

    assert len(buttons) <= 100
    assert pagination_buttons
    assert all(
        len(button.callback_data.encode()) <= 64
        for button in buttons
        if button.callback_data
    )
    assert any(
        PaginationCallback.unpack(button.callback_data).page == 1
        for button in pagination_buttons
    )
