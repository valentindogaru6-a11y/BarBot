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
    SubcategoryCallback,
)
from app.models.catalog_item import CatalogItem
from app.models.category import Category
from app.models.subcategory import Subcategory
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
    subcategory_id: str = "_",
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
                    subcategory_id=subcategory_id,
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
                subcategory_id=subcategory_id,
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
                    subcategory_id=subcategory_id,
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


def build_subcategories_keyboard(
    category: Category,
    subcategories: Sequence[Subcategory],
    page: int = 0,
) -> InlineKeyboardMarkup:
    """Build one dynamic subcategory page for a category."""

    page_items, safe_page, total_pages = paginate(subcategories, page)
    builder = InlineKeyboardBuilder()
    for subcategory in page_items:
        emoji = f"{subcategory.emoji.strip()} " if subcategory.emoji else ""
        builder.button(
            text=f"{emoji}{subcategory.name}",
            callback_data=SubcategoryCallback(
                category_id=category.id,
                subcategory_id=subcategory.id,
            ),
        )
    builder.adjust(1)
    _add_pagination_row(
        builder,
        section="subcategories",
        page=safe_page,
        total_pages=total_pages,
        category_id=category.id,
    )
    builder.row(
        InlineKeyboardButton(
            text="⬅️ К основным категориям",
            callback_data=MainCallback(action="catalog").pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🏠 Главное меню",
            callback_data=MainCallback(action="home").pack(),
        )
    )
    return builder.as_markup()


def build_items_keyboard(
    category: Category,
    items: Sequence[CatalogItem],
    page: int = 0,
    subcategory: Subcategory | None = None,
) -> InlineKeyboardMarkup:
    """Build one item page for a category or subcategory."""

    page_items, safe_page, total_pages = paginate(items, page)
    subcategory_id = subcategory.id if subcategory else "_"
    builder = InlineKeyboardBuilder()
    for item in page_items:
        builder.button(
            text=item.name,
            callback_data=ItemCallback(
                category_id=category.id,
                item_id=item.id,
                subcategory_id=subcategory_id,
            ),
        )
    builder.adjust(1)
    _add_pagination_row(
        builder,
        section="items",
        page=safe_page,
        total_pages=total_pages,
        category_id=category.id,
        subcategory_id=subcategory_id,
    )
    if subcategory is not None:
        back_text = "⬅️ К подкатегориям"
        back_callback = CategoryCallback(category_id=category.id).pack()
    else:
        back_text = "⬅️ К основным категориям"
        back_callback = MainCallback(action="catalog").pack()
    builder.row(
        InlineKeyboardButton(
            text=back_text,
            callback_data=back_callback,
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🏠 Главное меню",
            callback_data=MainCallback(action="home").pack(),
        )
    )
    return builder.as_markup()


def build_item_keyboard(
    category_id: str,
    subcategory_id: str | None = None,
) -> InlineKeyboardMarkup:
    """Build navigation shown below a regular item card."""

    builder = InlineKeyboardBuilder()
    if subcategory_id is None:
        list_callback = CategoryCallback(category_id=category_id).pack()
    else:
        list_callback = SubcategoryCallback(
            category_id=category_id,
            subcategory_id=subcategory_id,
        ).pack()
    builder.row(
        InlineKeyboardButton(
            text="⬅️ К списку",
            callback_data=list_callback,
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🏠 Главное меню",
            callback_data=MainCallback(action="home").pack(),
        )
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
        section_text = f"{category_prefix}{result.category.name}"
        if result.subcategory is not None:
            subcategory_prefix = (
                f"{result.subcategory.emoji} "
                if result.subcategory.emoji
                else ""
            )
            section_text += (
                f" / {subcategory_prefix}{result.subcategory.name}"
            )
        button_text = f"{section_text} · {result.item.name}"
        builder.button(
            text=button_text[:64],
            callback_data=ItemCallback(
                category_id=result.category.id,
                item_id=result.item.id,
                subcategory_id=(
                    result.subcategory.id if result.subcategory else "_"
                ),
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
