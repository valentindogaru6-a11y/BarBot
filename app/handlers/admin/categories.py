"""Administrator category CRUD handlers."""

import logging
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from pydantic import ValidationError

from app.handlers.admin.common import (
    ADMIN_HEADER,
    actor_from_user,
    require_admin_callback,
    require_admin_message,
)
from app.handlers.admin.states import (
    CreateCategoryStates,
    EditCategoryStates,
)
from app.handlers.common import edit_or_send_text
from app.keyboards.admin import (
    build_admin_cancel_keyboard,
    build_admin_confirm_keyboard,
)
from app.keyboards.admin_callbacks import (
    AdminCategoryCallback,
    AdminConfirmCallback,
    AdminPaginationCallback,
)
from app.keyboards.admin_catalog import (
    build_admin_categories_keyboard,
    build_admin_category_actions,
)
from app.models.catalog_item import MAX_STABLE_ID_LENGTH
from app.models.category import Category
from app.services.admin_service import AdminService, AdminServiceError
from app.services.auth_service import AuthService
from app.services.stable_id import (
    StableIdGenerationError,
    generate_stable_id,
)

router = Router(name=__name__)
logger = logging.getLogger(__name__)


def _category_text(category: Category) -> str:
    status = "активен" if category.is_active else "скрыто"
    nested_items = len(category.items) + sum(
        len(subcategory.items)
        for subcategory in category.subcategories
    )
    return (
        f"{ADMIN_HEADER}\n\n"
        f"<b>{escape(category.name)}</b> ({category.id})\n"
        f"Статус: {status}\n"
        f"Порядок: {category.sort_order}\n"
        f"Подразделов: {len(category.subcategories)}\n"
        f"Позиций: {nested_items}"
    )


async def _show_category(
    message: Message,
    admin_service: AdminService,
    category_id: str,
) -> None:
    catalog = await admin_service.get_catalog()
    category = AdminService._category(catalog, category_id)
    await edit_or_send_text(
        message,
        _category_text(category),
        build_admin_category_actions(category),
    )


@router.callback_query(
    AdminPaginationCallback.filter(F.entity == "c")
)
async def handle_category_page(
    callback: CallbackQuery,
    callback_data: AdminPaginationCallback,
    auth_service: AuthService,
    admin_service: AdminService,
) -> None:
    if not await require_admin_callback(callback, auth_service):
        return
    if not isinstance(callback.message, Message):
        return
    catalog = await admin_service.get_catalog()
    mode = {"a": "all", "v": "active", "h": "hidden"}.get(
        callback_data.filter_mode,
        "all",
    )
    categories = AdminService.filter_entities(catalog.categories, mode)
    await edit_or_send_text(
        callback.message,
        f"{ADMIN_HEADER}\n\n📁 Разделы:",
        build_admin_categories_keyboard(
            categories,
            callback_data.page,
            callback_data.filter_mode,
        ),
    )


@router.callback_query(AdminCategoryCallback.filter(F.action == "o"))
async def handle_category_open(
    callback: CallbackQuery,
    callback_data: AdminCategoryCallback,
    auth_service: AuthService,
    admin_service: AdminService,
) -> None:
    if not await require_admin_callback(callback, auth_service):
        return
    if isinstance(callback.message, Message):
        await _show_category(
            callback.message,
            admin_service,
            callback_data.category_id,
        )


@router.callback_query(AdminCategoryCallback.filter(F.action == "n"))
async def handle_category_create_start(
    callback: CallbackQuery,
    state: FSMContext,
    auth_service: AuthService,
) -> None:
    if not await require_admin_callback(callback, auth_service):
        return
    await state.clear()
    await state.set_state(CreateCategoryStates.name)
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message,
            f"{ADMIN_HEADER}\n\nВведите название раздела:",
            build_admin_cancel_keyboard(),
        )


