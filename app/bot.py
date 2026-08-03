"""Long-polling entry point for the BarBot Telegram application."""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    MenuButtonCommands,
)

from app.config import PROJECT_ROOT, Settings
from app.handlers import catalog, search, start
from app.handlers.admin import router as admin_router
from app.services.admin_service import AdminService
from app.services.audit_service import AuditService
from app.services.auth_service import AuthService
from app.storage.json_storage import JsonCatalogStorage

USER_COMMANDS = (
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="catalog", description="Открыть каталог"),
    BotCommand(command="search", description="Поиск по каталогу"),
    BotCommand(command="help", description="Помощь"),
)
ADMIN_COMMANDS = USER_COMMANDS + (
    BotCommand(command="admin", description="Управление каталогом"),
    BotCommand(command="myid", description="Показать мой Telegram ID"),
)


async def configure_bot_commands(
    bot: Bot,
    admin_ids: frozenset[int],
) -> None:
    """Register public commands and private administrator overrides."""

    await bot.set_my_commands(
        commands=list(USER_COMMANDS),
        scope=BotCommandScopeDefault(),
    )
    for admin_id in sorted(admin_ids):
        await bot.set_my_commands(
            commands=list(ADMIN_COMMANDS),
            scope=BotCommandScopeChat(chat_id=admin_id),
        )
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())


async def main() -> None:
    """Configure and run the bot with long polling."""

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    settings = Settings()
    auth_service = AuthService(settings.admin_ids)
    admin_service = AdminService(
        storage=JsonCatalogStorage(settings.catalog_path),
        audit=AuditService(PROJECT_ROOT / "logs" / "admin_audit.jsonl"),
    )
    bot = Bot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(start.router)
    dispatcher.include_router(catalog.router)
    dispatcher.include_router(search.router)
    dispatcher.include_router(admin_router)
    dispatcher.errors.register(start.handle_unexpected_error)

    try:
        await configure_bot_commands(bot, settings.admin_ids)
        await bot.delete_webhook(drop_pending_updates=False)
        await dispatcher.start_polling(
            bot,
            catalog_path=settings.catalog_path,
            auth_service=auth_service,
            admin_service=admin_service,
            close_bot_session=False,
        )
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
