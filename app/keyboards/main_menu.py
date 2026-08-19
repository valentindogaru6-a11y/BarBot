"""Inline keyboards for top-level navigation."""

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.keyboards.callbacks import MainCallback, SearchCallback
from app.keyboards.admin_callbacks import AdminPanelCallback


def build_main_menu_keyboard(
    is_admin: bool = False,
) -> InlineKeyboardMarkup:
    """Build the static top-level menu without catalog business logic."""

    builder = InlineKeyboardBuilder()
    builder.button(
        text="📚 Каталог",
        callback_data=MainCallback(action="catalog"),
    )
    builder.button(
        text="🔍 Поиск",
        callback_data=MainCallback(action="search"),
    )
    if is_admin:
        builder.button(
            text="🛠 Управление каталогом",
            callback_data=AdminPanelCallback(action="o"),
        )
    builder.adjust(1)
    return builder.as_markup()


def build_search_prompt_keyboard() -> InlineKeyboardMarkup:
    """Build a keyboard for cancelling an active search."""

    builder = InlineKeyboardBuilder()
    builder.button(
        text="Отмена",
        callback_data=SearchCallback(action="cancel"),
    )
    return builder.as_markup()


def build_search_retry_keyboard() -> InlineKeyboardMarkup:
    """Build controls shown after an unsuccessful search."""

    builder = InlineKeyboardBuilder()
    builder.button(
        text="🔍 Повторить поиск",
        callback_data=SearchCallback(action="retry"),
    )
    builder.button(
        text="⬅️ Главное меню",
        callback_data=MainCallback(action="home"),
    )
    builder.adjust(1)
    return builder.as_markup()


def build_voice_retry_keyboard() -> InlineKeyboardMarkup:
    """Build recovery controls after an uncertain voice request."""

    builder = InlineKeyboardBuilder()
    builder.button(
        text="🎙 Повторить голосом",
        callback_data=SearchCallback(action="voice_retry"),
    )
    builder.button(
        text="🔍 Обычный поиск",
        callback_data=MainCallback(action="search"),
    )
    builder.button(
        text="⬅️ Главное меню",
        callback_data=MainCallback(action="home"),
    )
    builder.adjust(1)
    return builder.as_markup()
