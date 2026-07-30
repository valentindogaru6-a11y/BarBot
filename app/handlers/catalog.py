"""Telegram handlers for dynamic catalog navigation."""

import logging
from pathlib import Path

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message

from app.handlers.common import (
    catalog_error_message,
    edit_or_send_text,
    load_catalog,
)
from app.keyboards.callbacks import (
    CategoryCallback,
    ItemCallback,
    MainCallback,
    PaginationCallback,
)
from app.keyboards.catalog import (
    build_categories_keyboard,
    build_item_keyboard,
    build_items_keyboard,
)
from app.keyboards.main_menu import build_main_menu_keyboard
from app.services.catalog_service import CatalogService
from app.storage.json_storage import CatalogStorageError
from app.utils.formatting import format_category_heading, format_item_card

router = Router(name=__name__)
logger = logging.getLogger(__name__)

TELEGRAM_CAPTION_LIMIT = 1024


async def _show_catalog(
    message: Message,
    catalog_path: Path,
    page: int = 0,
) -> None:
    try:
        service = CatalogService(load_catalog(catalog_path))
    except CatalogStorageError as exc:
        logger.error("Could not load catalog from %s: %s", catalog_path, exc)
        await edit_or_send_text(
            message,
            catalog_error_message(exc),
            build_main_menu_keyboard(),
        )
        return

    categories = service.get_active_categories()
    text = "<b>Каталог</b>\n\nВыберите категорию:"
    if not categories:
        text = "<b>Каталог</b>\n\nАктивных категорий пока нет."
    await edit_or_send_text(
        message,
        text,
        build_categories_keyboard(categories, page),
    )


async def _show_category(
    message: Message,
    catalog_path: Path,
    category_id: str,
    page: int = 0,
) -> None:
    try:
        service = CatalogService(load_catalog(catalog_path))
    except CatalogStorageError as exc:
        logger.error("Could not load catalog from %s: %s", catalog_path, exc)
        await edit_or_send_text(
            message,
            catalog_error_message(exc),
            build_main_menu_keyboard(),
        )
        return

    category = service.get_category(category_id)
    if category is None or not category.is_active:
        await edit_or_send_text(
            message,
            "Категория не найдена или больше недоступна.",
            build_categories_keyboard(service.get_active_categories()),
        )
        return

    items = service.get_active_items(category.id)
    text = format_category_heading(category)
    text += "\n\nВыберите позицию:"
    if not items:
        text += "\nВ этой категории пока нет активных позиций."
    await edit_or_send_text(
        message,
        text,
        build_items_keyboard(category, items, page),
    )


async def _show_item(
    message: Message,
    catalog_path: Path,
    category_id: str,
    item_id: str,
) -> None:
    try:
        service = CatalogService(load_catalog(catalog_path))
    except CatalogStorageError as exc:
        logger.error("Could not load catalog from %s: %s", catalog_path, exc)
        await edit_or_send_text(
            message,
            catalog_error_message(exc),
            build_main_menu_keyboard(),
        )
        return

    category = service.get_category(category_id)
    item = service.get_item(category_id, item_id)
    if (
        category is None
        or not category.is_active
        or item is None
        or not item.is_active
    ):
        await edit_or_send_text(
            message,
            "Позиция не найдена или больше недоступна.",
            build_main_menu_keyboard(),
        )
        return

    card = format_item_card(item)
    keyboard = build_item_keyboard(category.id)
    if not item.photo_file_id:
        await edit_or_send_text(message, card, keyboard)
        return

    try:
        if len(card) <= TELEGRAM_CAPTION_LIMIT:
            await message.answer_photo(
                photo=item.photo_file_id,
                caption=card,
                reply_markup=keyboard,
            )
        else:
            await message.answer_photo(photo=item.photo_file_id)
            await message.answer(card, reply_markup=keyboard)
    except TelegramAPIError as exc:
        logger.warning(
            "Could not send item photo for category=%s item=%s; "
            "falling back to text: %s",
            category.id,
            item.id,
            exc,
        )
        await edit_or_send_text(message, card, keyboard)


@router.callback_query(MainCallback.filter(F.action == "catalog"))
async def handle_catalog(
    callback: CallbackQuery,
    catalog_path: Path,
) -> None:
    """Show the first page of active categories."""

    await callback.answer()
    if isinstance(callback.message, Message):
        await _show_catalog(callback.message, catalog_path)


@router.callback_query(
    PaginationCallback.filter(F.section == "categories")
)
async def handle_category_page(
    callback: CallbackQuery,
    callback_data: PaginationCallback,
    catalog_path: Path,
) -> None:
    """Show a safely bounded category page."""

    await callback.answer()
    if isinstance(callback.message, Message):
        await _show_catalog(
            callback.message,
            catalog_path,
            callback_data.page,
        )


@router.callback_query(CategoryCallback.filter())
async def handle_category(
    callback: CallbackQuery,
    callback_data: CategoryCallback,
    catalog_path: Path,
) -> None:
    """Show active items for a selected category."""

    await callback.answer()
    if isinstance(callback.message, Message):
        await _show_category(
            callback.message,
            catalog_path,
            callback_data.category_id,
        )


@router.callback_query(PaginationCallback.filter(F.section == "items"))
async def handle_item_page(
    callback: CallbackQuery,
    callback_data: PaginationCallback,
    catalog_path: Path,
) -> None:
    """Show a safely bounded item page."""

    await callback.answer()
    if isinstance(callback.message, Message):
        await _show_category(
            callback.message,
            catalog_path,
            callback_data.category_id,
            callback_data.page,
        )


@router.callback_query(ItemCallback.filter())
async def handle_item(
    callback: CallbackQuery,
    callback_data: ItemCallback,
    catalog_path: Path,
) -> None:
    """Show a universal item card."""

    await callback.answer()
    if isinstance(callback.message, Message):
        await _show_item(
            callback.message,
            catalog_path,
            callback_data.category_id,
            callback_data.item_id,
        )
