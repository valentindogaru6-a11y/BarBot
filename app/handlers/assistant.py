"""Private super-admin handlers for the bounded AI assistant pilot."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from html import escape
from pathlib import Path
from time import monotonic
from typing import Any

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import BaseFilter, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.config import Settings
from app.handlers.catalog import _show_item
from app.handlers.common import answer_callback_safely, load_catalog
from app.handlers.warehouse import prepare_intake_message
from app.keyboards.assistant import build_assistant_candidates_keyboard
from app.keyboards.callbacks import AssistantCandidateCallback
from app.models.user import Role
from app.services.assistant import (
    AssistantOrchestrator,
    AssistantConversationMemory,
    AssistantRequestCoordinator,
    AssistantToolRegistry,
    AssistantUnavailableError,
    ProviderError,
    SpeechToTextClient,
    extract_catalog_entity,
)
from app.services.assistant.retrieval import CatalogRetrieval
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
AI_CONTEXT_CANCELLED_TEXT = "Текущий AI-запрос отменён."


def _latency_bucket(elapsed: float) -> str:
    if elapsed < 0.25:
        return "lt_250ms"
    if elapsed < 1:
        return "lt_1s"
    if elapsed < 3:
        return "lt_3s"
    return "gte_3s"


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
    conversation_memory: AssistantConversationMemory | None = None,
) -> None:
    try:
        catalog = load_catalog(catalog_path)
    except CatalogStorageError:
        if conversation_memory is not None and message.from_user is not None:
            conversation_memory.clear(message.from_user.id)
        logger.exception("Assistant catalog load failed")
        await message.answer("Каталог временно недоступен.")
        return
    registry = AssistantToolRegistry(catalog)
    user_id = message.from_user.id if message.from_user is not None else None
    normalized_command = " ".join(text.casefold().split())
    if normalized_command in {"отмена", "отмени", "cancel"}:
        if conversation_memory is not None and user_id is not None:
            conversation_memory.clear(user_id)
        await message.answer(AI_CONTEXT_CANCELLED_TEXT)
        return
    context = (
        conversation_memory.get(user_id)
        if conversation_memory is not None and user_id is not None
        else None
    )
    request_text = text
    if context is not None:
        if context.last_entity_type == "tech_card":
            request_text = f"покажи техкарту {context.last_query} {text}"
        elif context.last_entity_type == "warehouse_product":
            verb = (
                "спиши"
                if context.last_intent == "prepare_writeoff"
                else "перемести"
            )
            request_text = f"{verb} {context.last_query} {text}"

    local_exact_hit = False
    provider_latency_bucket = "local"
    intent = None
    try:
        local_intent = orchestrator.local_intent(request_text)
        retrieval = (
            CatalogRetrieval(normalized_query=context.last_query)
            if context is not None
            and context.last_entity_type == "warehouse_product"
            else registry.retriever.retrieve(request_text)
        )
        if local_intent is None and retrieval.exact:
            local_exact_hit = True
            intent = await orchestrator.interpret(
                f"покажи техкарту {retrieval.normalized_query}"
            )
        else:
            provider_candidates = tuple(
                candidate.provider_label for candidate in retrieval.candidates
            )
            started = monotonic()
            intent = await orchestrator.interpret(
                request_text,
                candidates=provider_candidates,
                normalized_entity=retrieval.normalized_query,
            )
            if local_intent is None:
                provider_latency_bucket = _latency_bucket(monotonic() - started)
        action = registry.execute(intent)

        if (
            local_intent is None
            and retrieval.candidates
            and action.kind == "clarify"
        ):
            candidate_action = registry.execute(
                type(intent)(
                    intent="find_tech_card",
                    confidence=10_000,
                    query=retrieval.normalized_query,
                )
            )
            action = replace(candidate_action, message=action.message)

        if (
            local_intent is None
            and retrieval.candidates
            and intent.intent.value == "find_tech_card"
            and action.catalog_exact
        ):
            allowed = {candidate.key for candidate in retrieval.candidates}
            if (
                len(allowed) != 1
                or len(action.candidate_ids) != 1
                or action.candidate_ids[0] not in allowed
            ):
                action = registry.execute(
                    type(intent)(
                        intent="find_tech_card",
                        confidence=10_000,
                        query=retrieval.normalized_query,
                    )
                )

        if (
            intent.intent.value == "find_tech_card"
            and not action.catalog_exact
            and action.search_results
            and local_intent is not None
        ):
            original_action = action
            provider_candidates = tuple(
                candidate.provider_label for candidate in retrieval.candidates
            )
            started = monotonic()
            refined = await orchestrator.interpret_with_provider(
                text,
                candidates=provider_candidates,
                normalized_entity=action.entity_query or retrieval.normalized_query,
            )
            provider_latency_bucket = _latency_bucket(monotonic() - started)
            refined_action = registry.execute(refined)
            if refined.intent.value == "clarify":
                intent = refined
                action = replace(original_action, message=refined_action.message)
            elif refined.intent.value == "not_found":
                intent, action = refined, refined_action
            elif (
                refined.intent.value == "find_tech_card"
                and refined_action.catalog_exact
                and len(original_action.candidate_ids) == 1
                and refined_action.candidate_ids[0]
                in original_action.candidate_ids
            ):
                intent, action = refined, refined_action
    except AssistantUnavailableError:
        if conversation_memory is not None and user_id is not None:
            conversation_memory.clear(user_id)
        await message.answer(AI_UNAVAILABLE_TEXT)
        return
    except ProviderError as exc:
        if conversation_memory is not None and user_id is not None:
            conversation_memory.clear(user_id)
        logger.warning(
            "Assistant intent provider failed: provider=%s kind=%s",
            exc.provider,
            exc.kind.value,
        )
        await message.answer(AI_UNAVAILABLE_TEXT)
        return
    except asyncio.CancelledError:
        if conversation_memory is not None and user_id is not None:
            conversation_memory.clear(user_id)
        raise
    except Exception:
        if conversation_memory is not None and user_id is not None:
            conversation_memory.clear(user_id)
        raise

    logger.info(
        "Assistant decision intent=%s local_exact_hit=%s candidate_count=%d "
        "clarification_required=%s tool_result=%s provider_latency_bucket=%s",
        intent.intent.value,
        local_exact_hit,
        len(action.search_results),
        action.kind == "clarify",
        action.kind,
        provider_latency_bucket,
    )

    if action.kind == "find_tech_card":
        if action.catalog_exact and len(action.search_results) == 1:
            result = action.search_results[0]
            if conversation_memory is not None and user_id is not None:
                conversation_memory.clear(user_id)
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
            if conversation_memory is not None and user_id is not None:
                conversation_memory.remember(
                    user_id,
                    last_intent="find_tech_card",
                    last_entity_type="tech_card",
                    last_query=action.entity_query
                    or extract_catalog_entity(request_text),
                    candidate_ids=action.candidate_ids,
                    pending_slots=("candidate",),
                )
            await message.answer(
                action.message or "Возможно, вы имели в виду:",
                reply_markup=build_assistant_candidates_keyboard(
                    action.search_results
                ),
            )
        return
    if action.kind in {"prepare_writeoff", "prepare_transfer"}:
        if warehouse_service is None or action.raw_intake is None:
            if conversation_memory is not None and user_id is not None:
                conversation_memory.clear(user_id)
            await message.answer("Складской модуль сейчас недоступен.")
            return
        if conversation_memory is not None and user_id is not None:
            conversation_memory.clear(user_id)
        await prepare_intake_message(
            message,
            state,
            warehouse_service,
            action.raw_intake,
        )
        return
    if (
        action.kind == "clarify"
        and action.pending_slots
        and action.entity_query
        and conversation_memory is not None
        and user_id is not None
    ):
        conversation_memory.remember(
            user_id,
            last_intent=intent.intent.value,
            last_entity_type="warehouse_product",
            last_query=action.entity_query,
            pending_slots=action.pending_slots,
        )
    elif action.kind == "clarify" and context is not None:
        if conversation_memory is not None and user_id is not None:
            conversation_memory.refresh(user_id)
    elif conversation_memory is not None and user_id is not None:
        conversation_memory.clear(user_id)
    await message.answer(escape(action.message or AI_UNAVAILABLE_TEXT))


@voice_router.callback_query(
    StateFilter(None), AssistantCandidateCallback.filter()
)
async def handle_assistant_candidate(
    callback: CallbackQuery,
    callback_data: AssistantCandidateCallback,
    catalog_path: Path,
    auth_service: AuthService,
    assistant_conversation_memory: AssistantConversationMemory,
) -> None:
    """Resolve a bounded candidate callback against current local catalog."""

    if (
        not isinstance(callback.message, Message)
        or callback.message.chat.type != ChatType.PRIVATE
        or auth_service.get_context(callback.from_user.id).role
        is not Role.SUPER_ADMIN
    ):
        await answer_callback_safely(callback, "Недостаточно прав.", show_alert=True)
        return
    subcategory_id = callback_data.subcategory_id
    key = "/".join(
        (callback_data.category_id, subcategory_id, callback_data.item_id)
    )
    context = assistant_conversation_memory.get(callback.from_user.id)
    if context is None or key not in context.candidate_ids:
        await answer_callback_safely(
            callback, "Этот выбор устарел. Повторите запрос.", show_alert=True
        )
        return
    try:
        catalog = load_catalog(catalog_path)
    except CatalogStorageError:
        assistant_conversation_memory.clear(callback.from_user.id)
        await answer_callback_safely(
            callback, "Каталог временно недоступен.", show_alert=True
        )
        return
    result = AssistantToolRegistry(catalog).retriever.by_key(key)
    if result is None:
        assistant_conversation_memory.clear(callback.from_user.id)
        await answer_callback_safely(
            callback, "Карточка больше недоступна.", show_alert=True
        )
        return
    if not await answer_callback_safely(callback):
        return
    assistant_conversation_memory.clear(callback.from_user.id)
    await _show_item(
        callback.message,
        catalog_path,
        result.category.id,
        result.item.id,
        result.subcategory.id if result.subcategory else None,
        is_admin=auth_service.is_admin(callback.from_user.id),
    )


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
    assistant_conversation_memory: AssistantConversationMemory | None = None,
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
            assistant_conversation_memory,
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
    assistant_conversation_memory: AssistantConversationMemory | None,
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
        if assistant_conversation_memory is not None and message.from_user is not None:
            assistant_conversation_memory.clear(message.from_user.id)
        logger.warning(
            "Assistant voice provider failed: provider=%s kind=%s",
            exc.provider,
            exc.kind.value,
        )
        await message.answer(VOICE_UNAVAILABLE_TEXT)
        return
    except asyncio.CancelledError:
        if assistant_conversation_memory is not None and message.from_user is not None:
            assistant_conversation_memory.clear(message.from_user.id)
        raise
    except OSError:
        if assistant_conversation_memory is not None and message.from_user is not None:
            assistant_conversation_memory.clear(message.from_user.id)
        logger.exception("Assistant voice temporary-file processing failed")
        await message.answer(VOICE_UNAVAILABLE_TEXT)
        return
    transcript = " ".join(transcript.split())
    await message.answer(f"🎙 <b>Распознано:</b>\n«{escape(transcript)}»")
    if len(transcript) < 2:
        if assistant_conversation_memory is not None and message.from_user is not None:
            assistant_conversation_memory.clear(message.from_user.id)
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
        conversation_memory=assistant_conversation_memory,
    )


@text_router.message(StateFilter(None), F.text, AssistantTextEligibleFilter())
async def handle_assistant_text(
    message: Message,
    state: FSMContext,
    catalog_path: Path,
    auth_service: AuthService,
    assistant_orchestrator: AssistantOrchestrator | None,
    assistant_request_coordinator: AssistantRequestCoordinator,
    assistant_conversation_memory: AssistantConversationMemory | None = None,
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
            conversation_memory=assistant_conversation_memory,
        )
