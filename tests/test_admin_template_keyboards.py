"""Tests for template editing controls without Telegram API calls."""

from app.keyboards.admin import build_admin_text_input_keyboard
from app.keyboards.admin_callbacks import AdminItemCallback
from app.keyboards.admin_catalog import (
    build_admin_item_creation_modes,
    build_admin_item_actions,
)
from app.models.catalog_item import CatalogItem


def _buttons(markup) -> list:
    return [button for row in markup.inline_keyboard for button in row]


def test_copy_button_contains_exact_text_up_to_256_characters() -> None:
    current = "я" * 256

    buttons = _buttons(build_admin_text_input_keyboard(current))
    copy = next(button for button in buttons if button.copy_text is not None)

    assert copy.text == "📋 Скопировать текст"
    assert copy.copy_text.text == current


def test_copy_button_is_hidden_at_257_characters() -> None:
    buttons = _buttons(build_admin_text_input_keyboard("я" * 257))

    assert not any(button.copy_text is not None for button in buttons)
    assert any(button.text == "❌ Отмена" for button in buttons)


def test_item_editor_keeps_fields_and_adds_full_template_action() -> None:
    markup = build_admin_item_actions(
        "cocktails",
        "classic",
        CatalogItem(id="negroni", name="Negroni"),
    )
    buttons = _buttons(markup)

    assert "📝 Редактировать карточку целиком" in {
        button.text for button in buttons
    }
    assert "✏️ Название" in {button.text for button in buttons}


def test_creation_mode_keyboard_offers_both_modes() -> None:
    buttons = _buttons(build_admin_item_creation_modes("coffee", None))

    by_text = {button.text: button for button in buttons}
    assert "🧩 Пошаговое создание" in by_text
    assert "📝 Создать по шаблону" in by_text
    assert AdminItemCallback.unpack(
        by_text["🧩 Пошаговое создание"].callback_data
    ).action == "w"
    assert AdminItemCallback.unpack(
        by_text["📝 Создать по шаблону"].callback_data
    ).action == "k"

