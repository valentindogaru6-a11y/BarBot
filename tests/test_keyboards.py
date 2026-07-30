"""Tests for catalog keyboard builders and pagination."""

from app.keyboards.callbacks import (
    CategoryCallback,
    ItemCallback,
    MainCallback,
    SubcategoryCallback,
)
from app.keyboards.catalog import (
    PAGE_SIZE,
    build_categories_keyboard,
    build_item_keyboard,
    build_items_keyboard,
    build_subcategories_keyboard,
    paginate,
)
from app.models.catalog_item import CatalogItem
from app.models.category import Category
from app.models.subcategory import Subcategory


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
