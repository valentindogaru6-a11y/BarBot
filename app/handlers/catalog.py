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
    SubcategoryCallback,
)
from app.keyboards.catalog import (
    build_categories_keyboard,
    build_item_keyboard,
    build_items_keyboard,
    build_subcategories_keyboard,
)
from app.keyboards.main_menu import build_main_menu_keyboard
from app.services.catalog_service import CatalogService
from app.storage.json_storage import CatalogStorageError
from app.utils.formatting import (
    format_category_heading,
    format_item_card,
    format_subcategory_heading,
)

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

    subcategories = service.get_active_subcategories(category.id)
    text = format_category_heading(category)
    if subcategories:
        text += "\n\nВыберите раздел:"
        keyboard = build_subcategories_keyboard(
            category,
            subcategories,
            page,
        )
    else:
        items = service.get_active_items(category.id)
        text += "\n\nВыберите позицию:"
        if not items:
            text += "\nВ этой категории пока нет активных позиций."
        keyboard = build_items_keyboard(category, items, page)
    await edit_or_send_text(
        message,
        text,
        keyboard,
    )


async def _show_subcategory(
    message: Message,
    catalog_path: Path,
    category_id: str,
    subcategory_id: str,
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
    subcategory = service.get_subcategory(category_id, subcategory_id)
    if (
        category is None
        or not category.is_active
        or subcategory is None
        or not subcategory.is_active
    ):
        await edit_or_send_text(
            message,
            "Подкатегория не найдена или больше недоступна.",
            build_categories_keyboard(service.get_active_categories()),
        )
        return

    items = service.get_active_items(category.id, subcategory.id)
    text = format_subcategory_heading(subcategory)
    text += "\n\nВыберите позицию:"
    if not items:
        text += "\nВ этом разделе пока нет активных позиций."
    await edit_or_send_text(
        message,
        text,
        build_items_keyboard(
            category,
            items,
            page,
            subcategory=subcategory,
        ),
    )


async def _show_item(
    message: Message,
    catalog_path: Path,
    category_id: str,
    item_id: str,
    subcategory_id: str | None = None,
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
    subcategory = (
        service.get_subcategory(category_id, subcategory_id)
        if subcategory_id is not None
        else None
    )
    if subcategory_id is None:
        item = service.get_item(category_id, item_id)
    else:
        item = service.get_subcategory_item(
            category_id,
            subcategory_id,
            item_id,
        )
    if (
        category is None
        or not category.is_active
        or (
            subcategory_id is not None
            and (subcategory is None or not subcategory.is_active)
        )
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
    keyboard = build_item_keyboard(category.id, subcategory_id)
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


@router.callback_query(
    PaginationCallback.filter(F.section == "subcategories")
)
async def handle_subcategory_page(
    callback: CallbackQuery,
    callback_data: PaginationCallback,
    catalog_path: Path,
) -> None:
    """Show a safely bounded subcategory page."""

    await callback.answer()
    if isinstance(callback.message, Message):
        await _show_category(
            callback.message,
            catalog_path,
            callback_data.category_id,
            callback_data.page,
        )


@router.callback_query(SubcategoryCallback.filter())
async def handle_subcategory(
    callback: CallbackQuery,
    callback_data: SubcategoryCallback,
    catalog_path: Path,
) -> None:
    """Show active items for a selected subcategory."""

    await callback.answer()
    if isinstance(callback.message, Message):
        await _show_subcategory(
            callback.message,
            catalog_path,
            callback_data.category_id,
            callback_data.subcategory_id,
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
        if callback_data.subcategory_id == "_":
            await _show_category(
                callback.message,
                catalog_path,
                callback_data.category_id,
                callback_data.page,
            )
        else:
            await _show_subcategory(
                callback.message,
                catalog_path,
                callback_data.category_id,
                callback_data.subcategory_id,
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
            (
                None
                if callback_data.subcategory_id == "_"
                else callback_data.subcategory_id
            ),
        )
