"""Administrator photo replacement and deletion workflows."""

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.handlers.admin.common import (
    ADMIN_HEADER,
    actor_from_user,
    require_admin_callback,
    require_admin_message,
)
from app.handlers.admin.items import _show_item, _sub_id
from app.handlers.admin.states import CreateItemStates, ReplacePhotoStates
from app.handlers.common import edit_or_send_text
from app.keyboards.admin import (
    build_admin_confirm_keyboard,
    build_admin_wizard_keyboard,
)
from app.keyboards.admin_callbacks import (
    AdminConfirmCallback,
    AdminItemCallback,
)
from app.services.admin_service import AdminService
from app.services.auth_service import AuthService

router = Router(name=__name__)
logger = logging.getLogger(__name__)


@router.callback_query(AdminItemCallback.filter(F.action == "rp"))
async def replace_photo_start(
    callback: CallbackQuery,
    callback_data: AdminItemCallback,
    state: FSMContext,
    auth_service: AuthService,
) -> None:
    if not await require_admin_callback(callback, auth_service):
        return
    await state.set_state(ReplacePhotoStates.photo)
    await state.set_data(
        {
            "category_id": callback_data.category_id,
            "subcategory_id": _sub_id(callback_data.subcategory_id),
            "item_id": callback_data.item_id,
        }
    )
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message,
            f"{ADMIN_HEADER}\n\nОтправьте изображение как фотографию.",
            build_admin_wizard_keyboard(False),
        )


@router.message(ReplacePhotoStates.photo, F.photo)
async def replace_photo_receive(
    message: Message,
    state: FSMContext,
    auth_service: AuthService,
) -> None:
    if not await require_admin_message(message, auth_service):
        return
    file_id = message.photo[-1].file_id
    await state.update_data(pending_photo_file_id=file_id)
    await state.set_state(ReplacePhotoStates.confirm)
    await message.answer_photo(
        photo=file_id,
        caption=f"{ADMIN_HEADER}\n\nСохранить эту фотографию?",
        reply_markup=build_admin_confirm_keyboard(action="up"),
    )


@router.callback_query(AdminConfirmCallback.filter(F.action == "up"))
async def replace_photo_confirm(
    callback: CallbackQuery,
    state: FSMContext,
    auth_service: AuthService,
    admin_service: AdminService,
) -> None:
    if not await require_admin_callback(callback, auth_service):
        return
    data = await state.get_data()
    try:
        await admin_service.replace_photo(
            actor_from_user(callback.from_user),
            data["category_id"],
            data.get("subcategory_id"),
            data["item_id"],
            data["pending_photo_file_id"],
        )
    except Exception:
        logger.exception("Could not replace item photo")
        if isinstance(callback.message, Message):
            await callback.message.answer("⚠️ Не удалось сохранить изменения")
        return
    await state.clear()
    if isinstance(callback.message, Message):
        await callback.message.answer("✅ Изменения сохранены")
        await _show_item(
            callback.message,
            admin_service,
            data["category_id"],
            data.get("subcategory_id"),
            data["item_id"],
        )


@router.callback_query(AdminItemCallback.filter(F.action == "dp"))
async def delete_photo_prompt(
    callback: CallbackQuery,
    callback_data: AdminItemCallback,
    auth_service: AuthService,
) -> None:
    if not await require_admin_callback(callback, auth_service):
        return
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message,
            f"{ADMIN_HEADER}\n\n⚠️ Удалить фотографию из карточки?",
            build_admin_confirm_keyboard(
                action="xp",
                category_id=callback_data.category_id,
                subcategory_id=callback_data.subcategory_id,
                item_id=callback_data.item_id,
            ),
        )


@router.callback_query(AdminConfirmCallback.filter(F.action == "xp"))
async def delete_photo_confirm(
    callback: CallbackQuery,
    callback_data: AdminConfirmCallback,
    auth_service: AuthService,
    admin_service: AdminService,
) -> None:
    if not await require_admin_callback(callback, auth_service):
        return
    try:
        await admin_service.remove_photo(
            actor_from_user(callback.from_user),
            callback_data.category_id,
            _sub_id(callback_data.subcategory_id),
            callback_data.item_id,
            confirmed=True,
        )
    except Exception:
        logger.exception("Could not remove item photo")
        if isinstance(callback.message, Message):
            await callback.message.answer("⚠️ Не удалось сохранить изменения")
        return
    if isinstance(callback.message, Message):
        await _show_item(
            callback.message,
            admin_service,
            callback_data.category_id,
            _sub_id(callback_data.subcategory_id),
            callback_data.item_id,
        )


@router.message(CreateItemStates.photo, F.photo)
async def create_photo_receive(
    message: Message,
    state: FSMContext,
    auth_service: AuthService,
) -> None:
    if not await require_admin_message(message, auth_service):
        return
    file_id = message.photo[-1].file_id
    await state.update_data(pending_photo_file_id=file_id)
    await state.set_state(CreateItemStates.photo_confirm)
    await message.answer_photo(
        photo=file_id,
        caption=f"{ADMIN_HEADER}\n\nИспользовать эту фотографию?",
        reply_markup=build_admin_confirm_keyboard(action="cp"),
    )


@router.callback_query(AdminConfirmCallback.filter(F.action == "cp"))
async def create_photo_confirm(
    callback: CallbackQuery,
    state: FSMContext,
    auth_service: AuthService,
) -> None:
    if not await require_admin_callback(callback, auth_service):
        return
    data = await state.get_data()
    await state.update_data(
        photo_file_id=data.get("pending_photo_file_id"),
        pending_photo_file_id=None,
    )
    await state.set_state(CreateItemStates.sort_order)
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "Введите целый sort_order:",
            reply_markup=build_admin_wizard_keyboard(),
        )


@router.message(
    ReplacePhotoStates.photo,
    F.document,
)
@router.message(
    CreateItemStates.photo,
    F.document,
)
async def reject_photo_document(
    message: Message,
    auth_service: AuthService,
) -> None:
    if not await require_admin_message(message, auth_service):
        return
    await message.answer(
        "Отправьте изображение именно как фотографию, не как документ."
    )
