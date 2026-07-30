"""Start command, main-menu navigation, and global error handling."""

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ErrorEvent, Message

from app.handlers.common import edit_or_send_text
from app.keyboards.callbacks import MainCallback
from app.keyboards.main_menu import build_main_menu_keyboard

router = Router(name=__name__)
logger = logging.getLogger(__name__)

WELCOME_TEXT = (
    "<b>BarBot</b>\n\n"
    "Справочник для сотрудников бара. Выберите нужный раздел:"
)


@router.message(CommandStart())
async def handle_start(message: Message, state: FSMContext) -> None:
    """Show the main menu and reset an unfinished interaction."""

    await state.clear()
    await message.answer(
        WELCOME_TEXT,
        reply_markup=build_main_menu_keyboard(),
    )


@router.callback_query(MainCallback.filter(F.action == "home"))
async def handle_home(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """Return to the main menu."""

    await callback.answer()
    await state.clear()
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message,
            WELCOME_TEXT,
            build_main_menu_keyboard(),
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
