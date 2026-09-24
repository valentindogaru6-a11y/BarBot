"""Start command, main-menu navigation, and global error handling."""

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardRemove,
)

from app.handlers.common import edit_or_send_text, is_expired_callback_error
from app.keyboards.callbacks import MainCallback
from app.keyboards.main_menu import build_main_menu_keyboard
from app.middlewares.access import BLOCKED_TEXT
from app.models.user import Role
from app.services.access_service import UserContext
from app.services.auth_service import AuthService
from app.services.warehouse.attendance_service import WarehouseAttendanceService
from app.telegram_commands import update_user_commands

router = Router(name=__name__)
logger = logging.getLogger(__name__)

WELCOME_TEXT = (
    "🍸 <b>BarBot</b>\n\n"
    "Быстрый справочник для команды бара. Здесь можно открыть "
    "технологические карты и найти позицию по названию, ингредиенту "
    "или тегу.\n\n"
    "Выберите действие:"
)
HELP_TEXT = (
    "<b>Помощь</b>\n\n"
    "Используйте inline-кнопки под сообщениями для всей навигации.\n\n"
    "📚 <b>Каталог</b> — категории, разделы и карточки позиций.\n"
    "🔍 <b>Поиск</b> — поиск по названию, ингредиенту или тегу.\n"
    "🎙 <b>Голосом</b> — отправьте название позиции голосовым сообщением.\n\n"
    "Основные команды также доступны через встроенное меню Telegram."
)
ACCOUNTANT_WELCOME_TEXT = (
    "🧾 <b>BarBot для бухгалтера</b>\n\n"
    "Доступ к каталогу и поиску для этой роли закрыт. Просмотр и выгрузка "
    "отчётов появятся после включения складского модуля.\n\n"
    "Сейчас доступны команды /start, /help и /myid."
)
ACCOUNTANT_HELP_TEXT = (
    "<b>Помощь</b>\n\n"
    "Для роли бухгалтера сейчас доступны /start, /help и /myid.\n"
    "Каталог, поиск и административные функции недоступны. Просмотр отчётов "
    "будет добавлен вместе со складским модулем."
)
ACCOUNTANT_WAREHOUSE_WELCOME_TEXT = (
    "🧾 <b>BarBot для бухгалтера</b>\n\n"
    "Доступен просмотр записей и итогов текущей смены. Изменение и удаление "
    "операций для этой роли закрыто.\n\n"
    "Выберите действие:"
)
ACCOUNTANT_WAREHOUSE_HELP_TEXT = (
    "<b>Помощь</b>\n\n"
    "Откройте «📦 Складские операции», чтобы посмотреть записи и итоги "
    "текущей смены. Добавление, изменение и удаление недоступны."
)


def _main_menu_for(
    context: UserContext,
    warehouse_enabled: bool = False,
    attendance_open: bool | None = None,
) -> InlineKeyboardMarkup | None:
    return build_main_menu_keyboard(
        can_view_catalog=context.can_view_catalog,
        can_manage_catalog=context.can_manage_catalog,
        can_view_warehouse=warehouse_enabled and context.can_view_warehouse,
        attendance_open=(attendance_open if warehouse_enabled and context.can_use_warehouse_attendance else None),
    )


async def _attendance_open(service: WarehouseAttendanceService | None, context: UserContext,
                           warehouse_enabled: bool) -> bool | None:
    if not warehouse_enabled or service is None or not context.can_use_warehouse_attendance:
        return None
    return await __import__("asyncio").to_thread(service.status, context.telegram_user_id) is not None


@router.message(CommandStart())
async def handle_start(
    message: Message,
    state: FSMContext,
    auth_service: AuthService,
    bot: Bot,
    warehouse_enabled: bool = False,
    warehouse_attendance_service: WarehouseAttendanceService | None = None,
) -> None:
    """Show the main menu and reset an unfinished interaction."""

    current_state = await state.get_state()
    if current_state and current_state.startswith("UserPickerStates:"):
        await message.answer(
            "Выбор пользователя отменён.",
            reply_markup=ReplyKeyboardRemove(),
        )
    await state.clear()
    user = message.from_user
    if user is None:
        return
    context = auth_service.get_context(user.id)
    await update_user_commands(
        bot, user.id, context.role, warehouse_enabled=warehouse_enabled
    )
    if context.role is Role.BLOCKED:
        await message.answer(BLOCKED_TEXT)
        return
    text = WELCOME_TEXT
    if context.role is Role.ACCOUNTANT:
        text = (
            ACCOUNTANT_WAREHOUSE_WELCOME_TEXT
            if warehouse_enabled
            else ACCOUNTANT_WELCOME_TEXT
        )
    await message.answer(
        text,
        reply_markup=_main_menu_for(context, warehouse_enabled, await _attendance_open(
            warehouse_attendance_service, context, warehouse_enabled)),
    )


