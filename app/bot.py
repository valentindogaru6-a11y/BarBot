"""Long-polling entry point for the BarBot Telegram application."""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.config import PROJECT_ROOT, Settings
from app.handlers import (
    assistant,
    catalog,
    search,
    start,
    voice,
    warehouse,
    warehouse_attendance,
    warehouse_closing,
    warehouse_corrections,
    warehouse_recipients,
    warehouse_reminders,
    warehouse_schedules,
    warehouse_reports,
)
from app.handlers.admin import router as admin_router
from app.middlewares.user_context import UserSyncMiddleware
from app.services.admin_service import AdminService
from app.services.assistant import (
    AioHttpTransport,
    AssistantHttpTransport,
    AssistantOrchestrator,
    PersistentAssistantConversationMemory,
    AssistantRequestCoordinator,
    GeminiIntentClient,
    GeminiGroundedSearchClient,
    GroqSpeechToTextClient,
    AssistantActionProposalService,
    AssistantCapabilityController,
    CapabilityRegistry,
)
from app.services.audio_service import AudioService
from app.services.audit_service import AuditService
from app.services.auth_service import AuthService
from app.services.speech_service import SpeechService
from app.services.user_service import UserService
from app.services.voice_processing_service import VoiceProcessingService
from app.services.warehouse.operations_service import WarehouseOperationsService
from app.services.warehouse.nomenclature_service import WarehouseNomenclatureService
from app.services.warehouse.closing_service import WarehouseClosingService
from app.services.warehouse.report_service import WarehouseReportService
from app.services.warehouse.correction_service import WarehouseCorrectionService
from app.services.warehouse.delivery_service import WarehouseReportDeliveryService
from app.services.warehouse.recipient_service import WarehouseRecipientService
from app.services.warehouse.telegram_delivery import (
    TelegramChatVerifier,
    TelegramReportSender,
    TelegramReminderSender,
)
from app.services.warehouse.reminder_service import WarehouseReminderService
from app.services.warehouse.reminder_service import WarehouseReminderSettingsService
from app.services.warehouse.schedule_service import WarehouseScheduleService
from app.services.warehouse.attendance_service import WarehouseAttendanceService
from app.services.warehouse.attendance_xlsx_service import AttendanceReportCoordinator, WarehouseAttendanceXlsxService
from app.storage.json_storage import JsonCatalogStorage
from app.storage.user_database import UserDatabase
from app.storage.warehouse_operations_database import WarehouseOperationsDatabase
from app.storage.warehouse_database import WarehouseDatabase
from app.storage.warehouse_closing_database import WarehouseClosingDatabase
from app.storage.warehouse_reports_database import WarehouseReportsDatabase
from app.storage.warehouse_correction_database import WarehouseCorrectionDatabase
from app.storage.warehouse_delivery_database import WarehouseDeliveryDatabase
from app.storage.warehouse_reminder_database import WarehouseReminderDatabase
from app.storage.warehouse_schedule_database import WarehouseScheduleDatabase
from app.storage.warehouse_attendance_database import WarehouseAttendanceDatabase
from app.storage.assistant_conversation_database import AssistantConversationDatabase
from app.storage.assistant_action_database import AssistantActionDatabase
from app.telegram_commands import configure_bot_commands