@router.message(CreateCategoryStates.name)
async def category_create_name(
    message: Message,
    state: FSMContext,
    auth_service: AuthService,
    admin_service: AdminService,
) -> None:
    if not await require_admin_message(message, auth_service):
        return
    value = (message.text or "").strip()
    if not value:
        await message.answer("Название не может быть пустым.")
        return
    catalog = await admin_service.get_catalog()
    try:
        stable_id = generate_stable_id(
            value,
            {category.id for category in catalog.categories},
            MAX_STABLE_ID_LENGTH,
        )
    except StableIdGenerationError as exc:
        await message.answer(str(exc))
        return
    await state.update_data(name=value, stable_id=stable_id)
    await state.set_state(CreateCategoryStates.emoji)
    await message.answer("Введите emoji или -:", reply_markup=build_admin_cancel_keyboard())


@router.message(CreateCategoryStates.emoji)
async def category_create_emoji(
    message: Message,
    state: FSMContext,
    auth_service: AuthService,
) -> None:
    if not await require_admin_message(message, auth_service):
        return
    value = (message.text or "").strip()
    await state.update_data(emoji=None if value == "-" else value)
    await state.set_state(CreateCategoryStates.description)
    await message.answer(
        "Введите описание или -:",
        reply_markup=build_admin_cancel_keyboard(),
    )


@router.message(CreateCategoryStates.description)
async def category_create_description(
    message: Message,
    state: FSMContext,
    auth_service: AuthService,
) -> None:
    if not await require_admin_message(message, auth_service):
        return
    value = (message.text or "").strip()
    await state.update_data(description=None if value == "-" else value)
    await state.set_state(CreateCategoryStates.sort_order)
    await message.answer(
        "Введите целый sort_order:",
        reply_markup=build_admin_cancel_keyboard(),
    )


@router.message(CreateCategoryStates.sort_order)
async def category_create_sort(
    message: Message,
    state: FSMContext,
    auth_service: AuthService,
) -> None:
    if not await require_admin_message(message, auth_service):
        return
    try:
        sort_order = int((message.text or "").strip())
    except ValueError:
        await message.answer("sort_order должен быть целым числом.")
        return
    await state.update_data(sort_order=sort_order)
    data = await state.get_data()
    preview = Category(
        id=data["stable_id"],
        name=data["name"],
        emoji=data.get("emoji"),
        description=data.get("description"),
        sort_order=sort_order,
    )
    await state.set_state(CreateCategoryStates.confirm)
    await message.answer(
        f"{_category_text(preview)}\n\nСохранить раздел?",
        reply_markup=build_admin_confirm_keyboard(action="cc"),
    )


@router.callback_query(AdminConfirmCallback.filter(F.action == "cc"))
async def category_create_confirm(
    callback: CallbackQuery,
    state: FSMContext,
    auth_service: AuthService,
    admin_service: AdminService,
) -> None:
    if not await require_admin_callback(callback, auth_service):
        return
    data = await state.get_data()
    try:
        catalog = await admin_service.get_catalog()
        stable_id = generate_stable_id(
            data["name"],
            {category.id for category in catalog.categories},
            MAX_STABLE_ID_LENGTH,
        )
        category = Category(
            id=stable_id,
            name=data["name"],
            emoji=data.get("emoji"),
            description=data.get("description"),
            sort_order=data["sort_order"],
        )
        await admin_service.create_category(
            actor_from_user(callback.from_user),
            category,
        )
    except (
        AdminServiceError,
        StableIdGenerationError,
        ValidationError,
        KeyError,
    ):
        logger.exception("Could not create category")
        if isinstance(callback.message, Message):
            await callback.message.answer("⚠️ Не удалось сохранить изменения")
        return
    await state.clear()
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message,
            f"{ADMIN_HEADER}\n\n✅ Изменения сохранены",
            build_admin_category_actions(category),
        )


