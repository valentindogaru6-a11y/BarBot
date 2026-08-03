"""Administrator subcategory CRUD handlers."""

import logging
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.handlers.admin.common import (
    ADMIN_HEADER,
    actor_from_user,
    require_admin_callback,
    require_admin_message,
)
from app.handlers.admin.states import (
    CreateSubcategoryStates,
    EditSubcategoryStates,
)
from app.handlers.common import edit_or_send_text
from app.keyboards.admin import (
    build_admin_cancel_keyboard,
    build_admin_confirm_keyboard,
)
from app.keyboards.admin_callbacks import (
    AdminConfirmCallback,
    AdminPaginationCallback,
    AdminSubcategoryCallback,
)
from app.keyboards.admin_catalog import (
    build_admin_subcategories_keyboard,
    build_admin_subcategory_actions,
)
from app.models.catalog_item import MAX_STABLE_ID_LENGTH
from app.models.subcategory import Subcategory
from app.services.admin_service import AdminService
from app.services.auth_service import AuthService
from app.services.stable_id import (
    StableIdGenerationError,
    generate_stable_id,
)

router = Router(name=__name__)
logger = logging.getLogger(__name__)


def _text(category_id: str, subcategory: Subcategory) -> str:
    status = "активен" if subcategory.is_active else "скрыто"
    return (
        f"{ADMIN_HEADER}\n\n"
        f"<b>{escape(subcategory.name)}</b> ({subcategory.id})\n"
        f"Родитель: {category_id}\n"
        f"Статус: {status}\n"
        f"Порядок: {subcategory.sort_order}\n"
        f"Позиций: {len(subcategory.items)}"
    )


async def _show(
    message: Message,
    service: AdminService,
    category_id: str,
    subcategory_id: str,
) -> None:
    catalog = await service.get_catalog()
    subcategory = AdminService._subcategory(
        catalog,
        category_id,
        subcategory_id,
    )
    await edit_or_send_text(
        message,
        _text(category_id, subcategory),
        build_admin_subcategory_actions(category_id, subcategory),
    )


@router.callback_query(AdminSubcategoryCallback.filter(F.action == "l"))
async def subcategory_list(
    callback: CallbackQuery,
    callback_data: AdminSubcategoryCallback,
    auth_service: AuthService,
    admin_service: AdminService,
) -> None:
    if not await require_admin_callback(callback, auth_service):
        return
    catalog = await admin_service.get_catalog()
    category = AdminService._category(catalog, callback_data.category_id)
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message,
            f"{ADMIN_HEADER}\n\n🗂 Подразделы «{escape(category.name)}»:",
            build_admin_subcategories_keyboard(
                category,
                category.subcategories,
            ),
        )


@router.callback_query(AdminSubcategoryCallback.filter(F.action == "o"))
async def subcategory_open(
    callback: CallbackQuery,
    callback_data: AdminSubcategoryCallback,
    auth_service: AuthService,
    admin_service: AdminService,
) -> None:
    if not await require_admin_callback(callback, auth_service):
        return
    if isinstance(callback.message, Message):
        await _show(
            callback.message,
            admin_service,
            callback_data.category_id,
            callback_data.subcategory_id,
        )


@router.callback_query(
    AdminPaginationCallback.filter(F.entity == "s")
)
async def subcategory_page(
    callback: CallbackQuery,
    callback_data: AdminPaginationCallback,
    auth_service: AuthService,
    admin_service: AdminService,
) -> None:
    if not await require_admin_callback(callback, auth_service):
        return
    catalog = await admin_service.get_catalog()
    category = AdminService._category(
        catalog,
        callback_data.category_id,
    )
    mode = {"a": "all", "v": "active", "h": "hidden"}.get(
        callback_data.filter_mode,
        "all",
    )
    subcategories = AdminService.filter_entities(
        category.subcategories,
        mode,
    )
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message,
            f"{ADMIN_HEADER}\n\n🗂 Подразделы «{escape(category.name)}»:",
            build_admin_subcategories_keyboard(
                category,
                subcategories,
                callback_data.page,
                callback_data.filter_mode,
            ),
        )