@router.message(Command("myid"))
async def handle_myid(message: Message) -> None:
    """Show only the requesting user's own Telegram ID."""

    if message.from_user is not None:
        await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>")


@router.message(Command("help"))
async def handle_help(
    message: Message,
    state: FSMContext,
    auth_service: AuthService,
    warehouse_enabled: bool = False,
) -> None:
    """Show concise usage help and the role-aware main menu."""

    current_state = await state.get_state()
    if current_state and current_state.startswith("UserPickerStates:"):
        await message.answer(
            "Выбор пользователя отменён.",
            reply_markup=ReplyKeyboardRemove(),
        )
    await state.clear()
    user = message.from_user
    context = auth_service.get_context(user.id) if user else None
    if context and context.role is Role.BLOCKED:
        await message.answer(BLOCKED_TEXT)
        return
    if context and context.role is Role.ACCOUNTANT:
        if warehouse_enabled:
            await message.answer(
                ACCOUNTANT_WAREHOUSE_HELP_TEXT,
                reply_markup=_main_menu_for(context, warehouse_enabled),
            )
        else:
            await message.answer(ACCOUNTANT_HELP_TEXT)
        return
    text = HELP_TEXT
    if context and context.can_manage_catalog:
        text += (
            "\n\n🛠 <b>Управление каталогом</b> доступно "
            "администраторам."
        )
    await message.answer(
        text,
        reply_markup=(
            _main_menu_for(context, warehouse_enabled)
            if context is not None
            else build_main_menu_keyboard()
        ),
    )


@router.callback_query(MainCallback.filter(F.action == "home"))
async def handle_home(
    callback: CallbackQuery,
    state: FSMContext,
    auth_service: AuthService,
    warehouse_enabled: bool = False,
    warehouse_attendance_service: WarehouseAttendanceService | None = None,
) -> None:
    """Return to the main menu."""

    context = auth_service.get_context(callback.from_user.id)
    if context.role is Role.BLOCKED:
        await callback.answer(BLOCKED_TEXT, show_alert=True)
        return
    await callback.answer()
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
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message,
            (
                ACCOUNTANT_WAREHOUSE_WELCOME_TEXT
                if context.role is Role.ACCOUNTANT and warehouse_enabled
                else ACCOUNTANT_WELCOME_TEXT
                if context.role is Role.ACCOUNTANT
                else WELCOME_TEXT
            ),
            _main_menu_for(context, warehouse_enabled, await _attendance_open(
                warehouse_attendance_service, context, warehouse_enabled)),
        )


async def handle_unexpected_error(event: ErrorEvent) -> bool:
    """Log unexpected errors and show a safe generic response."""

    if is_expired_callback_error(event.exception):
        logger.info("Expired Telegram callback ignored")
        return True

    logger.error(
        "Unhandled update error: %s",
        type(event.exception).__name__,
        exc_info=(
            type(event.exception),
            event.exception,
            event.exception.__traceback__,
        ),
    )

    callback = event.update.callback_query
    message = event.update.message
    try:
        if callback is not None:
            await callback.answer(
                "Произошла ошибка. Попробуйте ещё раз.",
                show_alert=True,
            )
            message = (
                callback.message
                if isinstance(callback.message, Message)
                else None
            )
        if message is not None:
            await message.answer(
                "Произошла непредвиденная ошибка. Попробуйте позже.",
                reply_markup=build_main_menu_keyboard(),
            )
    except TelegramAPIError as exc:
        logger.error(
            "Could not send error response through Telegram API: %s",
            exc,
        )
    return True
