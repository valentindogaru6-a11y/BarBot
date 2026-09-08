"""Administrator workflows for local users and role management."""

import logging
import secrets
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove

from app.handlers.admin.common import ADMIN_HEADER
from app.handlers.admin.user_states import (
    RoleChangeStates,
    UserPickerStates,
    UserSearchStates,
)
from app.handlers.common import edit_or_send_text
from app.keyboards.user_admin import (
    ROLE_LABELS,
    build_picker_inline_keyboard,
    build_picker_reply_keyboard,
    build_role_confirmation_keyboard,
    build_user_cancel_keyboard,
    build_user_card_keyboard,
    build_user_management_keyboard,
    build_users_list_keyboard,
)
from app.keyboards.user_callbacks import (
    UserAdminCallback,
    UserListCallback,
    UserRoleCallback,
    UserViewCallback,
)
from app.models.user import DATABASE_ROLES, Role, UserRecord
from app.services.access_service import AccessDeniedError
from app.services.auth_service import AuthService
from app.services.user_service import UserService
from app.telegram_commands import update_user_commands

router = Router(name=__name__)
logger = logging.getLogger(__name__)
USERS_HEADER = "👥 <b>Управление пользователями</b>"
PAGE_SIZE = 8


async def _require_callback(
    callback: CallbackQuery,
    auth_service: AuthService,
) -> bool:
    if not auth_service.can_manage_users(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return False
    await callback.answer()
    return True


async def _require_message(
    message: Message,
    auth_service: AuthService,
) -> bool:
    user = message.from_user
    if user is None or not auth_service.can_manage_users(user.id):
        await message.answer("Недостаточно прав")
        return False
    return True


async def _show_menu(
    message: Message,
    auth_service: AuthService,
    actor_user_id: int,
) -> None:
    await edit_or_send_text(
        message,
        f"{USERS_HEADER}\n\nВыберите действие:",
        build_user_management_keyboard(
            can_assign_admin=auth_service.can_assign_admin(actor_user_id),
            can_assign_accountant=auth_service.can_assign_accountant(
                actor_user_id
            ),
        ),
    )


def _parse_role(value: str) -> Role | None:
    try:
        role = Role(value)
    except ValueError:
        return None
    return role if role in DATABASE_ROLES else None


def _filtered_users(
    user_service: UserService,
    auth_service: AuthService,
    role_value: str,
    page: int,
) -> tuple[list[tuple[UserRecord, Role]], int, int]:
    all_users, _, _ = user_service.list_users(page_size=100_000)
    role = _parse_role(role_value)
    pairs = [
        (user, auth_service.get_context(user.telegram_user_id).role)
        for user in all_users
    ]
    if role is not None:
        visible_roles = (
            {Role.SUPER_ADMIN, Role.ADMIN}
            if role is Role.ADMIN
            else {role}
        )
        pairs = [pair for pair in pairs if pair[1] in visible_roles]
    total_pages = max(1, (len(pairs) + PAGE_SIZE - 1) // PAGE_SIZE)
    safe_page = min(max(page, 0), total_pages - 1)
    start = safe_page * PAGE_SIZE
    return pairs[start : start + PAGE_SIZE], safe_page, total_pages


async def _show_list(
    message: Message,
    user_service: UserService,
    auth_service: AuthService,
    *,
    role: str,
    page: int = 0,
) -> None:
    users, safe_page, total_pages = _filtered_users(
        user_service,
        auth_service,
        role,
        page,
    )
    role_model = _parse_role(role)
    title = ROLE_LABELS.get(role_model, "Все пользователи")
    text = f"{USERS_HEADER}\n\n<b>{title}</b>"
    if not users:
        text += "\n\nСписок пуст."
    await edit_or_send_text(
        message,
        text,
        build_users_list_keyboard(
            users,
            role=role,
            page=safe_page,
            total_pages=total_pages,
        ),
    )


def _format_user(user: UserRecord, role: Role) -> str:
    username = f"@{escape(user.username)}" if user.username else "отсутствует"
    creator = str(user.created_by) if user.created_by else "неизвестно"
    activity = "активен" if role is not Role.BLOCKED else "заблокирован"
    return (
        f"{USERS_HEADER}\n\n"
        f"<b>Имя:</b> {escape(user.display_name)}\n"
        f"<b>Username:</b> {username}\n"
        f"<b>Telegram ID:</b> <code>{user.telegram_user_id}</code>\n"
        f"<b>Роль:</b> {ROLE_LABELS[role]}\n"
        f"<b>Первый вход:</b> {escape(user.first_seen_at)}\n"
        f"<b>Последняя активность:</b> {escape(user.last_seen_at)}\n"
        f"<b>Статус:</b> {activity}\n"
        f"<b>Роль назначена:</b> {creator}"
    )


async def _show_user_card(
    message: Message,
    user_service: UserService,
    auth_service: AuthService,
    actor_user_id: int,
    target_user_id: int,
    *,
    back_role: str = "all",
    back_page: int = 0,
) -> None:
    user = user_service.get_user(target_user_id)
    if user is None:
        await edit_or_send_text(
            message,
            f"{USERS_HEADER}\n\nПользователь больше не найден.",
            build_user_management_keyboard(
                can_assign_admin=auth_service.can_assign_admin(actor_user_id),
                can_assign_accountant=auth_service.can_assign_accountant(
                    actor_user_id
                ),
            ),
        )
        return
    context = auth_service.get_context(target_user_id)
    roles = auth_service.available_role_changes(actor_user_id, target_user_id)
    await edit_or_send_text(
        message,
        _format_user(user, context.role),
        build_user_card_keyboard(
            target_id=target_user_id,
            current_role=context.role,
            allowed_roles=roles,
            back_role=back_role,
            back_page=back_page,
        ),
    )


@router.message(Command("users"))
async def users_command(
    message: Message,
    state: FSMContext,
    auth_service: AuthService,
) -> None:
    if not await _require_message(message, auth_service):
        return
    current = await state.get_state()
    if current and current.startswith("UserPickerStates:"):
        await message.answer(
            "Выбор пользователя отменён.",
            reply_markup=ReplyKeyboardRemove(),
        )
    await state.clear()
    await message.answer(
        f"{USERS_HEADER}\n\nВыберите действие:",
        reply_markup=build_user_management_keyboard(
            can_assign_admin=auth_service.can_assign_admin(message.from_user.id),
            can_assign_accountant=auth_service.can_assign_accountant(
                message.from_user.id
            ),
        ),
    )


@router.callback_query(UserAdminCallback.filter(F.action == "menu"))
async def users_menu(
    callback: CallbackQuery,
    state: FSMContext,
    auth_service: AuthService,
) -> None:
    if not await _require_callback(callback, auth_service):
        return
    current = await state.get_state()
    if (
        current
        and current.startswith("UserPickerStates:")
        and isinstance(callback.message, Message)
    ):
        await callback.message.answer(
            "Выбор пользователя отменён.",
            reply_markup=ReplyKeyboardRemove(),
        )
    await state.clear()
    if isinstance(callback.message, Message):
        await _show_menu(callback.message, auth_service, callback.from_user.id)


@router.callback_query(UserAdminCallback.filter(F.action == "cancel"))
async def users_cancel(
    callback: CallbackQuery,
    state: FSMContext,
    auth_service: AuthService,
) -> None:
    if not await _require_callback(callback, auth_service):
        return
    await state.clear()
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "Выбор пользователя отменён.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await _show_menu(callback.message, auth_service, callback.from_user.id)


@router.callback_query(
    UserAdminCallback.filter(F.action.in_({"pa", "pb", "pc"}))
)
async def picker_start(
    callback: CallbackQuery,
    callback_data: UserAdminCallback,
    state: FSMContext,
    auth_service: AuthService,
) -> None:
    if not await _require_callback(callback, auth_service):
        return
    new_role = {
        "pa": Role.ADMIN,
        "pb": Role.BARTENDER,
        "pc": Role.ACCOUNTANT,
    }[callback_data.action]
    if (
        new_role is Role.ADMIN
        and not auth_service.can_assign_admin(callback.from_user.id)
    ) or (
        new_role is Role.BARTENDER
        and not auth_service.can_assign_bartender(callback.from_user.id)
    ) or (
        new_role is Role.ACCOUNTANT
        and not auth_service.can_assign_accountant(callback.from_user.id)
    ):
        if isinstance(callback.message, Message):
            await callback.message.answer("Недостаточно прав")
        return
    if (
        not isinstance(callback.message, Message)
        or callback.message.chat.type != ChatType.PRIVATE
    ):
        if isinstance(callback.message, Message):
            await callback.message.answer(
                "Системный выбор пользователя доступен только в личном чате."
            )
        return
    request_id = secrets.randbelow(2_000_000_000) + 1
    await state.set_state(UserPickerStates.waiting)
    await state.set_data({"request_id": request_id, "new_role": new_role.value})
    await edit_or_send_text(
        callback.message,
        f"{USERS_HEADER}\n\nВыберите сотрудника через системную кнопку "
        "или из пользователей, уже запускавших бота.",
        build_picker_inline_keyboard(),
    )
    await callback.message.answer(
        "Нажмите «👤 Выбрать пользователя».",
        reply_markup=build_picker_reply_keyboard(request_id),
    )


@router.callback_query(UserAdminCallback.filter(F.action == "known"))
async def picker_known_users(
    callback: CallbackQuery,
    state: FSMContext,
    auth_service: AuthService,
    user_service: UserService,
) -> None:
    if not await _require_callback(callback, auth_service):
        return
    data = await state.get_data()
    if _parse_role(str(data.get("new_role", ""))) is None:
        if isinstance(callback.message, Message):
            await callback.message.answer("Сценарий выбора устарел.")
        return
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "Системная клавиатура скрыта.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await _show_list(
            callback.message,
            user_service,
            auth_service,
            role="all",
        )


@router.message(UserPickerStates.waiting, F.users_shared)
async def picker_users_shared(
    message: Message,
    state: FSMContext,
    auth_service: AuthService,
    user_service: UserService,
) -> None:
    if not await _require_message(message, auth_service):
        await message.answer("Клавиатура скрыта.", reply_markup=ReplyKeyboardRemove())
        await state.clear()
        return
    data = await state.get_data()
    shared = message.users_shared
    if (
        shared is None
        or shared.request_id != data.get("request_id")
        or len(shared.users) != 1
    ):
        await message.answer(
            "Не удалось проверить выбранного пользователя.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await state.clear()
        return
    selected = shared.users[0]
    new_role = _parse_role(str(data.get("new_role", "")))
    if new_role is None:
        await message.answer("Недостаточно прав", reply_markup=ReplyKeyboardRemove())
        await state.clear()
        return
    try:
        auth_service.require_role_change(
            message.from_user.id,
            selected.user_id,
            new_role,
        )
    except AccessDeniedError:
        await message.answer("Недостаточно прав", reply_markup=ReplyKeyboardRemove())
        await state.clear()
        return
    await message.answer(
        "Пользователь выбран.",
        reply_markup=ReplyKeyboardRemove(),
    )
    user = user_service.ensure_user(
        selected.user_id,
        username=selected.username,
        first_name=selected.first_name,
        last_name=selected.last_name,
        created_by=message.from_user.id,
    )
    await state.set_state(RoleChangeStates.confirm)
    await message.answer(
        _role_confirmation_text(user, auth_service, new_role),
        reply_markup=build_role_confirmation_keyboard(
            user.telegram_user_id,
            new_role,
        ),
    )


def _role_confirmation_text(
    user: UserRecord,
    auth_service: AuthService,
    new_role: Role,
) -> str:
    current_role = auth_service.get_context(user.telegram_user_id).role
    username = f"@{escape(user.username)}" if user.username else "отсутствует"
    return (
        f"{USERS_HEADER}\n\n"
        f"<b>Пользователь:</b> {username}\n"
        f"<b>Имя:</b> {escape(user.display_name)}\n"
        f"<b>Telegram ID:</b> <code>{user.telegram_user_id}</code>\n"
        f"<b>Текущая роль:</b> {ROLE_LABELS[current_role]}\n"
        f"<b>Новая роль:</b> {ROLE_LABELS[new_role]}\n\n"
        "Назначить роль?"
    )


@router.callback_query(UserListCallback.filter())
async def users_list(
    callback: CallbackQuery,
    callback_data: UserListCallback,
    auth_service: AuthService,
    user_service: UserService,
) -> None:
    if not await _require_callback(callback, auth_service):
        return
    if callback_data.role != "all" and _parse_role(callback_data.role) is None:
        return
    if isinstance(callback.message, Message):
        await _show_list(
            callback.message,
            user_service,
            auth_service,
            role=callback_data.role,
            page=callback_data.page,
        )


@router.callback_query(UserViewCallback.filter())
async def user_view(
    callback: CallbackQuery,
    callback_data: UserViewCallback,
    auth_service: AuthService,
    user_service: UserService,
) -> None:
    if not await _require_callback(callback, auth_service):
        return
    if isinstance(callback.message, Message):
        await _show_user_card(
            callback.message,
            user_service,
            auth_service,
            callback.from_user.id,
            callback_data.user_id,
            back_role=callback_data.role,
            back_page=callback_data.page,
        )


@router.callback_query(UserRoleCallback.filter(F.action == "ask"))
async def role_change_prompt(
    callback: CallbackQuery,
    callback_data: UserRoleCallback,
    state: FSMContext,
    auth_service: AuthService,
    user_service: UserService,
) -> None:
    if not await _require_callback(callback, auth_service):
        return
    role = _parse_role(callback_data.role)
    user = user_service.get_user(callback_data.user_id)
    if role is None or user is None:
        if isinstance(callback.message, Message):
            await callback.message.answer("Недостаточно прав")
        return
    try:
        auth_service.require_role_change(
            callback.from_user.id,
            callback_data.user_id,
            role,
        )
    except AccessDeniedError:
        if isinstance(callback.message, Message):
            await callback.message.answer("Недостаточно прав")
        return
    await state.set_state(RoleChangeStates.confirm)
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message,
            _role_confirmation_text(user, auth_service, role),
            build_role_confirmation_keyboard(callback_data.user_id, role),
        )


@router.callback_query(UserRoleCallback.filter(F.action == "confirm"))
async def role_change_confirm(
    callback: CallbackQuery,
    callback_data: UserRoleCallback,
    state: FSMContext,
    auth_service: AuthService,
    user_service: UserService,
    bot: Bot,
) -> None:
    if not await _require_callback(callback, auth_service):
        return
    role = _parse_role(callback_data.role)
    if role is None:
        return
    try:
        updated = auth_service.change_role(
            callback.from_user.id,
            callback_data.user_id,
            role,
        )
    except AccessDeniedError:
        if isinstance(callback.message, Message):
            await callback.message.answer("Недостаточно прав")
        return
    await state.clear()
    await update_user_commands(bot, updated.telegram_user_id, role)
    if isinstance(callback.message, Message):
        text = "✅ Роль сохранена."
        if not updated.has_started:
            text += (
                "\n\nПопросите сотрудника открыть бота и нажать /start."
            )
        await callback.message.answer(text)
        await _show_user_card(
            callback.message,
            user_service,
            auth_service,
            callback.from_user.id,
            updated.telegram_user_id,
        )


@router.callback_query(UserAdminCallback.filter(F.action == "search"))
async def user_search_start(
    callback: CallbackQuery,
    state: FSMContext,
    auth_service: AuthService,
) -> None:
    if not await _require_callback(callback, auth_service):
        return
    await state.set_state(UserSearchStates.query)
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message,
            f"{USERS_HEADER}\n\nВведите Telegram ID, локальный @username "
            "или часть имени:",
            build_user_cancel_keyboard(),
        )


@router.message(UserSearchStates.query)
async def user_search_query(
    message: Message,
    state: FSMContext,
    auth_service: AuthService,
    user_service: UserService,
) -> None:
    if not await _require_message(message, auth_service):
        return
    query = (message.text or "").strip()
    users = user_service.search_users(query)
    await state.clear()
    pairs = [
        (user, auth_service.get_context(user.telegram_user_id).role)
        for user in users
    ]
    text = f"{USERS_HEADER}\n\nРезультаты локального поиска: {len(pairs)}"
    await message.answer(
        text,
        reply_markup=build_users_list_keyboard(
            pairs,
            role="all",
            page=0,
            total_pages=1,
        ),
    )
