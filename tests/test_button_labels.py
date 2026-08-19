"""Tests for shared Telegram button-label formatting."""

from pathlib import Path

from app.keyboards.admin_callbacks import AdminItemCallback
from app.keyboards.admin_catalog import build_admin_items_keyboard
from app.keyboards.catalog import (
    build_items_keyboard,
    build_search_results_keyboard,
)
from app.keyboards.user_admin import build_users_list_keyboard
from app.models.catalog_item import CatalogItem
from app.models.category import Category
from app.models.subcategory import Subcategory
from app.models.user import Role, UserRecord
from app.services.catalog_service import CatalogService
from app.services.search_service import SearchResult
from app.storage.json_storage import JsonCatalogStorage
from app.utils.button_labels import (
    MAX_BUTTON_LABEL_LENGTH,
    admin_entity_button_label,
    category_button_label,
    item_button_label,
    item_button_labels,
    search_result_button_labels,
    subcategory_button_label,
    truncate_button_label,
    user_button_label,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _buttons(markup) -> list:
    return [button for row in markup.inline_keyboard for button in row]


def _item_buttons(markup, prefix: str = "i:") -> list:
    return [
        button
        for button in _buttons(markup)
        if button.callback_data and button.callback_data.startswith(prefix)
    ]


def test_short_item_name_remains_complete() -> None:
    assert item_button_label("Манго") == "Манго"


def test_long_item_name_is_only_truncated_for_button_display() -> None:
    item = CatalogItem(
        id="long-item",
        name=(
            "Пюре манго с маракуйей и апельсином для специального "
            "летнего меню"
        ),
    )
    original_name = item.name

    label = item_button_label(item.name)

    assert label.endswith("…")
    assert len(label) <= MAX_BUTTON_LABEL_LENGTH
    assert item.name == original_name


def test_truncation_prefers_a_word_boundary_and_unicode_ellipsis() -> None:
    text = "Пюре манго с маракуйей и апельсином для летнего меню"

    label = truncate_button_label(text, 40)

    assert label == "Пюре манго с маракуйей и апельсином…"
    assert "�" not in label


def test_unicode_label_is_not_truncated_by_encoded_byte_length() -> None:
    text = "🍹 Клубника маракуйя апельсин ананас кокос малина лимон"

    label = truncate_button_label(text, 32)

    assert len(label) <= 32
    assert label.startswith("🍹 Клубника")
    assert label.endswith("…")


def test_category_and_subcategory_labels_have_no_parent_path() -> None:
    assert category_button_label("Полуфабрикаты", "🧪") == (
        "🧪 Полуфабрикаты"
    )
    assert subcategory_button_label("Пюре и фруктовые основы", "🍓") == (
        "🍓 Пюре и фруктовые основы"
    )


def test_item_list_contains_name_only_not_hierarchy_path() -> None:
    category = Category(id="semi", name="Полуфабрикаты", emoji="🧪")
    subcategory = Subcategory(
        id="puree",
        name="Пюре и фруктовые основы",
        emoji="🍓",
    )
    item = CatalogItem(id="mango", name="Манго")

    markup = build_items_keyboard(category, [item], subcategory=subcategory)
    label = _item_buttons(markup)[0].text

    assert label == "Манго"
    assert "Полуфабрикаты" not in label
    assert "Пюре" not in label


def test_search_result_puts_item_name_before_short_context() -> None:
    result = SearchResult(
        category=Category(id="semi", name="Полуфабрикаты"),
        subcategory=Subcategory(
            id="puree",
            name="Пюре и фруктовые заготовки",
        ),
        item=CatalogItem(id="mango", name="Манго"),
    )

    label = search_result_button_labels([result])[0]

    assert label.startswith("Манго — ")
    assert "Полуфабрикаты /" not in label
    assert len(label) <= MAX_BUTTON_LABEL_LENGTH


def test_same_display_names_get_distinguishable_minimal_context() -> None:
    results = [
        SearchResult(
            Category(id="semi", name="Полуфабрикаты"),
            CatalogItem(id="mango-puree", name="Манго"),
            Subcategory(id="puree", name="Пюре"),
        ),
        SearchResult(
            Category(id="coffee", name="Кофейная карта"),
            CatalogItem(id="mango-syrup", name="Манго"),
            Subcategory(id="syrups", name="Сиропы"),
        ),
    ]

    labels = search_result_button_labels(results)

    assert labels == ["Манго — Пюре", "Манго — Сиропы"]


def test_regression_item_difference_precedes_long_hierarchy_context() -> None:
    category = Category(id="semi", name="Полуфабрикаты", emoji="🧪")
    subcategory = Subcategory(
        id="puree",
        name="Пюре и фруктовые заготовки",
        emoji="🍓",
    )
    results = [
        SearchResult(
            category,
            CatalogItem(id="mango", name="Манго"),
            subcategory,
        ),
        SearchResult(
            category,
            CatalogItem(id="passion", name="Маракуйя"),
            subcategory,
        ),
    ]

    labels = search_result_button_labels(results)

    assert labels[0].startswith("Манго")
    assert labels[1].startswith("Маракуйя")
    assert labels[0] != labels[1]
    assert all(len(label) <= MAX_BUTTON_LABEL_LENGTH for label in labels)


def test_long_colliding_names_move_distinguishing_suffix_to_front() -> None:
    names = [
        "Очень длинное название позиции с одинаковым началом и объёмом 150 мл",
        "Очень длинное название позиции с одинаковым началом и объёмом 750 мл",
    ]

    labels = item_button_labels(names)

    assert labels[0].startswith("150 мл · ")
    assert labels[1].startswith("750 мл · ")
    assert len(set(labels)) == 2


def test_colliding_search_names_keep_context_after_disambiguation() -> None:
    category = Category(id="wine", name="Винная карта")
    subcategory = Subcategory(id="white", name="Белые вина")
    results = [
        SearchResult(
            category,
            CatalogItem(
                id="wine-150",
                name="Mango Lou Gat Maison Marcellin Cotes de Gascogne 150 мл",
            ),
            subcategory,
        ),
        SearchResult(
            category,
            CatalogItem(
                id="wine-750",
                name="Mango Lou Gat Maison Marcellin Cotes de Gascogne 750 мл",
            ),
            subcategory,
        ),
    ]

    labels = search_result_button_labels(results)

    assert labels[0].startswith("150 мл · ")
    assert labels[1].startswith("750 мл · ")
    assert all(label.endswith("— Белые вина") for label in labels)


def test_admin_list_uses_shared_formatter_and_keeps_hidden_marker() -> None:
    item = CatalogItem(
        id="hidden-item",
        name="Очень длинное административное название позиции для проверки",
        is_active=False,
    )

    markup = build_admin_items_keyboard("semi", "puree", [item])
    button = _item_buttons(markup, prefix="at:")[0]

    assert button.text == admin_entity_button_label(item.name, False)
    assert button.text.endswith("[скрыто]")
    assert len(button.text) <= MAX_BUTTON_LABEL_LENGTH
    assert AdminItemCallback.unpack(button.callback_data).item_id == item.id


def test_user_label_contains_only_name_and_username() -> None:
    assert user_button_label("Очень Длинное Имя Пользователя", "worker") == (
        "Очень Длинное Имя Пользователя · @worker"
    )


def test_user_list_uses_shared_compact_label_without_id_or_role() -> None:
    user = UserRecord(
        telegram_user_id=123456789,
        username="worker",
        first_name="Мария",
        last_name="Сотрудник",
        role=Role.USER,
        first_seen_at="2026-01-01T00:00:00+00:00",
        last_seen_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )

    markup = build_users_list_keyboard(
        [(user, Role.USER)],
        role=Role.USER.value,
        page=0,
        total_pages=1,
    )
    label = next(
        button.text
        for button in _buttons(markup)
        if button.callback_data and button.callback_data.startswith("uv:")
    )

    assert label == "Мария Сотрудник · @worker"
    assert "123456789" not in label
    assert "User" not in label


def test_real_catalog_item_screens_have_unique_bounded_labels() -> None:
    catalog = JsonCatalogStorage(
        PROJECT_ROOT / "data" / "catalog.json"
    ).load()
    service = CatalogService(catalog)
    checked = 0
    for category in service.get_active_categories():
        locations = [
            (None, service.get_active_items(category.id)),
            *[
                (
                    subcategory,
                    service.get_active_items(category.id, subcategory.id),
                )
                for subcategory in service.get_active_subcategories(category.id)
            ],
        ]
        for subcategory, items in locations:
            labels = item_button_labels([item.name for item in items])
            assert len(labels) == len(set(label.casefold() for label in labels))
            assert all(
                len(label) <= MAX_BUTTON_LABEL_LENGTH for label in labels
            )
            checked += len(items)

    assert checked >= 24


def test_search_keyboard_uses_same_name_first_formatter_as_voice() -> None:
    result = SearchResult(
        Category(id="cocktails", name="Коктейли"),
        CatalogItem(id="pornstar-girl", name="Pornstar Girl"),
        Subcategory(id="signature", name="Фирменные коктейли"),
    )

    markup = build_search_results_keyboard([result])
    label = _item_buttons(markup)[0].text

    assert label == search_result_button_labels([result])[0]
    assert label.startswith("Pornstar Girl — ")
