"""Common administrator keyboards."""

from aiogram.types import (
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.keyboards.admin_callbacks import (
    AdminCancelCallback,
    AdminConfirmCallback,
    AdminEntityCallback,
    AdminPanelCallback,
)
from app.keyboards.callbacks import MainCallback
from app.keyboards.user_callbacks import UserAdminCallback


def build_admin_panel_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for text, entity in (
        ("📁 Разделы", "c"),
        ("🗂 Подразделы", "s"),
        ("📇 Позиции", "i"),
        ("➕ Быстро добавить позицию", "q"),
    ):
        builder.button(
            text=text,
            callback_data=AdminEntityCallback(entity=entity),
        )
    builder.button(
        text="👥 Управление пользователями",
        callback_data=UserAdminCallback(action="menu"),
    )
    builder.button(
        text="🏠 Главное меню",
        callback_data=MainCallback(action="home"),
    )
    builder.adjust(1)
    return builder.as_markup()


def build_admin_cancel_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="❌ Отмена",
        callback_data=AdminCancelCallback(),
    )
    return builder.as_markup()


def build_admin_wizard_keyboard(
    allow_back: bool = True,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if allow_back:
        builder.button(
            text="⬅️ Назад",
            callback_data=AdminCancelCallback(action="b"),
        )
    builder.button(
        text="❌ Отмена",
        callback_data=AdminCancelCallback(action="x"),
    )
    builder.adjust(2)
    return builder.as_markup()


def build_admin_confirm_keyboard(
    *,
    action: str,
    category_id: str = "_",
    subcategory_id: str = "_",
    item_id: str = "_",
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="✅ Подтвердить",
            callback_data=AdminConfirmCallback(
                action=action,
                category_id=category_id,
                subcategory_id=subcategory_id,
                item_id=item_id,
            ).pack(),
        ),
        InlineKeyboardButton(
            text="❌ Отмена",
            callback_data=AdminCancelCallback().pack(),
        ),
    )
    return builder.as_markup()


def build_admin_back_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="⬅️ В панель",
        callback_data=AdminPanelCallback(action="o"),
    )
    return builder.as_markup()


def build_admin_item_missing_keyboard() -> InlineKeyboardMarkup:
    """Offer safe navigation when an item changed before editor opening."""

    builder = InlineKeyboardBuilder()
    builder.button(
        text="📚 Вернуться в каталог",
        callback_data=MainCallback(action="catalog"),
    )
    builder.button(
        text="🛠 Админ-панель",
        callback_data=AdminPanelCallback(action="o"),
    )
    builder.adjust(1)
    return builder.as_markup()


def build_admin_text_input_keyboard(text: str) -> InlineKeyboardMarkup:
    """Build copy/cancel controls without exceeding Telegram copy limits."""

    builder = InlineKeyboardBuilder()
    if 1 <= len(text) <= 256:
        builder.button(
            text="📋 Скопировать текст",
            copy_text=CopyTextButton(text=text),
        )
    builder.button(
        text="❌ Отмена",
        callback_data=AdminCancelCallback(action="x"),
    )
    builder.adjust(1)
    return builder.as_markup()


def build_admin_template_error_keyboard() -> InlineKeyboardMarkup:
    """Offer a retry that preserves the submitted template in FSM data."""

    builder = InlineKeyboardBuilder()
    builder.button(
        text="🔄 Исправить шаблон",
        callback_data=AdminCancelCallback(action="r"),
    )
    builder.button(
        text="❌ Отмена",
        callback_data=AdminCancelCallback(action="x"),
    )
    builder.adjust(1)
    return builder.as_markup()
