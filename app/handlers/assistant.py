"""Private super-admin handlers for the bounded AI assistant pilot."""

from __future__ import annotations

import logging
from html import escape
from pathlib import Path
from typing import Any

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import BaseFilter, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.config import Settings
from app.handlers.catalog import _show_item
from app.handlers.common import load_catalog
from app.handlers.warehouse import prepare_intake_message
from app.keyboards.catalog import build_search_results_keyboard
from app.models.user import Role
from app.services.assistant import (
    AssistantOrchestrator,
    AssistantRequestCoordinator,
    AssistantToolRegistry,
    AssistantUnavailableError,
    ProviderError,
    SpeechToTextClient,
)
from app.services.auth_service import AuthService
from app.services.audio_service import temporary_audio_files
from app.services.warehouse.intake_parser import is_warehouse_trigger_header
from app.services.warehouse.operations_service import WarehouseOperationsService
from app.storage.json_storage import CatalogStorageError

voice_router = Router(name="app.handlers.assistant_voice")
text_router = Router(name="app.handlers.assistant_text")
logger = logging.getLogger(__name__)

AI_UNAVAILABLE_TEXT = "AI-помощник временно недоступен. Попробуйте позже."
VOICE_UNAVAILABLE_TEXT = "Не удалось распознать голосовое. Попробуйте ещё раз или отправьте текст."
AI_REQUEST_BUSY_TEXT = (
    "⏳ Я ещё обрабатываю предыдущий запрос. Дождитесь результата."
)


def _private_super_admin(
    message: Message,
    auth_service: AuthService,
) -> bool:
    return bool(
        message.from_user is not None
        and message.chat.type == ChatType.PRIVATE
        and auth_service.get_context(message.from_user.id).role is Role.SUPER_ADMIN
    )


class AssistantVoiceEligibleFilter(BaseFilter):
    async def __call__(
        self,
        message: Message,
        settings: Settings,
        auth_service: AuthService,
        raw_state: str | None = None,
    ) -> bool:
        return bool(
            raw_state is None
            and settings.ai_assistant_enabled
            and settings.ai_voice_enabled
            and _private_super_admin(message, auth_service)
        )


class AssistantTextEligibleFilter(BaseFilter):
    async def __call__(
        self,
        message: Message,
        settings: Settings,
        auth_service: AuthService,
        raw_state: str | None = None,
    ) -> bool:
        text = (message.text or "").strip()
        if (
            raw_state is not None
            or not settings.ai_assistant_enabled
            or not settings.ai_text_enabled
            or not text
            or text.startswith("/")
            or not _private_super_admin(message, auth_service)
        ):
            return False
        first_line = next(
            (line.strip() for line in text.splitlines() if line.strip()),
            "",
        )
        return not is_warehouse_trigger_header(first_line)


async def _run_assistant(
    message: Message,
    state: FSMContext,
    *,
    text: str,
    catalog_path: Path,
    auth_service: AuthService,
    orchestrator: AssistantOrchestrator,
    warehouse_service: WarehouseOperationsService | None,
) -> None:
    try:
        catalog = load_catalog(catalog_path)
    except CatalogStorageError:
        logger.exception("Assistant catalog load failed")
        await message.answer("Каталог временно недоступен.")
        return
    registry = AssistantToolRegistry(catalog)
    try:
        intent = await orchestrator.interpret(
            text,
            candidates=registry.relevant_candidate_names(text),
        )
        action = registry.execute(intent)
    except AssistantUnavailableError:
        await message.answer(AI_UNAVAILABLE_TEXT)
        return
    except ProviderError as exc:
        logger.warning(
            "Assistant intent provider failed: provider=%s kind=%s",
            exc.provider,
            exc.kind.value,
        )
        await message.answer(AI_UNAVAILABLE_TEXT)
        return

    if action.kind == "find_tech_card":
        if len(action.search_results) == 1:
            result = action.search_results[0]
            await _show_item(
                message,
                catalog_path,
                result.category.id,
                result.item.id,
                result.subcategory.id if result.subcategory else None,
                is_admin=bool(
                    message.from_user
                    and auth_service.is_admin(message.from_user.id)
                ),
            )
        else:
            await message.answer(
                "Нашёл несколько подходящих техкарт. Выберите:",
                reply_markup=build_search_results_keyboard(
                    list(action.search_results)
                ),
            )
        return
    if action.kind in {"prepare_writeoff", "prepare_transfer"}:
        if warehouse_service is None or action.raw_intake is None:
            await message.answer("Складской модуль сейчас недоступен.")
            return
        await prepare_intake_message(
            message,
            state,
            warehouse_service,
            action.raw_intake,
        )
        return
    await message.answer(escape(action.message or AI_UNAVAILABLE_TEXT))


