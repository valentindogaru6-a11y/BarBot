"""Central access filter for catalog and search routers."""

from typing import Any

from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.models.user import Role
from app.services.auth_service import AuthService

BLOCKED_TEXT = "Ваш доступ к боту ограничен. Обратитесь к администратору."
CATALOG_ACCESS_DENIED_TEXT = "Каталог и поиск недоступны для вашей роли."


class CatalogAccessFilter(BaseFilter):
    async def __call__(
        self,
        event: TelegramObject,
        auth_service: AuthService,
    ) -> bool | dict[str, Any]:
        user = getattr(event, "from_user", None)
        text = BLOCKED_TEXT
        if user is not None:
            context = auth_service.get_context(user.id)
            if context.can_view_catalog:
                return True
            if context.role is Role.ACCOUNTANT:
                text = CATALOG_ACCESS_DENIED_TEXT
        if isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
        elif isinstance(event, Message):
            await event.answer(text)
        return False
