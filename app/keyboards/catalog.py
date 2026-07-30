"""Inline keyboard builders for catalog presentation."""

from collections.abc import Sequence
from math import ceil
from typing import TypeVar

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.keyboards.callbacks import (
    CategoryCallback,
    ItemCallback,
    MainCallback,
    PaginationCallback,
    SearchCallback,
)
from app.models.catalog_item import CatalogItem
from app.models.category import Category
from app.services.search_service import SearchResult

PAGE_SIZE = 8
T = TypeVar("T")


def paginate(
    items: Sequence[T],
    page: int,
    page_size: int = PAGE_SIZE,
) -> tuple[list[T], int, int]:
    """Return one page, a safely clamped page index, and total page count."""

    if page_size <= 0:
        raise ValueError("page_size must be greater than zero")

    total_pages = max(1, ceil(len(items) / page_size))
    safe_page = min(max(page, 0), total_pages - 1)
    start = safe_page * page_size
    return list(items[start : start + page_size]), safe_page, total_pages


def _add_pagination_row(
    builder: InlineKeyboardBuilder,
    *,
    section: str,
    page: int,
    total_pages: int,
    category_id: str = "_",
) -> None:
    buttons: list[InlineKeyboardButton] = []
    if page > 0:
        buttons.append(
            InlineKeyboardButton(
                text="◀️",
                callback_data=PaginationCallback(
                    section=section,
                    page=page - 1,
                    category_id=category_id,
                ).pack(),
            )
        )

    buttons.append(
        InlineKeyboardButton(
            text=f"{page + 1}/{total_pages}",
            callback_data=PaginationCallback(
                section=section,
                page=page,
                category_id=category_id,
            ).pack(),
        )
    )

    if page < total_pages - 1:
        buttons.append(
            InlineKeyboardButton(
                text="▶️",
                callback_data=PaginationCallback(
                    section=section,
                    page=page + 1,
                    category_id=category_id,
                ).pack(),
            )
        )
    builder.row(*buttons)


def build_categories_keyboard(
    categories: Sequence[Category],
    page: int = 0,
) -> InlineKeyboardMarkup:
    """Build one dynamic category page and top-level navigation."""

    page_items, safe_page, total_pages = paginate(categories, page)
    builder = InlineKeyboardBuilder()
    for category in page_items:
        emoji = f"{category.emoji.strip()} " if category.emoji else ""
        builder.button(
            text=f"{emoji}{category.name}",
            callback_data=CategoryCallback(category_id=category.id),
        )
    builder.adjust(1)
    _add_pagination_row(
        builder,
        section="categories",
        page=safe_page,
        total_pages=total_pages,
    )
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Главное меню",
            callback_data=MainCallback(action="home").pack(),
        )
    )
    return builder.as_markup()


def build_items_keyboard(
    category: Category,
    items: Sequence[CatalogItem],
    page: int = 0,
) -> InlineKeyboardMarkup:
    """Build one dynamic item page for a category."""

    page_items, safe_page, total_pages = paginate(items, page)
    builder = InlineKeyboardBuilder()
    for item in page_items:
        builder.button(
            text=item.name,
            callback_data=ItemCallback(
                category_id=category.id,
                item_id=item.id,
            ),
        )
    builder.adjust(1)
    _add_pagination_row(
        builder,
        section="items",
        page=safe_page,
        total_pages=total_pages,
        category_id=category.id,
    )
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Назад к категориям",
            callback_data=MainCallback(action="catalog").pack(),
        )
    )
    return builder.as_markup()


def build_item_keyboard(category_id: str) -> InlineKeyboardMarkup:
    """Build navigation shown below a regular item card."""

    builder = InlineKeyboardBuilder()
    builder.button(
        text="⬅️ Назад к позициям",
        callback_data=CategoryCallback(category_id=category_id),
    )
    return builder.as_markup()


def build_search_results_keyboard(
    results: Sequence[SearchResult],
) -> InlineKeyboardMarkup:
    """Build search-result buttons with category context."""

    builder = InlineKeyboardBuilder()
    for result in results:
        category_prefix = (
            f"{result.category.emoji} " if result.category.emoji else ""
        )
        button_text = (
            f"{category_prefix}{result.category.name} · {result.item.name}"
        )
        builder.button(
            text=button_text[:64],
            callback_data=ItemCallback(
                category_id=result.category.id,
                item_id=result.item.id,
            ),
        )
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(
            text="🔍 Новый поиск",
            callback_data=SearchCallback(action="retry").pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Главное меню",
            callback_data=MainCallback(action="home").pack(),
        )
    )
    return builder.as_markup()
