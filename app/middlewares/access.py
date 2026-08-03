"""Central access filter for catalog and search routers."""

from typing import Any

from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.services.auth_service import AuthService

BLOCKED_TEXT = "Ваш доступ к боту ограничен. Обратитесь к администратору."


class CatalogAccessFilter(BaseFilter):
    async def __call__(
        self,
        event: TelegramObject,
        auth_service: AuthService,
    ) -> bool | dict[str, Any]:
        user = getattr(event, "from_user", None)
        if user is not None and auth_service.can_view_catalog(user.id):
            return True
        if isinstance(event, CallbackQuery):
            await event.answer(BLOCKED_TEXT, show_alert=True)
        elif isinstance(event, Message):
            await event.answer(BLOCKED_TEXT)
        return False
