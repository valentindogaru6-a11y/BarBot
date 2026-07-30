"""Administrator catalog-management keyboards."""

from collections.abc import Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.keyboards.admin_callbacks import (
    AdminCategoryCallback,
    AdminItemCallback,
    AdminPaginationCallback,
    AdminPanelCallback,
    AdminSubcategoryCallback,
)
from app.keyboards.catalog import paginate
from app.models.catalog_item import CatalogItem
from app.models.category import Category
from app.models.subcategory import Subcategory


def _label(name: str, is_active: bool, emoji: str | None = None) -> str:
    prefix = f"{emoji} " if emoji else ""
    hidden = "" if is_active else " [скрыто]"
    return f"{prefix}{name}{hidden}"


def _add_filters(
    builder: InlineKeyboardBuilder,
    entity: str,
    *,
    category_id: str = "_",
    subcategory_id: str = "_",
) -> None:
    builder.row(
        *[
            InlineKeyboardButton(
                text=text,
                callback_data=AdminPaginationCallback(
                    entity=entity,
                    filter_mode=mode,
                    page=0,
                    category_id=category_id,
                    subcategory_id=subcategory_id,
                ).pack(),
            )
            for text, mode in (
                ("Все", "a"),
                ("Активные", "v"),
                ("Скрытые", "h"),
            )
        ]
    )


