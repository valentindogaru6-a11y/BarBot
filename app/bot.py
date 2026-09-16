"""Long-polling entry point for the BarBot Telegram application."""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.config import PROJECT_ROOT, Settings
from app.handlers import (
    catalog,
    search,
    start,
    voice,
    warehouse,
    warehouse_closing,
    warehouse_recipients,
    warehouse_reminders,
    warehouse_reports,
)
from app.handlers.admin import router as admin_router
from app.middlewares.user_context import UserSyncMiddleware
from app.services.admin_service import AdminService
from app.services.audio_service import AudioService
from app.services.audit_service import AuditService
from app.services.auth_service import AuthService
from app.services.speech_service import SpeechService
from app.services.user_service import UserService
from app.services.voice_processing_service import VoiceProcessingService
from app.services.warehouse.operations_service import WarehouseOperationsService
from app.services.warehouse.closing_service import WarehouseClosingService
from app.services.warehouse.report_service import WarehouseReportService
from app.services.warehouse.delivery_service import WarehouseReportDeliveryService
from app.services.warehouse.recipient_service import WarehouseRecipientService
from app.services.warehouse.telegram_delivery import (
    TelegramChatVerifier,
    TelegramReportSender,
    TelegramReminderSender,
)
from app.services.warehouse.reminder_service import WarehouseReminderService
from app.services.warehouse.reminder_service import WarehouseReminderSettingsService
from app.storage.json_storage import JsonCatalogStorage
from app.storage.user_database import UserDatabase
from app.storage.warehouse_operations_database import WarehouseOperationsDatabase
from app.storage.warehouse_closing_database import WarehouseClosingDatabase
from app.storage.warehouse_reports_database import WarehouseReportsDatabase
from app.storage.warehouse_delivery_database import WarehouseDeliveryDatabase
from app.storage.warehouse_reminder_database import WarehouseReminderDatabase
from app.telegram_commands import configure_bot_commands


def router_names(*, warehouse_enabled: bool) -> tuple[str, ...]:
    names = [
        "app.handlers.start",
        "app.handlers.catalog",
        "app.handlers.voice",
        "app.handlers.search",
    ]
    if warehouse_enabled:
        names.append("app.handlers.warehouse")
        names.append("app.handlers.warehouse_recipients")
        names.append("app.handlers.warehouse_reminders")
        names.append("app.handlers.warehouse_closing")
        names.append("app.handlers.warehouse_reports")
    names.append("app.handlers.admin")
    return tuple(names)


def build_dispatcher(
    user_service: UserService,
    *,
    warehouse_enabled: bool = False,
) -> Dispatcher:
    """Build routers in conflict-safe order without starting Telegram polling."""

    dispatcher = Dispatcher()
    dispatcher.update.outer_middleware(UserSyncMiddleware(user_service))
    dispatcher.include_router(start.router)
    dispatcher.include_router(catalog.router)
    dispatcher.include_router(voice.router)
    dispatcher.include_router(search.router)
    if warehouse_enabled:
        dispatcher.include_router(warehouse.router)
        dispatcher.include_router(warehouse_recipients.router)
        dispatcher.include_router(warehouse_reminders.router)
        dispatcher.include_router(warehouse_closing.router)
        dispatcher.include_router(warehouse_reports.router)
    dispatcher.include_router(admin_router)
    dispatcher.errors.register(start.handle_unexpected_error)
    assert tuple(router.name for router in dispatcher.sub_routers) == router_names(
        warehouse_enabled=warehouse_enabled
    )
    return dispatcher


def start_reminder_worker(
    service: WarehouseReminderService | None,
) -> asyncio.Task[None] | None:
    """Create the optional worker only after the warehouse feature is enabled."""

    if service is None:
        return None
    return asyncio.create_task(service.run_forever())


async def stop_reminder_worker(task: asyncio.Task[None] | None) -> None:
    """Await cancellation so shutdown never leaves a reminder task behind."""

    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


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
    voice_processing_service = VoiceProcessingService(
        AudioService(),
        SpeechService(
            settings.vosk_model_path,
            language=settings.vosk_language,
        ),
        max_concurrent=settings.voice_max_concurrent,
    )
    bot = Bot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    warehouse_service = None
    warehouse_closing_service = None
    warehouse_report_service = None
    warehouse_recipient_service = None
    warehouse_delivery_service = None
    warehouse_chat_verifier = None
    warehouse_reminder_service = None
    warehouse_reminder_settings_service = None
    if settings.warehouse_enabled:
        warehouse_service = WarehouseOperationsService(
            WarehouseOperationsDatabase(settings.database_path),
            auth_service,
        )
        warehouse_closing_service = WarehouseClosingService(
            WarehouseClosingDatabase(settings.database_path),
            warehouse_service,
            auth_service,
        )
        warehouse_report_service = WarehouseReportService(
            WarehouseReportsDatabase(settings.database_path),
            warehouse_service,
            auth_service,
            settings.warehouse_reports_path,
        )
        warehouse_delivery_repository = WarehouseDeliveryDatabase(
            settings.database_path
        )
        warehouse_recipient_service = WarehouseRecipientService(
            warehouse_delivery_repository,
            auth_service,
        )
        warehouse_delivery_service = WarehouseReportDeliveryService(
            warehouse_delivery_repository,
            warehouse_report_service,
            auth_service,
            TelegramReportSender(bot),
        )
        warehouse_chat_verifier = TelegramChatVerifier(bot)
        warehouse_reminder_service = WarehouseReminderService(
            WarehouseReminderDatabase(settings.database_path),
            TelegramReminderSender(bot),
        )
        warehouse_reminder_settings_service = WarehouseReminderSettingsService(
            WarehouseReminderDatabase(settings.database_path), auth_service
        )
    dispatcher = build_dispatcher(
        user_service,
        warehouse_enabled=settings.warehouse_enabled,
    )

    reminder_task = None
    try:
        await configure_bot_commands(
            bot,
            auth_service,
            warehouse_enabled=settings.warehouse_enabled,
        )
        await bot.delete_webhook(drop_pending_updates=False)
        reminder_task = start_reminder_worker(warehouse_reminder_service)
        await dispatcher.start_polling(
            bot,
            catalog_path=settings.catalog_path,
            auth_service=auth_service,
            admin_service=admin_service,
            user_service=user_service,
            settings=settings,
            voice_processing_service=voice_processing_service,
            warehouse_enabled=settings.warehouse_enabled,
            warehouse_service=warehouse_service,
            warehouse_closing_service=warehouse_closing_service,
            warehouse_report_service=warehouse_report_service,
            warehouse_recipient_service=warehouse_recipient_service,
            warehouse_delivery_service=warehouse_delivery_service,
            warehouse_chat_verifier=warehouse_chat_verifier,
            warehouse_reminder_settings_service=warehouse_reminder_settings_service,
            close_bot_session=False,
        )
    finally:
        await stop_reminder_worker(reminder_task)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