def router_names(*, warehouse_enabled: bool) -> tuple[str, ...]:
    names = [
        "app.handlers.start",
        "app.handlers.catalog",
        "app.handlers.assistant_voice",
        "app.handlers.voice",
        "app.handlers.search",
    ]
    if warehouse_enabled:
        names.append("app.handlers.warehouse_attendance")
        names.append("app.handlers.warehouse")
        names.append("app.handlers.warehouse_recipients")
        names.append("app.handlers.warehouse_reminders")
        names.append("app.handlers.warehouse_schedules")
        names.append("app.handlers.warehouse_closing")
        names.append("app.handlers.warehouse_reports")
        names.append("app.handlers.warehouse_corrections")
    names.append("app.handlers.admin")
    names.append("app.handlers.assistant_text")
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
    dispatcher.include_router(assistant.voice_router)
    dispatcher.include_router(voice.router)
    dispatcher.include_router(search.router)
    if warehouse_enabled:
        dispatcher.include_router(warehouse_attendance.router)
        dispatcher.include_router(warehouse.router)
        dispatcher.include_router(warehouse_recipients.router)
        dispatcher.include_router(warehouse_reminders.router)
        dispatcher.include_router(warehouse_schedules.router)
        dispatcher.include_router(warehouse_closing.router)
        dispatcher.include_router(warehouse_reports.router)
        dispatcher.include_router(warehouse_corrections.router)
    dispatcher.include_router(admin_router)
    dispatcher.include_router(assistant.text_router)
    dispatcher.errors.register(start.handle_unexpected_error)
    assert tuple(router.name for router in dispatcher.sub_routers) == router_names(
        warehouse_enabled=warehouse_enabled
    )
    return dispatcher


def build_assistant_clients(
    settings: Settings,
    *,
    transport: AssistantHttpTransport | None = None,
) -> tuple[AssistantOrchestrator, GroqSpeechToTextClient | None]:
    """Build only the provider adapters enabled by the validated AI matrix."""

    speech_client = None
    intent_client = None
    search_client = None
    if settings.ai_assistant_enabled and (
        settings.ai_voice_enabled
        or settings.ai_text_enabled
        or settings.ai_general_chat_enabled
        or settings.ai_web_search_enabled
    ):
        provider_transport = transport or AioHttpTransport()
        if settings.ai_voice_enabled:
            assert settings.groq_api_key is not None
            speech_client = GroqSpeechToTextClient(
                provider_transport,
                api_key=settings.groq_api_key.get_secret_value(),
                model=settings.groq_stt_model,
                timeout_seconds=settings.ai_request_timeout_seconds,
            )
        assert settings.gemini_api_key is not None
        intent_client = GeminiIntentClient(
            provider_transport,
            api_key=settings.gemini_api_key.get_secret_value(),
            model=settings.gemini_fast_model,
            timeout_seconds=settings.ai_request_timeout_seconds,
        )
        if settings.ai_web_search_enabled:
            search_client = GeminiGroundedSearchClient(
                provider_transport,
                api_key=settings.gemini_api_key.get_secret_value(),
                model=settings.gemini_strong_model,
                timeout_seconds=settings.ai_request_timeout_seconds,
                max_provider_calls_per_update=(
                    settings.ai_web_search_max_provider_calls_per_update
                ),
            )
    return AssistantOrchestrator(
        intent_client,
        search_client=search_client,
        general_chat_enabled=settings.ai_general_chat_enabled,
        web_search_enabled=settings.ai_web_search_enabled,
    ), speech_client


def start_reminder_worker(
    service: WarehouseReminderService | None,
) -> asyncio.Task[None] | None:
    """Create the optional worker only after the warehouse feature is enabled."""

    if service is None:
        return None
    return asyncio.create_task(service.run_forever())


