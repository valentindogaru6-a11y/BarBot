"""Long-polling entry point for the BarBot Telegram application."""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.config import PROJECT_ROOT, Settings
from app.handlers import catalog, search, start
from app.handlers.admin import router as admin_router
from app.middlewares.user_context import UserSyncMiddleware
from app.services.admin_service import AdminService
from app.services.audit_service import AuditService
from app.services.auth_service import AuthService
from app.services.user_service import UserService
from app.storage.json_storage import JsonCatalogStorage
from app.storage.user_database import UserDatabase
from app.telegram_commands import configure_bot_commands


async def main() -> None:
    """Configure and run the bot with long polling."""

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    settings = Settings()
    audit_service = AuditService(PROJECT_ROOT / "logs" / "admin_audit.jsonl")
    user_database = UserDatabase(settings.database_path)
    user_database.initialize()
    user_service = UserService(user_database, audit_service)
    auth_service = AuthService(
        settings.effective_super_admin_ids,
        user_service,
        audit_service,
    )
    admin_service = AdminService(
        storage=JsonCatalogStorage(settings.catalog_path),
        audit=audit_service,
    )
    bot = Bot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher()
    dispatcher.update.outer_middleware(UserSyncMiddleware(user_service))
    dispatcher.include_router(start.router)
    dispatcher.include_router(catalog.router)
    dispatcher.include_router(search.router)
    dispatcher.include_router(admin_router)
    dispatcher.errors.register(start.handle_unexpected_error)

    try:
        await configure_bot_commands(bot, auth_service)
        await bot.delete_webhook(drop_pending_updates=False)
        await dispatcher.start_polling(
            bot,
            catalog_path=settings.catalog_path,
            auth_service=auth_service,
            admin_service=admin_service,
            user_service=user_service,
            close_bot_session=False,
        )
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
