"""Middleware that keeps known Telegram profiles current in SQLite."""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User

from app.services.user_service import UserService
from app.storage.user_database import UserDatabaseError

logger = logging.getLogger(__name__)


class UserSyncMiddleware(BaseMiddleware):
    def __init__(self, user_service: UserService) -> None:
        self.user_service = user_service

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if isinstance(user, User):
            try:
                self.user_service.sync_telegram_user(user)
            except UserDatabaseError:
                logger.exception("Could not synchronize Telegram user profile")
        return await handler(event, data)