@router.callback_query(AdminSubcategoryCallback.filter(F.action == "n"))
async def subcategory_create_start(
    callback: CallbackQuery,
    callback_data: AdminSubcategoryCallback,
    state: FSMContext,
    auth_service: AuthService,
) -> None:
    if not await require_admin_callback(callback, auth_service):
        return
    await state.set_state(CreateSubcategoryStates.name)
    await state.set_data({"category_id": callback_data.category_id})
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message,
            f"{ADMIN_HEADER}\n\nВведите название подраздела:",
            build_admin_cancel_keyboard(),
        )


@router.message(CreateSubcategoryStates.name)
async def subcategory_create_name(
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
    data = await state.get_data()
    catalog = await admin_service.get_catalog()
    category = AdminService._category(catalog, data["category_id"])
    try:
        stable_id = generate_stable_id(
            value,
            {entry.id for entry in category.subcategories},
            MAX_STABLE_ID_LENGTH,
        )
    except StableIdGenerationError as exc:
        await message.answer(str(exc))
        return
    await state.update_data(name=value, stable_id=stable_id)
    await state.set_state(CreateSubcategoryStates.emoji)
    await message.answer("Введите emoji или -:", reply_markup=build_admin_cancel_keyboard())


@router.message(CreateSubcategoryStates.emoji)
async def subcategory_create_emoji(
    message: Message,
    state: FSMContext,
    auth_service: AuthService,
) -> None:
    if not await require_admin_message(message, auth_service):
        return
    value = (message.text or "").strip()
    await state.update_data(emoji=None if value == "-" else value)
    await state.set_state(CreateSubcategoryStates.description)
    await message.answer(
        "Введите описание или -:",
        reply_markup=build_admin_cancel_keyboard(),
    )


@router.message(CreateSubcategoryStates.description)
async def subcategory_create_description(
    message: Message,
    state: FSMContext,
    auth_service: AuthService,
) -> None:
    if not await require_admin_message(message, auth_service):
        return
    value = (message.text or "").strip()
    await state.update_data(description=None if value == "-" else value)
    await state.set_state(CreateSubcategoryStates.sort_order)
    await message.answer(
        "Введите целый sort_order:",
        reply_markup=build_admin_cancel_keyboard(),
    )


@router.message(CreateSubcategoryStates.sort_order)
async def subcategory_create_sort(
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
    preview = Subcategory(
        id=data["stable_id"],
        name=data["name"],
        emoji=data.get("emoji"),
        description=data.get("description"),
        sort_order=sort_order,
    )
    await state.set_state(CreateSubcategoryStates.confirm)
    await message.answer(
        f"{_text(data['category_id'], preview)}\n\nСохранить подраздел?",
        reply_markup=build_admin_confirm_keyboard(
            action="cs",
            category_id=data["category_id"],
        ),
    )


@router.callback_query(AdminConfirmCallback.filter(F.action == "cs"))
async def subcategory_create_confirm(
    callback: CallbackQuery,
    callback_data: AdminConfirmCallback,
    state: FSMContext,
    auth_service: AuthService,
    admin_service: AdminService,
) -> None:
    if not await require_admin_callback(callback, auth_service):
        return
    data = await state.get_data()
    try:
        catalog = await admin_service.get_catalog()
        category = AdminService._category(
            catalog,
            callback_data.category_id,
        )
        stable_id = generate_stable_id(
            data["name"],
            {entry.id for entry in category.subcategories},
            MAX_STABLE_ID_LENGTH,
        )
        subcategory = Subcategory(
            id=stable_id,
            name=data["name"],
            emoji=data.get("emoji"),
            description=data.get("description"),
            sort_order=data["sort_order"],
        )
        await admin_service.create_subcategory(
            actor_from_user(callback.from_user),
            callback_data.category_id,
            subcategory,
        )
    except Exception:
        logger.exception("Could not create subcategory")
        if isinstance(callback.message, Message):
            await callback.message.answer("⚠️ Не удалось сохранить изменения")
        return
    await state.clear()
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message,
            f"{ADMIN_HEADER}\n\n✅ Изменения сохранены",
            build_admin_subcategory_actions(
                callback_data.category_id,
                subcategory,
            ),
        )


@router.callback_query(
    AdminSubcategoryCallback.filter(F.action.startswith("e"))
)
async def subcategory_edit_start(
    callback: CallbackQuery,
    callback_data: AdminSubcategoryCallback,
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
    await state.set_state(EditSubcategoryStates.value)
    await state.set_data(
        {
            "category_id": callback_data.category_id,
            "subcategory_id": callback_data.subcategory_id,
            "field": field,
        }
    )
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message,
            f"{ADMIN_HEADER}\n\n{prompt}",
            build_admin_cancel_keyboard(),
        )


@router.message(EditSubcategoryStates.value)
async def subcategory_edit_value(
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
        await admin_service.update_subcategory(
            actor_from_user(message.from_user),
            data["category_id"],
            data["subcategory_id"],
            **{field: value},
        )
    except Exception:
        logger.exception("Could not update subcategory")
        await message.answer("⚠️ Не удалось сохранить изменения")
        return
    await state.clear()
    await message.answer("✅ Изменения сохранены")
    await _show(
        message,
        admin_service,
        data["category_id"],
        data["subcategory_id"],
    )


@router.callback_query(AdminSubcategoryCallback.filter(F.action == "t"))
async def subcategory_toggle(
    callback: CallbackQuery,
    callback_data: AdminSubcategoryCallback,
    auth_service: AuthService,
    admin_service: AdminService,
) -> None:
    if not await require_admin_callback(callback, auth_service):
        return
    try:
        await admin_service.toggle_subcategory(
            actor_from_user(callback.from_user),
            callback_data.category_id,
            callback_data.subcategory_id,
        )
        if isinstance(callback.message, Message):
            await _show(
                callback.message,
                admin_service,
                callback_data.category_id,
                callback_data.subcategory_id,
            )
    except Exception:
        logger.exception("Could not toggle subcategory")
        if isinstance(callback.message, Message):
            await callback.message.answer("⚠️ Не удалось сохранить изменения")


@router.callback_query(AdminSubcategoryCallback.filter(F.action == "d"))
async def subcategory_delete_prompt(
    callback: CallbackQuery,
    callback_data: AdminSubcategoryCallback,
    auth_service: AuthService,
    admin_service: AdminService,
) -> None:
    if not await require_admin_callback(callback, auth_service):
        return
    catalog = await admin_service.get_catalog()
    subcategory = AdminService._subcategory(
        catalog,
        callback_data.category_id,
        callback_data.subcategory_id,
    )
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message,
            f"{ADMIN_HEADER}\n\n⚠️ Удалить «{escape(subcategory.name)}»?\n"
            f"Вложенных позиций: {len(subcategory.items)}.",
            build_admin_confirm_keyboard(
                action="ds",
                category_id=callback_data.category_id,
                subcategory_id=subcategory.id,
            ),
        )


@router.callback_query(AdminConfirmCallback.filter(F.action == "ds"))
async def subcategory_delete_confirm(
    callback: CallbackQuery,
    callback_data: AdminConfirmCallback,
    auth_service: AuthService,
    admin_service: AdminService,
) -> None:
    if not await require_admin_callback(callback, auth_service):
        return
    try:
        catalog = await admin_service.delete_subcategory(
            actor_from_user(callback.from_user),
            callback_data.category_id,
            callback_data.subcategory_id,
            confirmed=True,
        )
        category = AdminService._category(
            catalog,
            callback_data.category_id,
        )
    except Exception:
        logger.exception("Could not delete subcategory")
        if isinstance(callback.message, Message):
            await callback.message.answer("⚠️ Не удалось сохранить изменения")
        return
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message,
            f"{ADMIN_HEADER}\n\n✅ Изменения сохранены",
            build_admin_subcategories_keyboard(
                category,
                category.subcategories,
            ),
        )
