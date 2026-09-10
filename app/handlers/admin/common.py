"""Authorization guards and common administrator-panel handlers."""

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove, User

from app.handlers.common import edit_or_send_text
from app.keyboards.admin import build_admin_panel_keyboard
from app.keyboards.admin_callbacks import (
    AdminCancelCallback,
    AdminEntityCallback,
    AdminPanelCallback,
)
from app.keyboards.admin_catalog import (
    build_admin_categories_keyboard,
    build_admin_category_picker,
)
from app.services.admin_service import AdminActor, AdminService
from app.services.auth_service import AuthService

router = Router(name=__name__)
logger = logging.getLogger(__name__)
ADMIN_HEADER = "🛠 <b>Режим администратора</b>"


def actor_from_user(user: User) -> AdminActor:
    return AdminActor(user_id=user.id, username=user.username)


async def require_admin_callback(
    callback: CallbackQuery,
    auth_service: AuthService,
) -> bool:
    """Answer every callback and reject unauthorized direct attempts."""

    user = callback.from_user
    if not auth_service.is_admin(user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return False
    await callback.answer()
    return True


async def require_admin_message(
    message: Message,
    auth_service: AuthService,
) -> bool:
    """Reject unauthorized admin commands and FSM messages."""

    user = message.from_user
    if user is None or not auth_service.is_admin(user.id):
        await message.answer("Недостаточно прав")
        return False
    return True


async def show_admin_panel(message: Message) -> None:
    await edit_or_send_text(
        message,
        f"{ADMIN_HEADER}\n\nВыберите действие:",
        build_admin_panel_keyboard(),
    )


@router.message(Command("admin"))
async def handle_admin_command(
    message: Message,
    state: FSMContext,
    auth_service: AuthService,
) -> None:
    if not await require_admin_message(message, auth_service):
        return
    current_state = await state.get_state()
    if current_state and current_state.startswith("UserPickerStates:"):
        await message.answer(
            "Выбор пользователя отменён.",
            reply_markup=ReplyKeyboardRemove(),
        )
    await state.clear()
    await message.answer(
        f"{ADMIN_HEADER}\n\nВыберите действие:",
        reply_markup=build_admin_panel_keyboard(),
    )


@router.message(Command("cancel"))
async def handle_admin_cancel_command(
    message: Message,
    state: FSMContext,
    auth_service: AuthService,
) -> None:
    """Cancel any active administrator FSM without treating it as input."""

    current_state = await state.get_state()
    if current_state is None:
        return
    if current_state.startswith("Warehouse"):
        return
    if not await require_admin_message(message, auth_service):
        return
    await state.clear()
    if current_state.startswith("UserPickerStates:"):
        await message.answer(
            "Выбор пользователя отменён.",
            reply_markup=ReplyKeyboardRemove(),
        )
    await message.answer(
        "❌ Действие отменено",
        reply_markup=build_admin_panel_keyboard(),
    )


@router.callback_query(AdminPanelCallback.filter())
async def handle_admin_panel(
    callback: CallbackQuery,
    callback_data: AdminPanelCallback,
    state: FSMContext,
    auth_service: AuthService,
    admin_service: AdminService,
) -> None:
    if not await require_admin_callback(callback, auth_service):
        return
    current_state = await state.get_state()
    if (
        current_state
        and current_state.startswith("UserPickerStates:")
        and isinstance(callback.message, Message)
    ):
        await callback.message.answer(
            "Выбор пользователя отменён.",
            reply_markup=ReplyKeyboardRemove(),
        )
    await state.clear()
    if not isinstance(callback.message, Message):
        return
    if callback_data.action == "o":
        await show_admin_panel(callback.message)
        return

    catalog = await admin_service.get_catalog()
    target = "s" if callback_data.action == "s" else "i"
    await edit_or_send_text(
        callback.message,
        f"{ADMIN_HEADER}\n\nВыберите родительский раздел:",
        build_admin_category_picker(catalog.categories, target),
    )


@router.callback_query(AdminEntityCallback.filter())
async def handle_admin_entity(
    callback: CallbackQuery,
    callback_data: AdminEntityCallback,
    auth_service: AuthService,
    admin_service: AdminService,
) -> None:
    if not await require_admin_callback(callback, auth_service):
        return
    if not isinstance(callback.message, Message):
        return

    catalog = await admin_service.get_catalog()
    if callback_data.entity == "c":
        await edit_or_send_text(
            callback.message,
            f"{ADMIN_HEADER}\n\n📁 Разделы:",
            build_admin_categories_keyboard(catalog.categories),
        )
    else:
        target = "q" if callback_data.entity == "q" else callback_data.entity
        await edit_or_send_text(
            callback.message,
            f"{ADMIN_HEADER}\n\nВыберите родительский раздел:",
            build_admin_category_picker(catalog.categories, target),
        )


@router.callback_query(AdminCancelCallback.filter(F.action == "x"))
async def handle_admin_cancel(
    callback: CallbackQuery,
    state: FSMContext,
    auth_service: AuthService,
) -> None:
    if not await require_admin_callback(callback, auth_service):
        return
    await state.clear()
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message,
            f"{ADMIN_HEADER}\n\n❌ Действие отменено",
            build_admin_panel_keyboard(),
        )