def _add_page_row(
    builder: InlineKeyboardBuilder,
    *,
    entity: str,
    filter_mode: str,
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
                callback_data=AdminPaginationCallback(
                    entity=entity,
                    filter_mode=filter_mode,
                    page=page - 1,
                    category_id=category_id,
                    subcategory_id=subcategory_id,
                ).pack(),
            )
        )
    buttons.append(
        InlineKeyboardButton(
            text=f"{page + 1}/{total_pages}",
            callback_data=AdminPaginationCallback(
                entity=entity,
                filter_mode=filter_mode,
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
                callback_data=AdminPaginationCallback(
                    entity=entity,
                    filter_mode=filter_mode,
                    page=page + 1,
                    category_id=category_id,
                    subcategory_id=subcategory_id,
                ).pack(),
            )
        )
    builder.row(*buttons)


def build_admin_categories_keyboard(
    categories: Sequence[Category],
    page: int = 0,
    filter_mode: str = "a",
) -> InlineKeyboardMarkup:
    page_items, safe_page, total_pages = paginate(categories, page)
    builder = InlineKeyboardBuilder()
    for category in page_items:
        builder.button(
            text=_label(category.name, category.is_active, category.emoji),
            callback_data=AdminCategoryCallback(
                action="o",
                category_id=category.id,
            ),
        )
    builder.adjust(1)
    _add_filters(builder, "c")
    _add_page_row(
        builder,
        entity="c",
        filter_mode=filter_mode,
        page=safe_page,
        total_pages=total_pages,
    )
    builder.row(
        InlineKeyboardButton(
            text="➕ Добавить раздел",
            callback_data=AdminCategoryCallback(action="n").pack(),
        ),
        InlineKeyboardButton(
            text="⬅️ В панель",
            callback_data=AdminPanelCallback(action="o").pack(),
        ),
    )
    return builder.as_markup()


def build_admin_category_actions(category: Category) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for text, action in (
        ("✏️ Название", "en"),
        ("😀 Emoji", "ee"),
        ("📝 Описание", "ed"),
        ("🔢 Порядок", "es"),
        ("👁 Скрыть/показать", "t"),
        ("⚠️ Удалить раздел", "d"),
    ):
        builder.button(
            text=text,
            callback_data=AdminCategoryCallback(
                action=action,
                category_id=category.id,
            ),
        )
    builder.button(
        text="⬅️ К разделам",
        callback_data=AdminPaginationCallback(
            entity="c",
            filter_mode="a",
            page=0,
        ),
    )
    builder.adjust(1)
    return builder.as_markup()


def build_admin_category_picker(
    categories: Sequence[Category],
    target: str,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for category in categories:
        if target == "s":
            callback = AdminSubcategoryCallback(
                action="l",
                category_id=category.id,
            )
        else:
            callback = AdminItemCallback(
                action=(
                    "q"
                    if target == "q"
                    else "mc"
                    if target == "m"
                    else "p"
                ),
                category_id=category.id,
            )
        builder.button(
            text=_label(category.name, category.is_active, category.emoji),
            callback_data=callback,
        )
    builder.button(
        text="⬅️ В панель",
        callback_data=AdminPanelCallback(action="o"),
    )
    builder.adjust(1)
    return builder.as_markup()


def build_admin_subcategories_keyboard(
    category: Category,
    subcategories: Sequence[Subcategory],
    page: int = 0,
    filter_mode: str = "a",
) -> InlineKeyboardMarkup:
    page_items, safe_page, total_pages = paginate(subcategories, page)
    builder = InlineKeyboardBuilder()
    for subcategory in page_items:
        builder.button(
            text=_label(
                subcategory.name,
                subcategory.is_active,
                subcategory.emoji,
            ),
            callback_data=AdminSubcategoryCallback(
                action="o",
                category_id=category.id,
                subcategory_id=subcategory.id,
            ),
        )
    builder.button(
        text="➕ Добавить подраздел",
        callback_data=AdminSubcategoryCallback(
            action="n",
            category_id=category.id,
        ),
    )
    builder.adjust(1)
    _add_filters(builder, "s", category_id=category.id)
    _add_page_row(
        builder,
        entity="s",
        filter_mode=filter_mode,
        page=safe_page,
        total_pages=total_pages,
        category_id=category.id,
    )
    builder.button(
        text="⬅️ К выбору раздела",
        callback_data=AdminPanelCallback(action="s"),
    )
    return builder.as_markup()


def build_admin_subcategory_actions(
    category_id: str,
    subcategory: Subcategory,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for text, action in (
        ("✏️ Название", "en"),
        ("😀 Emoji", "ee"),
        ("📝 Описание", "ed"),
        ("🔢 Порядок", "es"),
        ("👁 Скрыть/показать", "t"),
        ("⚠️ Удалить подраздел", "d"),
    ):
        builder.button(
            text=text,
            callback_data=AdminSubcategoryCallback(
                action=action,
                category_id=category_id,
                subcategory_id=subcategory.id,
            ),
        )
    builder.button(
        text="⬅️ К подразделам",
        callback_data=AdminSubcategoryCallback(
            action="l",
            category_id=category_id,
        ),
    )
    builder.adjust(1)
    return builder.as_markup()


def build_admin_placement_keyboard(
    category: Category,
    *,
    action: str,
    item_id: str = "_",
    include_direct: bool = True,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if include_direct:
        builder.button(
            text="📂 Непосредственно в разделе",
            callback_data=AdminItemCallback(
                action=action,
                category_id=category.id,
                item_id=item_id,
            ),
        )
    for subcategory in category.subcategories:
        builder.button(
            text=_label(
                subcategory.name,
                subcategory.is_active,
                subcategory.emoji,
            ),
            callback_data=AdminItemCallback(
                action=action,
                category_id=category.id,
                subcategory_id=subcategory.id,
                item_id=item_id,
            ),
        )
    builder.button(
        text="⬅️ В панель",
        callback_data=AdminPanelCallback(action="o"),
    )
    builder.adjust(1)
    return builder.as_markup()


def build_admin_items_keyboard(
    category_id: str,
    subcategory_id: str | None,
    items: Sequence[CatalogItem],
    page: int = 0,
    filter_mode: str = "a",
) -> InlineKeyboardMarkup:
    page_items, safe_page, total_pages = paginate(items, page)
    sub_id = subcategory_id or "_"
    builder = InlineKeyboardBuilder()
    for item in page_items:
        builder.button(
            text=_label(item.name, item.is_active),
            callback_data=AdminItemCallback(
                action="o",
                category_id=category_id,
                subcategory_id=sub_id,
                item_id=item.id,
            ),
        )
    builder.adjust(1)
    _add_filters(
        builder,
        "i",
        category_id=category_id,
        subcategory_id=sub_id,
    )
    _add_page_row(
        builder,
        entity="i",
        filter_mode=filter_mode,
        page=safe_page,
        total_pages=total_pages,
        category_id=category_id,
        subcategory_id=sub_id,
    )
    builder.row(
        InlineKeyboardButton(
            text="➕ Добавить позицию",
            callback_data=AdminItemCallback(
                action="n",
                category_id=category_id,
                subcategory_id=sub_id,
            ).pack(),
        ),
        InlineKeyboardButton(
            text="⬅️ В панель",
            callback_data=AdminPanelCallback(action="o").pack(),
        ),
    )
    return builder.as_markup()


def build_admin_item_actions(
    category_id: str,
    subcategory_id: str | None,
    item: CatalogItem,
) -> InlineKeyboardMarkup:
    sub_id = subcategory_id or "_"
    builder = InlineKeyboardBuilder()
    actions = (
        ("✏️ Название", "en"),
        ("📝 Краткое описание", "es"),
        ("📄 Полное описание", "ed"),
        ("🧾 Состав", "ei"),
        ("⚙️ Характеристики", "ef"),
        ("👣 Приготовление", "ep"),
        ("🏷 Теги", "et"),
        ("🖼 Заменить фото", "rp"),
        ("🗑 Удалить фото", "dp"),
        ("👁 Скрыть/показать", "t"),
        ("📁 Переместить", "m"),
        ("⚠️ Удалить позицию", "d"),
        ("👀 Предпросмотр карточки", "v"),
    )
    for text, action in actions:
        builder.button(
            text=text,
            callback_data=AdminItemCallback(
                action=action,
                category_id=category_id,
                subcategory_id=sub_id,
                item_id=item.id,
            ),
        )
    builder.button(
        text="⬅️ К позициям",
        callback_data=AdminItemCallback(
            action="l",
            category_id=category_id,
            subcategory_id=sub_id,
        ),
    )
    builder.adjust(1)
    return builder.as_markup()
