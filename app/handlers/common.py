"""Shared Telegram handler helpers."""

import logging
from pathlib import Path

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, Message

from app.models.catalog import Catalog
from app.storage.json_storage import (
    CatalogFileNotFoundError,
    CatalogJSONDecodeError,
    CatalogStorageError,
    CatalogValidationError,
    JsonCatalogStorage,
)

logger = logging.getLogger(__name__)


def load_catalog(path: Path) -> Catalog:
    """Load catalog data exclusively through the storage layer."""

    return JsonCatalogStorage(path).load()


def catalog_error_message(error: CatalogStorageError) -> str:
    """Map technical storage errors to short user-facing messages."""

    if isinstance(error, CatalogFileNotFoundError):
        return "Каталог временно недоступен: файл данных не найден."
    if isinstance(error, (CatalogJSONDecodeError, CatalogValidationError)):
        return "Каталог временно недоступен из-за ошибки в данных."
    return "Не удалось загрузить каталог. Попробуйте позже."


async def edit_or_send_text(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Edit a bot text message when possible, otherwise send a new one."""

    if (
        message.photo
        or message.from_user is None
        or not message.from_user.is_bot
    ):
        await message.answer(text, reply_markup=reply_markup)
        return

    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).casefold():
            return
        logger.warning(
            "Could not edit Telegram message; sending a new one: %s",
            exc,
        )
        await message.answer(text, reply_markup=reply_markup)
    except TelegramAPIError as exc:
        logger.warning(
            "Telegram API error while editing message; sending a new one: %s",
            exc,
        )
        await message.answer(text, reply_markup=reply_markup)
