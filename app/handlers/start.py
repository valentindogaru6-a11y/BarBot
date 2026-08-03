"""Start command, main-menu navigation, and global error handling."""

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ErrorEvent, Message, ReplyKeyboardRemove

from app.handlers.common import edit_or_send_text
from app.keyboards.callbacks import MainCallback
from app.keyboards.main_menu import build_main_menu_keyboard
from app.middlewares.access import BLOCKED_TEXT
from app.models.user import Role
from app.services.auth_service import AuthService
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
    "🔍 <b>Поиск</b> — поиск по названию, ингредиенту или тегу.\n\n"
    "Основные команды также доступны через встроенное меню Telegram."
)


@router.message(CommandStart())
async def handle_start(
    message: Message,
    state: FSMContext,
    auth_service: AuthService,
    bot: Bot,
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
    await update_user_commands(bot, user.id, context.role)
    if context.role is Role.BLOCKED:
        await message.answer(BLOCKED_TEXT)
        return
    await message.answer(
        WELCOME_TEXT,
        reply_markup=build_main_menu_keyboard(
            is_admin=context.can_manage_catalog
        ),
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
    if user and not auth_service.can_view_catalog(user.id):
        await message.answer(BLOCKED_TEXT)
        return
    is_admin = bool(user and auth_service.is_admin(user.id))
    text = HELP_TEXT
    if is_admin:
        text += (
            "\n\n🛠 <b>Управление каталогом</b> доступно "
            "администраторам."
        )
    await message.answer(
        text,
        reply_markup=build_main_menu_keyboard(is_admin=is_admin),
    )


@router.callback_query(MainCallback.filter(F.action == "home"))
async def handle_home(
    callback: CallbackQuery,
    state: FSMContext,
    auth_service: AuthService,
) -> None:
    """Return to the main menu."""

    if not auth_service.can_view_catalog(callback.from_user.id):
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
            WELCOME_TEXT,
            build_main_menu_keyboard(
                is_admin=auth_service.is_admin(callback.from_user.id)
            ),
        )


async def handle_unexpected_error(event: ErrorEvent) -> bool:
    """Log unexpected errors and show a safe generic response."""

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