@router.callback_query(AdminCategoryCallback.filter(F.action.startswith("e")))
async def category_edit_start(
    callback: CallbackQuery,
    callback_data: AdminCategoryCallback,
    state: FSMContext,
    auth_service: AuthService,
) -> None:
    if not await require_admin_callback(callback, auth_service):
        return
    fields = {
        "en": ("name", "Введите новое название:"),
        "ee": ("emoji", "Введите emoji или - для очистки:"),
        "ed": ("description", "Введите описание или - для очистки:"),
        "es": ("sort_order", "Введите целый sort_order:"),
    }
    field, prompt = fields[callback_data.action]
    await state.set_state(EditCategoryStates.value)
    await state.set_data(
        {"category_id": callback_data.category_id, "field": field}
    )
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message,
            f"{ADMIN_HEADER}\n\n{prompt}",
            build_admin_cancel_keyboard(),
        )


@router.message(EditCategoryStates.value)
async def category_edit_value(
    message: Message,
    state: FSMContext,
    auth_service: AuthService,
    admin_service: AdminService,
) -> None:
    if not await require_admin_message(message, auth_service):
        return
    data = await state.get_data()
    raw = (message.text or "").strip()
    field = data["field"]
    if field == "name" and not raw:
        await message.answer("Название не может быть пустым.")
        return
    if field == "sort_order":
        try:
            value: object = int(raw)
        except ValueError:
            await message.answer("sort_order должен быть целым числом.")
            return
    else:
        value = None if raw == "-" else raw
    try:
        await admin_service.update_category(
            actor_from_user(message.from_user),
            data["category_id"],
            **{field: value},
        )
    except Exception:
        logger.exception("Could not update category")
        await message.answer("⚠️ Не удалось сохранить изменения")
        return
    await state.clear()
    await message.answer("✅ Изменения сохранены")
    await _show_category(message, admin_service, data["category_id"])


@router.callback_query(AdminCategoryCallback.filter(F.action == "t"))
async def category_toggle(
    callback: CallbackQuery,
    callback_data: AdminCategoryCallback,
    auth_service: AuthService,
    admin_service: AdminService,
) -> None:
    if not await require_admin_callback(callback, auth_service):
        return
    try:
        await admin_service.toggle_category(
            actor_from_user(callback.from_user),
            callback_data.category_id,
        )
        if isinstance(callback.message, Message):
            await _show_category(
                callback.message,
                admin_service,
                callback_data.category_id,
            )
    except Exception:
        logger.exception("Could not toggle category")
        if isinstance(callback.message, Message):
            await callback.message.answer("⚠️ Не удалось сохранить изменения")


@router.callback_query(AdminCategoryCallback.filter(F.action == "d"))
async def category_delete_prompt(
    callback: CallbackQuery,
    callback_data: AdminCategoryCallback,
    auth_service: AuthService,
    admin_service: AdminService,
) -> None:
    if not await require_admin_callback(callback, auth_service):
        return
    catalog = await admin_service.get_catalog()
    category = AdminService._category(catalog, callback_data.category_id)
    count = len(category.items) + sum(
        len(subcategory.items)
        for subcategory in category.subcategories
    )
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message,
            f"{ADMIN_HEADER}\n\n⚠️ Удалить «{escape(category.name)}»?\n"
            f"Подразделов: {len(category.subcategories)}, позиций: {count}.",
            build_admin_confirm_keyboard(
                action="dc",
                category_id=category.id,
            ),
        )


@router.callback_query(AdminConfirmCallback.filter(F.action == "dc"))
async def category_delete_confirm(
    callback: CallbackQuery,
    callback_data: AdminConfirmCallback,
    auth_service: AuthService,
    admin_service: AdminService,
) -> None:
    if not await require_admin_callback(callback, auth_service):
        return
    try:
        catalog = await admin_service.delete_category(
            actor_from_user(callback.from_user),
            callback_data.category_id,
            confirmed=True,
        )
    except Exception:
        logger.exception("Could not delete category")
        if isinstance(callback.message, Message):
            await callback.message.answer("⚠️ Не удалось сохранить изменения")
        return
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message,
            f"{ADMIN_HEADER}\n\n✅ Изменения сохранены",
            build_admin_categories_keyboard(catalog.categories),
        )