def start_schedule_worker(service: WarehouseScheduleService | None) -> asyncio.Task[None] | None:
    return asyncio.create_task(service.run_forever()) if service is not None else None


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
    assistant_orchestrator, assistant_speech_client = build_assistant_clients(
        settings
    )
    assistant_request_coordinator = AssistantRequestCoordinator()
    assistant_conversation_memory = PersistentAssistantConversationMemory(
        AssistantConversationDatabase(settings.database_path),
        ttl_seconds=settings.ai_conversation_ttl_seconds,
        max_turns=settings.ai_conversation_max_turns,
        max_chars=settings.ai_conversation_max_chars,
    )
    bot = Bot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    warehouse_service = None
    warehouse_nomenclature_service = None
    warehouse_closing_service = None
    warehouse_report_service = None
    warehouse_correction_service = None
    warehouse_recipient_service = None
    warehouse_delivery_service = None
    warehouse_chat_verifier = None
    warehouse_reminder_service = None
    warehouse_reminder_settings_service = None
    warehouse_schedule_service = None
    warehouse_attendance_service = None
    warehouse_attendance_xlsx_service = None
    warehouse_attendance_report_coordinator = None
    if settings.warehouse_enabled:
        warehouse_attendance_repository = WarehouseAttendanceDatabase(settings.database_path)
        warehouse_attendance_service = WarehouseAttendanceService(
            warehouse_attendance_repository, auth_service
        )
        warehouse_attendance_xlsx_service = WarehouseAttendanceXlsxService(
            warehouse_attendance_repository, auth_service, settings.warehouse_reports_path
        )
        warehouse_attendance_report_coordinator = AttendanceReportCoordinator()
        warehouse_service = WarehouseOperationsService(
            WarehouseOperationsDatabase(settings.database_path),
            auth_service,
        )
        warehouse_nomenclature_service = WarehouseNomenclatureService(
            WarehouseDatabase(settings.database_path),
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
        warehouse_correction_service = WarehouseCorrectionService(
            WarehouseCorrectionDatabase(settings.database_path),
            warehouse_service,
            auth_service,
            warehouse_report_service.xlsx,
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
        warehouse_schedule_service = WarehouseScheduleService(
            WarehouseScheduleDatabase(settings.database_path), warehouse_delivery_service,
            auth_service,
        )
    assistant_capability_registry = CapabilityRegistry()
    assistant_flags = {
        "ai_assistant_enabled": settings.ai_assistant_enabled,
        "warehouse_enabled": settings.warehouse_enabled,
    }
    assistant_action_proposals = AssistantActionProposalService(
        AssistantActionDatabase(settings.database_path),
        auth_service,
        assistant_capability_registry,
        flags=assistant_flags,
    )
    assistant_capability_controller = AssistantCapabilityController(
        assistant_capability_registry,
        assistant_action_proposals,
        auth_service,
        flags=assistant_flags,
        users=user_service,
        attendance=warehouse_attendance_service,
        operations=warehouse_service,
        closing=warehouse_closing_service,
        reports=warehouse_report_service,
        recipients=warehouse_recipient_service,
        reminders=warehouse_reminder_settings_service,
        schedules=warehouse_schedule_service,
    )
    dispatcher = build_dispatcher(
        user_service,
        warehouse_enabled=settings.warehouse_enabled,
    )

    reminder_task = None
    schedule_task = None
    try:
        await configure_bot_commands(
            bot,
            auth_service,
            warehouse_enabled=settings.warehouse_enabled,
        )
        await bot.delete_webhook(drop_pending_updates=False)
        reminder_task = start_reminder_worker(warehouse_reminder_service)
        schedule_task = start_schedule_worker(warehouse_schedule_service)
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
            warehouse_nomenclature_service=warehouse_nomenclature_service,
            warehouse_closing_service=warehouse_closing_service,
            warehouse_report_service=warehouse_report_service,
            warehouse_correction_service=warehouse_correction_service,
            warehouse_recipient_service=warehouse_recipient_service,
            warehouse_delivery_service=warehouse_delivery_service,
            warehouse_chat_verifier=warehouse_chat_verifier,
            warehouse_reminder_settings_service=warehouse_reminder_settings_service,
            warehouse_schedule_service=warehouse_schedule_service,
            warehouse_attendance_service=warehouse_attendance_service,
            warehouse_attendance_xlsx_service=warehouse_attendance_xlsx_service,
            warehouse_attendance_report_coordinator=warehouse_attendance_report_coordinator,
            assistant_orchestrator=assistant_orchestrator,
            assistant_speech_client=assistant_speech_client,
            assistant_request_coordinator=assistant_request_coordinator,
            assistant_conversation_memory=assistant_conversation_memory,
            assistant_action_proposals=assistant_action_proposals,
            assistant_capability_controller=assistant_capability_controller,
            close_bot_session=False,
        )
    finally:
        await stop_reminder_worker(reminder_task)
        await stop_reminder_worker(schedule_task)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