@voice_router.message(F.voice, AssistantVoiceEligibleFilter())
async def handle_assistant_voice(
    message: Message,
    state: FSMContext,
    bot: Bot,
    catalog_path: Path,
    auth_service: AuthService,
    settings: Settings,
    assistant_orchestrator: AssistantOrchestrator | None,
    assistant_speech_client: SpeechToTextClient | None,
    assistant_request_coordinator: AssistantRequestCoordinator,
    warehouse_service: WarehouseOperationsService | None = None,
) -> None:
    voice = message.voice
    if voice is None or assistant_speech_client is None or assistant_orchestrator is None:
        await message.answer(VOICE_UNAVAILABLE_TEXT)
        return
    if message.from_user is None:
        return
    async with assistant_request_coordinator.claim(
        message.from_user.id
    ) as acquired:
        if not acquired:
            await message.answer(AI_REQUEST_BUSY_TEXT)
            return
        await _handle_assistant_voice_claimed(
            message,
            state,
            bot,
            catalog_path,
            auth_service,
            settings,
            assistant_orchestrator,
            assistant_speech_client,
            warehouse_service,
        )


async def _handle_assistant_voice_claimed(
    message: Message,
    state: FSMContext,
    bot: Bot,
    catalog_path: Path,
    auth_service: AuthService,
    settings: Settings,
    assistant_orchestrator: AssistantOrchestrator,
    assistant_speech_client: SpeechToTextClient,
    warehouse_service: WarehouseOperationsService | None,
) -> None:
    """Run the voice pipeline while the caller owns the per-user claim."""

    voice = message.voice
    if voice is None:
        return
    if voice.duration > settings.ai_voice_max_duration_seconds:
        await message.answer(
            "Голосовое слишком длинное. "
            f"Максимум — {settings.ai_voice_max_duration_seconds} секунд."
        )
        return
    if voice.file_size is not None and voice.file_size > settings.ai_voice_max_file_bytes:
        await message.answer("Голосовой файл слишком большой.")
        return
    content_type = (voice.mime_type or "").casefold()
    if content_type not in {"audio/ogg", "audio/opus", "audio/ogg; codecs=opus"}:
        await message.answer("Поддерживаются только голосовые OGG/Opus.")
        return
    try:
        with temporary_audio_files() as files:
            await bot.download(voice, destination=files.source)
            size = files.source.stat().st_size
            if size <= 0 or size > settings.ai_voice_max_file_bytes:
                await message.answer(
                    "Голосовой файл пустой или превышает допустимый размер."
                )
                return
            transcript = await assistant_speech_client.transcribe(
                files.source.read_bytes(),
                content_type=content_type,
            )
    except ProviderError as exc:
        logger.warning(
            "Assistant voice provider failed: provider=%s kind=%s",
            exc.provider,
            exc.kind.value,
        )
        await message.answer(VOICE_UNAVAILABLE_TEXT)
        return
    except OSError:
        logger.exception("Assistant voice temporary-file processing failed")
        await message.answer(VOICE_UNAVAILABLE_TEXT)
        return
    transcript = " ".join(transcript.split())
    await message.answer(f"🎙 <b>Распознано:</b>\n«{escape(transcript)}»")
    if len(transcript) < 2:
        await message.answer("Не удалось понять запрос. Повторите голосовое или отправьте текст.")
        return
    await _run_assistant(
        message,
        state,
        text=transcript,
        catalog_path=catalog_path,
        auth_service=auth_service,
        orchestrator=assistant_orchestrator,
        warehouse_service=warehouse_service,
    )


@text_router.message(StateFilter(None), F.text, AssistantTextEligibleFilter())
async def handle_assistant_text(
    message: Message,
    state: FSMContext,
    catalog_path: Path,
    auth_service: AuthService,
    assistant_orchestrator: AssistantOrchestrator | None,
    assistant_request_coordinator: AssistantRequestCoordinator,
    warehouse_service: WarehouseOperationsService | None = None,
) -> None:
    if assistant_orchestrator is None or message.from_user is None:
        return
    async with assistant_request_coordinator.claim(
        message.from_user.id
    ) as acquired:
        if not acquired:
            await message.answer(AI_REQUEST_BUSY_TEXT)
            return
        await _run_assistant(
            message,
            state,
            text=message.text or "",
            catalog_path=catalog_path,
            auth_service=auth_service,
            orchestrator=assistant_orchestrator,
            warehouse_service=warehouse_service,
        )
