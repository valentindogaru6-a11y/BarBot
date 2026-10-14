"""Private super-admin handlers for the bounded AI assistant pilot."""

from __future__ import annotations

import asyncio
import logging
import re
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
from app.keyboards.assistant import (
    build_assistant_candidates_keyboard,
    build_assistant_flow_keyboard,
    build_assistant_proposal_keyboard,
)
from app.keyboards.callbacks import AssistantCandidateCallback, AssistantProposalCallback
from app.keyboards.main_menu import build_main_menu_keyboard
from app.models.assistant import (
    AssistantDomain,
    AssistantIntent,
    AssistantIntentName,
    AssistantRelation,
    AssistantRequestedKind,
)
from app.services.assistant import (
    AssistantAccessDenied,
    AssistantAccessPolicy,
    AssistantOrchestrator,
    AssistantConversationMemory,
    AssistantRequestCoordinator,
    AssistantToolRegistry,
    AssistantUnavailableError,
    ProviderError,
    ProviderErrorKind,
    SpeechToTextClient,
    AssistantActionProposalService,
    AssistantCapabilityController,
    extract_catalog_entity,
    capability_ids_for_domain,
    context_reference_relation,
    has_context_reference,
    parse_bar_request,
)
from app.services.assistant.proposals import (
    AssistantProposalError,
    AssistantProposalExpired,
    AssistantProposalPermissionDenied,
    AssistantProposalTargetConflict,
)
from app.services.assistant.retrieval import CatalogRetrieval
from app.services.assistant.tools import AssistantReadOnlyTools
from app.services.auth_service import AuthService
from app.services.access_service import AccessDeniedError
from app.services.audio_service import temporary_audio_files
from app.services.warehouse.intake_parser import is_warehouse_trigger_header
from app.services.warehouse.operations_service import WarehouseOperationsService
from app.services.warehouse.attendance_service import WarehouseAttendanceService
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
AI_CONTEXT_RESET_TEXT = "Контекст разговора очищен. Начнём заново."
AI_RATE_LIMIT_TEXT = "Лимит AI временно исчерпан. Попробуйте немного позже."
WEB_SEARCH_UNAVAILABLE_TEXT = (
    "Актуальный поиск в интернете пока недоступен. Могу ответить по общим "
    "знаниям или помочь с данными BarBot."
)
_CONVERSATION_RESET_PHRASES = {
    "забудь этот разговор",
    "начнем заново",
    "начнём заново",
    "сбрось контекст",
    "начать новый разговор",
}
_OPEN_SESSIONS_RE = re.compile(
    r"\b(?:чьи|какие|открыт\w*)\b.*\b(?:смен\w*|сесси\w*)\b|"
    r"\b(?:смен\w*|сесси\w*)\b.*\b(?:открыт\w*|до сих пор)\b",
    re.IGNORECASE,
)
_IMAGE_FOLLOWUP_RE = re.compile(r"\b(?:картинк\w*|изображени\w*|фото)\b", re.IGNORECASE)
_OFFLINE_FOLLOWUP_RE = re.compile(r"\b(?:без\s+интернет\w*|выдай\s+что\s+знаешь)\b", re.IGNORECASE)
_CANDIDATE_ORDINAL_RE = re.compile(
    r"^\s*(?:покажи\s+)?(?P<ordinal>перв\w*|втор\w*|трет\w*|четверт\w*|пят\w*)\s*[?!.]*\s*$",
    re.IGNORECASE,
)
_CARD_CONTENT_RE = re.compile(
    r"\b(?:что\s+входит|состав|ингредиент\w*|как\s+(?:делать|готовить))\b",
    re.IGNORECASE,
)
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(https?://[^)\s]+\)", re.IGNORECASE)
_BARE_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def _without_model_links(text: str) -> str:
    """Keep links out of model prose; citations come from grounding metadata."""

    without_markdown = _MARKDOWN_LINK_RE.sub(r"\1", text)
    return " ".join(_BARE_URL_RE.sub("", without_markdown).split())


def resolve_contextual_request(
    text: str,
    context: Any | None,
) -> tuple[str, bool]:
    """Resolve only high-confidence references; otherwise request clarification."""

    if context is None:
        return text, has_context_reference(text)
    if _OFFLINE_FOLLOWUP_RE.search(text) and context.topic:
        return (
            f"Расскажи по общим знаниям без поиска в интернете: {context.topic}. {text}",
            False,
        )
    if has_context_reference(text):
        if context.current_candidates and context.pending_clarification == "candidate":
            return text, True
        if not context.last_successful_entity_id:
            return text, True
    return text, False


async def _answer_safe_text(
    message: Message,
    text: str,
    *,
    limit: int = 3500,
) -> None:
    """Send escaped text in bounded Telegram-safe chunks."""

    cleaned = text.strip()
    while cleaned:
        candidate = cleaned[:limit]
        while len(escape(candidate)) > limit and len(candidate) > 1:
            candidate = candidate[: max(1, int(len(candidate) * 0.8))]
        if len(candidate) == len(cleaned):
            chunk, cleaned = candidate, ""
        else:
            split_at = candidate.rfind("\n")
            if split_at < limit // 2:
                split_at = candidate.rfind(" ")
            if split_at < limit // 2:
                split_at = len(candidate)
            chunk, cleaned = cleaned[:split_at], cleaned[split_at:].lstrip()
        await message.answer(escape(chunk))


def _latency_bucket(elapsed: float) -> str:
    if elapsed < 0.25:
        return "lt_250ms"
    if elapsed < 1:
        return "lt_1s"
    if elapsed < 3:
        return "lt_3s"
    return "gte_3s"


def _provider_error_text(error: ProviderError) -> str:
    if error.kind is ProviderErrorKind.RATE_LIMIT:
        return AI_RATE_LIMIT_TEXT
    if error.kind in {ProviderErrorKind.TIMEOUT, ProviderErrorKind.SERVER, ProviderErrorKind.UNAVAILABLE}:
        return "Gemini временно недоступен. Локальные функции BarBot продолжают работать."
    return AI_UNAVAILABLE_TEXT


class AssistantVoiceEligibleFilter(BaseFilter):
    async def __call__(
        self,
        message: Message,
        settings: Settings,
        assistant_access_policy: AssistantAccessPolicy,
        raw_state: str | None = None,
    ) -> bool:
        return bool(
            raw_state is None
            and settings.ai_assistant_enabled
            and settings.ai_voice_enabled
            and message.from_user is not None
            and assistant_access_policy.can_enter_assistant(
                message.from_user.id,
                chat_type=message.chat.type,
            )
        )


class AssistantTextEligibleFilter(BaseFilter):
    async def __call__(
        self,
        message: Message,
        settings: Settings,
        assistant_access_policy: AssistantAccessPolicy,
        raw_state: str | None = None,
    ) -> bool:
        text = (message.text or "").strip()
        if (
            raw_state is not None
            or not settings.ai_assistant_enabled
            or not settings.ai_text_enabled
            or not text
            or text.startswith("/")
            or message.from_user is None
            or not assistant_access_policy.can_enter_assistant(
                message.from_user.id,
                chat_type=message.chat.type,
            )
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
    warehouse_attendance_service: WarehouseAttendanceService | None = None,
    assistant_capability_controller: AssistantCapabilityController | None = None,
    assistant_access_policy: AssistantAccessPolicy | None = None,
) -> None:
    user_id = message.from_user.id if message.from_user is not None else None
    if user_id is None:
        return
    # Runtime entry points always inject and recheck the unified policy. Keeping
    # this private execution helper independently testable avoids coupling pure
    # routing regressions to the full user repository composition.
    if assistant_access_policy is not None:
        try:
            assistant_access_policy.require_assistant_access(
                user_id,
                chat_type=message.chat.type,
            )
        except AssistantAccessDenied:
            return

    async def send_capability(dispatch: Any) -> None:
        if conversation_memory is not None and user_id is not None:
            conversation_memory.append_turn(
                user_id,
                chat_id=message.chat.id,
                role="user",
                text=text,
                last_intent=dispatch.capability_id,
                topic=dispatch.capability_id,
            )
            conversation_memory.append_turn(
                user_id,
                chat_id=message.chat.id,
                role="assistant",
                text=dispatch.message,
                last_intent=dispatch.capability_id,
            )
        await message.answer(
            escape(dispatch.message),
            reply_markup=(
                build_assistant_proposal_keyboard(dispatch.proposal.token)
                if dispatch.proposal is not None
                else build_main_menu_keyboard(
                    can_view_catalog=auth_service.can_view_catalog(user_id),
                    can_manage_catalog=auth_service.can_manage_catalog(user_id),
                    can_view_warehouse=auth_service.can_view_warehouse(user_id),
                    attendance_open=(
                        warehouse_attendance_service.status(user_id) is not None
                        if warehouse_attendance_service is not None
                        and auth_service.can_use_warehouse_attendance(user_id)
                        else None
                    ),
                ) if dispatch.ui_action == "main_menu"
                else build_assistant_flow_keyboard(dispatch.ui_action, dispatch.data)
                if dispatch.ui_action is not None
                else None
            ),
        )

    if assistant_capability_controller is not None and user_id is not None:
        dispatch = assistant_capability_controller.dispatch(
            text,
            actor_user_id=user_id,
            source_chat_id=message.chat.id,
            source_message_id=int(getattr(message, "message_id", 0)),
        )
        if dispatch is not None:
            await send_capability(dispatch)
            return
    try:
        catalog = load_catalog(catalog_path)
    except CatalogStorageError:
        if conversation_memory is not None and message.from_user is not None:
            conversation_memory.clear(message.from_user.id, message.chat.id)
        logger.exception("Assistant catalog load failed")
        await message.answer("Каталог временно недоступен.")
        return
    registry = AssistantToolRegistry(catalog)
    normalized_command = " ".join(text.casefold().split())
    if normalized_command in _CONVERSATION_RESET_PHRASES:
        if conversation_memory is not None and user_id is not None:
            conversation_memory.clear(user_id, message.chat.id)
        await message.answer(AI_CONTEXT_RESET_TEXT)
        return
    if normalized_command in {"отмена", "отмени", "cancel"}:
        if conversation_memory is not None and user_id is not None:
            conversation_memory.clear(user_id, message.chat.id)
        await message.answer(AI_CONTEXT_CANCELLED_TEXT)
        return
    context = (
        conversation_memory.get(user_id, message.chat.id)
        if conversation_memory is not None and user_id is not None
        else None
    )
    if (
        context is not None
        and context.pending_slots == ("reference",)
        and conversation_memory is not None
        and user_id is not None
    ):
        conversation_memory.clear_structured(
            user_id, chat_id=message.chat.id,
            last_intent="clarification_answer", topic=text
        )
        context = conversation_memory.get(user_id, message.chat.id)
    if user_id is not None and _OPEN_SESSIONS_RE.search(text):
        tools = AssistantReadOnlyTools(
            catalog,
            auth_service,
            attendance=warehouse_attendance_service,
            operations=warehouse_service,
        )
        try:
            sessions = tools.list_open_attendance_sessions(user_id)
        except AccessDeniedError:
            await message.answer("Недостаточно прав для просмотра открытых смен.")
            return
        except Exception:
            logger.exception("Assistant read-only attendance tool failed")
            await message.answer("Не удалось прочитать открытые смены. Попробуйте позже.")
            return
        if sessions:
            lines = ["Сейчас открыты персональные смены:"]
            for session in sessions:
                hours, remainder = divmod(session.duration_seconds, 3600)
                minutes = remainder // 60
                label = session.employee
                if session.username:
                    label += f" (@{session.username})"
                lines.append(
                    f"• {label} — {session.location}, {hours} ч {minutes} мин"
                )
            answer = "\n".join(lines)
        else:
            answer = "Открытых персональных смен сейчас нет."
        if conversation_memory is not None:
            conversation_memory.append_turn(user_id, chat_id=message.chat.id, role="user", text=text, last_intent="list_open_attendance_sessions", topic="открытые смены")
            conversation_memory.append_turn(user_id, chat_id=message.chat.id, role="assistant", text=answer, last_intent="list_open_attendance_sessions")
            conversation_memory.remember(
                user_id, chat_id=message.chat.id,
                last_intent="list_open_attendance_sessions",
                last_entity_type="attendance", last_query="открытые смены",
                topic="открытые смены", last_tool_name="list_open_attendance_sessions",
            )
        await _answer_safe_text(message, answer)
        return
    if context is not None and _IMAGE_FOLLOWUP_RE.search(text):
        answer = (
            "Сейчас я не умею присылать изображения. "
            f"Могу продолжить по теме «{context.topic or context.last_query}» текстом."
        )
        if conversation_memory is not None and user_id is not None:
            conversation_memory.append_turn(user_id, chat_id=message.chat.id, role="user", text=text, last_intent="unsupported")
            conversation_memory.append_turn(user_id, chat_id=message.chat.id, role="assistant", text=answer, last_intent="unsupported")
        await _answer_safe_text(message, answer)
        return
    allowed_action = orchestrator.local_action_evidence(text)
    if (
        allowed_action is None
        and context is not None
        and context.authorized_action
        in {
            AssistantIntentName.PREPARE_WRITEOFF.value,
            AssistantIntentName.PREPARE_TRANSFER.value,
        }
    ):
        allowed_action = AssistantIntentName(context.authorized_action)
    request_text = text
    provider_history = (
        conversation_memory.provider_history(user_id, message.chat.id)
        if conversation_memory is not None and user_id is not None
        else ()
    )
    if context is not None:
        compact_context = "; ".join(
            part for part in (
                f"тема: {context.topic}" if context.topic else "",
                f"последний запрос каталога: {context.last_query}" if context.last_query else "",
                f"последнее действие: {context.last_requested_action}" if context.last_requested_action else "",
                f"summary: {context.summary_text}" if context.summary_text else "",
            ) if part
        )[:1000]
        if compact_context:
            provider_history = (("user", f"[Рабочий контекст: {compact_context}]"), *provider_history)
    if context is not None and allowed_action is None:
        request_text, ambiguous_reference = resolve_contextual_request(text, context)
        if ambiguous_reference:
            if conversation_memory is not None and user_id is not None:
                conversation_memory.remember(
                    user_id, chat_id=message.chat.id,
                    last_intent="clarify", last_entity_type="conversation",
                    last_query="", pending_slots=("reference",),
                    topic=context.topic, last_attempt=text,
                )
            await message.answer("Уточните, пожалуйста, о какой позиции или теме речь.")
            return
    elif context is None and allowed_action is None:
        request_text, ambiguous_reference = resolve_contextual_request(text, None)
        if ambiguous_reference:
            if conversation_memory is not None and user_id is not None:
                conversation_memory.remember(
                    user_id, chat_id=message.chat.id,
                    last_intent="clarify", last_entity_type="conversation",
                    last_query="", pending_slots=("reference",), topic="",
                    last_attempt=text,
                )
            await message.answer("Уточните, пожалуйста, о какой позиции или теме речь.")
            return
    elif (
        context is not None
        and context.last_entity_type == "warehouse_product"
        and context.authorized_action
        in {
            AssistantIntentName.PREPARE_WRITEOFF.value,
            AssistantIntentName.PREPARE_TRANSFER.value,
        }
    ):
        allowed_action = AssistantIntentName(context.authorized_action)
        verb = (
            "спиши"
            if allowed_action is AssistantIntentName.PREPARE_WRITEOFF
            else "перемести"
        )
        request_text = f"{verb} {context.last_query} {text}"

    contextual_intent: AssistantIntent | None = None
    reference_relation = (
        None if allowed_action is not None else context_reference_relation(text)
    )
    ordinal_match = _CANDIDATE_ORDINAL_RE.fullmatch(text)
    if context is not None and ordinal_match is not None and context.candidate_ids:
        ordinal = ordinal_match.group("ordinal").casefold()
        ordinal_index = next(
            (index for prefix, index in (("перв", 0), ("втор", 1), ("трет", 2), ("четверт", 3), ("пят", 4)) if ordinal.startswith(prefix)),
            -1,
        )
        if 0 <= ordinal_index < len(context.candidate_ids):
            selected = registry.retriever.by_key(context.candidate_ids[ordinal_index])
            if selected is not None:
                contextual_intent = AssistantIntent(
                    intent=AssistantIntentName.FIND_TECH_CARD,
                    confidence=10_000,
                    query=context.last_query or selected.item.name,
                    domain=AssistantDomain.CATALOG,
                    requested_kind=context.requested_kind,
                    relation=context.relation,
                    relation_target=(
                        context.relation_target or context.last_query
                        if context.relation is not AssistantRelation.UNSPECIFIED
                        else None
                    ),
                    parent_entity_id=context.parent_entity_id or None,
                    selected_entity_id=context.candidate_ids[ordinal_index],
                    evidence=("stable_candidate_selection",),
                )
    elif context is not None and reference_relation is not None:
        selected_id = context.last_successful_entity_id
        selected_label = context.last_successful_entity_label
        if not selected_id:
            if conversation_memory is not None:
                conversation_memory.remember(
                    user_id,
                    chat_id=message.chat.id,
                    last_intent="clarify",
                    last_entity_type="conversation",
                    last_query="",
                    candidate_ids=context.current_candidates,
                    pending_slots=("reference",),
                    topic=context.topic,
                    last_attempt=text,
                )
            await message.answer("Уточните, пожалуйста, о какой позиции или теме речь.")
            return
        contextual_intent = AssistantIntent(
            intent=AssistantIntentName.FIND_TECH_CARD,
            confidence=10_000,
            query=selected_label or context.last_query,
            domain=AssistantDomain.CATALOG,
            requested_kind=(
                AssistantRequestedKind.INGREDIENT
                if reference_relation is AssistantRelation.INGREDIENTS_OF
                else AssistantRequestedKind.TECH_CARD
            ),
            relation=reference_relation,
            relation_target=selected_label or context.last_query,
            selected_entity_id=selected_id,
            evidence=("stable_entity_followup",),
        )

    local_exact_hit = False
    provider_latency_bucket = "local"
    intent = None
    fallback_intent = None
    fallback_action = None
    fallback_used = False
    provider_started: float | None = None
    parsed_request = parse_bar_request(request_text)
    try:
        allowed_capability_ids = (
            assistant_capability_controller.available_ids(user_id)
            if assistant_capability_controller is not None and user_id is not None
            else ()
        )
        allowed_capability_ids = capability_ids_for_domain(
            allowed_capability_ids,
            parsed_request.domain,
        )
        local_intent = contextual_intent or orchestrator.local_intent(request_text)
        retrieval = (
            CatalogRetrieval(normalized_query=context.last_query)
            if context is not None
            and context.last_entity_type == "warehouse_product"
            else registry.retriever.retrieve(
                local_intent.query if local_intent is not None and local_intent.query else request_text,
                requested_kind=(
                    local_intent.requested_kind
                    if local_intent is not None
                    else AssistantRequestedKind.UNSPECIFIED
                ),
                relation=(
                    local_intent.relation
                    if local_intent is not None
                    else AssistantRelation.UNSPECIFIED
                ),
                relation_target=(
                    local_intent.relation_target if local_intent is not None else None
                ),
                parent_entity_id=(
                    local_intent.parent_entity_id if local_intent is not None else None
                ),
                selected_entity_id=(
                    local_intent.selected_entity_id if local_intent is not None else None
                ),
            )
        )
        if (
            local_intent is None
            and allowed_action is None
            and retrieval.candidates
        ):
            fallback_intent = AssistantIntent(
                intent="find_tech_card",
                confidence=10_000,
                query=retrieval.normalized_query,
                domain=AssistantDomain.CATALOG,
                requested_kind=retrieval.requested_kind,
                relation=retrieval.relation,
                relation_target=retrieval.relation_target,
            )
            fallback_action = registry.execute(fallback_intent)
        if local_intent is None and retrieval.exact:
            local_exact_hit = True
            intent = await orchestrator.interpret(
                f"покажи техкарту {retrieval.normalized_query}"
            )
        else:
            provider_candidates = tuple(
                candidate.provider_label for candidate in retrieval.candidates
            )
            provider_started = monotonic()
            intent = local_intent or await orchestrator.interpret(
                request_text,
                candidates=provider_candidates,
                normalized_entity=retrieval.normalized_query,
                conversation=provider_history,
                allowed_action=allowed_action,
                allowed_capability_ids=allowed_capability_ids,
            )
            if local_intent is None:
                provider_latency_bucket = _latency_bucket(
                    monotonic() - provider_started
                )
        if intent.intent is AssistantIntentName.CAPABILITY:
            if (
                assistant_capability_controller is None
                or intent.capability_id not in allowed_capability_ids
            ):
                raise AccessDeniedError("Provider selected unavailable capability")
            planned = assistant_capability_controller.dispatch(
                text,
                actor_user_id=user_id,
                source_chat_id=message.chat.id,
                source_message_id=int(getattr(message, "message_id", 0)),
            )
            if planned is None or planned.capability_id != intent.capability_id:
                await message.answer(
                    "Не хватает параметров для этого действия. Уточните запрос."
                )
                return
            await send_capability(planned)
            return
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
                    domain=AssistantDomain.CATALOG,
                    requested_kind=retrieval.requested_kind,
                    relation=retrieval.relation,
                    relation_target=retrieval.relation_target,
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
                        domain=AssistantDomain.CATALOG,
                        requested_kind=retrieval.requested_kind,
                        relation=retrieval.relation,
                        relation_target=retrieval.relation_target,
                    )
                )

        # A deterministic read-only catalog route keeps its bounded local
        # candidates. Provider refinement previously discarded this useful
        # result whenever Gemini timed out or returned malformed JSON.
    except AssistantUnavailableError:
        await message.answer(AI_UNAVAILABLE_TEXT)
        return
    except ProviderError as exc:
        if provider_started is not None:
            provider_latency_bucket = _latency_bucket(
                monotonic() - provider_started
            )
        logger.warning(
            "Assistant intent provider failed: provider=%s kind=%s",
            exc.provider,
            exc.kind.value,
        )
        if fallback_intent is None or fallback_action is None:
            await message.answer(_provider_error_text(exc))
            return
        intent, action = fallback_intent, fallback_action
        fallback_used = True
    except asyncio.CancelledError:
        raise
    except Exception:
        raise

    logger.info(
        "Assistant decision intent=%s domain=%s local_exact_hit=%s "
        "candidate_count=%d clarification_required=%s tool_result=%s "
        "provider_latency_bucket=%s fallback_used=%s mutation=%s evidence_count=%d",
        intent.intent.value,
        parsed_request.domain,
        local_exact_hit,
        len(action.search_results),
        action.kind == "clarify",
        action.kind,
        provider_latency_bucket,
        fallback_used,
        parsed_request.mutation,
        len(parsed_request.evidence),
    )

    if action.kind == "list_open_attendance_sessions":
        tools = AssistantReadOnlyTools(
            catalog, auth_service, attendance=warehouse_attendance_service,
            operations=warehouse_service,
        )
        try:
            sessions = tools.list_open_attendance_sessions(user_id) if user_id is not None else ()
        except AccessDeniedError:
            await message.answer("Недостаточно прав для просмотра открытых смен.")
            return
        except Exception:
            logger.exception("Assistant read-only attendance tool failed")
            await message.answer("Не удалось прочитать открытые смены. Попробуйте позже.")
            return
        if sessions:
            lines = ["Сейчас открыты персональные смены:"]
            for session in sessions:
                hours, remainder = divmod(session.duration_seconds, 3600)
                minutes = remainder // 60
                label = session.employee + (f" (@{session.username})" if session.username else "")
                lines.append(f"• {label} — {session.location}, {hours} ч {minutes} мин")
            answer = "\n".join(lines)
        else:
            answer = "Открытых персональных смен сейчас нет."
        await _answer_safe_text(message, answer)
        return

    if action.kind == "get_current_shift_summary":
        tools = AssistantReadOnlyTools(
            catalog, auth_service, attendance=warehouse_attendance_service,
            operations=warehouse_service,
        )
        try:
            shift = tools.get_current_shift_summary(user_id) if user_id is not None else None
        except AccessDeniedError:
            await message.answer("Недостаточно прав для просмотра текущей смены.")
            return
        except Exception:
            logger.exception("Assistant read-only shift tool failed")
            await message.answer("Не удалось прочитать текущую смену. Попробуйте позже.")
            return
        assert shift is not None
        answer = (
            f"Текущая смена: {shift.location}, {shift.business_date}, статус {shift.status}."
            if shift.exists else
            f"Для {shift.location} на {shift.business_date} складская смена ещё не создана."
        )
        if conversation_memory is not None and user_id is not None:
            conversation_memory.append_turn(user_id, chat_id=message.chat.id, role="user", text=text, last_intent=action.kind, topic="текущая смена")
            conversation_memory.append_turn(user_id, chat_id=message.chat.id, role="assistant", text=answer, last_intent=action.kind)
        await _answer_safe_text(message, answer)
        return

    if action.kind == "general_chat":
        if not orchestrator.general_chat_enabled:
            await message.answer(
                "Общий разговор пока отключён. Я могу помочь с данными BarBot."
            )
            return
        answer = (action.message or AI_UNAVAILABLE_TEXT).strip()[:1000]
        if conversation_memory is not None and user_id is not None:
            conversation_memory.append_turn(
                user_id,
                chat_id=message.chat.id,
                role="user",
                text=text,
                last_intent="general_chat",
                topic=(context.topic if context else text),
            )
            conversation_memory.append_turn(
                user_id,
                chat_id=message.chat.id,
                role="assistant",
                text=answer,
                last_intent="general_chat",
            )
        await _answer_safe_text(message, answer)
        return
    if action.kind == "web_search":
        if not orchestrator.web_search_enabled:
            if conversation_memory is not None and user_id is not None:
                conversation_memory.append_turn(
                    user_id, chat_id=message.chat.id,
                    role="user", text=text, last_intent="web_search",
                    topic=action.entity_query or (context.topic if context else ""),
                )
                conversation_memory.remember(
                    user_id, chat_id=message.chat.id,
                    last_intent="web_search", last_entity_type="conversation",
                    last_query=context.last_query if context else "",
                    topic=action.entity_query or (context.topic if context else text),
                    last_requested_action="web_search",
                )
            await message.answer(WEB_SEARCH_UNAVAILABLE_TEXT)
            return
        try:
            result = await orchestrator.search_web(
                action.entity_query or text,
                conversation=provider_history,
            )
        except (AssistantUnavailableError, ProviderError) as exc:
            if isinstance(exc, ProviderError):
                logger.warning(
                    "Assistant web provider failed: provider=%s kind=%s",
                    exc.provider,
                    exc.kind.value,
                )
            await message.answer(WEB_SEARCH_UNAVAILABLE_TEXT)
            return
        logger.info(
            "Assistant grounded search completed query_count=%d source_count=%d",
            result.query_count,
            len(result.sources),
        )
        answer = _without_model_links(result.answer_text).strip()[:3000]
        if not answer:
            answer = "Поиск завершён, но текст ответа недоступен."
        if conversation_memory is not None and user_id is not None:
            conversation_memory.append_turn(
                user_id,
                chat_id=message.chat.id,
                role="user",
                text=text,
                last_intent="web_search",
                topic=action.entity_query or "web_search",
            )
            conversation_memory.append_turn(
                user_id,
                chat_id=message.chat.id,
                role="assistant",
                text=answer,
                last_intent="web_search",
            )
        await _answer_safe_text(message, answer)
        if result.sources:
            links = ["<b>Источники:</b>"]
            for source in result.sources:
                links.append(
                    f'• <a href="{escape(source.uri, quote=True)}">'
                    f"{escape(source.title)}</a>"
                )
            await message.answer("\n".join(links))
        return

    if action.kind == "find_tech_card":
        if user_id is None or not auth_service.can_view_catalog(user_id):
            await message.answer("Недостаточно прав для просмотра каталога.")
            return
        if action.catalog_exact and len(action.search_results) == 1:
            result = action.search_results[0]
            if conversation_memory is not None and user_id is not None:
                conversation_memory.clear_structured(
                    user_id,
                    chat_id=message.chat.id,
                    last_intent="find_tech_card",
                    topic=result.item.name,
                )
                conversation_memory.remember(
                    user_id,
                    chat_id=message.chat.id,
                    last_intent="find_tech_card",
                    last_entity_type="tech_card",
                    last_query=result.item.name,
                    topic=result.item.name,
                    last_entity_id=AssistantToolRegistry._candidate_key(result),
                    last_entity_label=result.item.name,
                    last_requested_action="find_tech_card",
                    requested_kind=action.requested_kind,
                    relation=action.relation,
                    relation_target=action.relation_target or "",
                    parent_entity_id=action.parent_entity_id or "",
                    related_entity_ids=action.related_entity_ids,
                    selected_entity_id=(
                        action.selected_entity_id
                        or AssistantToolRegistry._candidate_key(result)
                    ),
                )
                conversation_memory.append_turn(
                    user_id,
                    chat_id=message.chat.id,
                    role="user",
                    text=text,
                    last_intent="find_tech_card",
                )
                conversation_memory.append_turn(
                    user_id,
                    chat_id=message.chat.id,
                    role="assistant",
                    text=f"Открыта техкарта: {result.item.name}",
                    last_intent="find_tech_card",
                )
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
                conversation_memory.append_turn(
                    user_id,
                    chat_id=message.chat.id,
                    role="user",
                    text=text,
                    last_intent="find_tech_card",
                    topic=action.entity_query or "tech_card",
                )
                conversation_memory.remember(
                    user_id,
                    chat_id=message.chat.id,
                    last_intent="find_tech_card",
                    last_entity_type="tech_card",
                    last_query=action.entity_query
                    or extract_catalog_entity(request_text),
                    candidate_ids=action.candidate_ids,
                    pending_slots=("candidate",),
                    requested_kind=action.requested_kind,
                    relation=action.relation,
                    relation_target=action.relation_target or "",
                    parent_entity_id=action.parent_entity_id or "",
                    related_entity_ids=action.related_entity_ids,
                    last_attempt=action.entity_query or request_text,
                )
            await message.answer(
                action.message or "Возможно, вы имели в виду:",
                reply_markup=build_assistant_candidates_keyboard(
                    action.search_results
                ),
            )
        return
    if action.kind == "not_found":
        if conversation_memory is not None and user_id is not None:
            conversation_memory.remember(
                user_id,
                chat_id=message.chat.id,
                last_intent="not_found",
                last_entity_type="catalog_attempt",
                last_query=action.entity_query or extract_catalog_entity(request_text),
                topic=context.topic if context is not None else "",
                last_attempt=action.entity_query or request_text,
            )
        await message.answer(escape(action.message or AI_UNAVAILABLE_TEXT))
        return
    if action.kind in {"prepare_writeoff", "prepare_transfer"}:
        if warehouse_service is None or action.raw_intake is None:
            await message.answer("Складской модуль сейчас недоступен.")
            return
        if conversation_memory is not None and user_id is not None:
            conversation_memory.clear_structured(
                user_id,
                chat_id=message.chat.id,
                last_intent=action.kind,
                topic=context.topic if context is not None else "",
            )
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
        and allowed_action is intent.intent
    ):
        conversation_memory.remember(
            user_id,
            chat_id=message.chat.id,
            last_intent=intent.intent.value,
            last_entity_type="warehouse_product",
            last_query=action.entity_query,
            pending_slots=action.pending_slots,
            authorized_action=intent.intent.value,
        )
    elif action.kind == "clarify" and context is not None:
        if conversation_memory is not None and user_id is not None:
            conversation_memory.refresh(user_id, message.chat.id)
    elif conversation_memory is not None and user_id is not None:
        conversation_memory.refresh(user_id, message.chat.id)
    await message.answer(escape(action.message or AI_UNAVAILABLE_TEXT))


@voice_router.callback_query(
    StateFilter(None), AssistantProposalCallback.filter()
)
async def handle_assistant_proposal(
    callback: CallbackQuery,
    callback_data: AssistantProposalCallback,
    auth_service: AuthService,
    assistant_action_proposals: AssistantActionProposalService,
    assistant_capability_controller: AssistantCapabilityController,
    assistant_access_policy: AssistantAccessPolicy,
) -> None:
    """Confirm only an owned opaque proposal through an allowlisted adapter."""

    if (
        not isinstance(callback.message, Message)
        or not assistant_access_policy.can_enter_assistant(
            callback.from_user.id,
            chat_type=(
                callback.message.chat.type
                if isinstance(callback.message, Message)
                else "unknown"
            ),
        )
    ):
        await answer_callback_safely(callback, "Недостаточно прав.", show_alert=True)
        return
    try:
        if callback_data.action == "x":
            assistant_action_proposals.cancel(
                callback_data.token,
                callback.from_user.id,
                callback.message.chat.id,
            )
            if await answer_callback_safely(callback, "Отменено"):
                await callback.message.answer("Действие отменено. Изменения не применены.")
            return
        _proposal, result = assistant_action_proposals.confirm(
            callback_data.token,
            callback.from_user.id,
            callback.message.chat.id,
            lambda row, connection: assistant_capability_controller.execute(
                row, callback.from_user.id, connection
            ),
        )
    except AssistantProposalExpired:
        await answer_callback_safely(callback, "Подтверждение устарело.", show_alert=True)
        return
    except (AssistantProposalPermissionDenied, AccessDeniedError):
        await answer_callback_safely(callback, "Недостаточно прав.", show_alert=True)
        return
    except AssistantProposalTargetConflict:
        await answer_callback_safely(callback, "Данные изменились. Повторите запрос.", show_alert=True)
        return
    except AssistantProposalError:
        await answer_callback_safely(callback, "Действие уже завершено или недоступно.", show_alert=True)
        return
    except Exception:
        logger.exception("Assistant proposal execution failed")
        await answer_callback_safely(callback, "Не удалось выполнить действие.", show_alert=True)
        return
    if not await answer_callback_safely(callback, "Готово"):
        return
    status = str(result.get("status", "executed"))
    await callback.message.answer(escape(f"Действие выполнено: {status}."))


@voice_router.callback_query(
    StateFilter(None), AssistantCandidateCallback.filter()
)
async def handle_assistant_candidate(
    callback: CallbackQuery,
    callback_data: AssistantCandidateCallback,
    catalog_path: Path,
    auth_service: AuthService,
    assistant_conversation_memory: AssistantConversationMemory,
    assistant_access_policy: AssistantAccessPolicy,
) -> None:
    """Resolve a bounded candidate callback against current local catalog."""

    if (
        not isinstance(callback.message, Message)
        or not assistant_access_policy.can_enter_assistant(
            callback.from_user.id,
            chat_type=(
                callback.message.chat.type
                if isinstance(callback.message, Message)
                else "unknown"
            ),
        )
        or not auth_service.can_view_catalog(callback.from_user.id)
    ):
        await answer_callback_safely(callback, "Недостаточно прав.", show_alert=True)
        return
    subcategory_id = callback_data.subcategory_id
    key = "/".join(
        (callback_data.category_id, subcategory_id, callback_data.item_id)
    )
    context = assistant_conversation_memory.get(
        callback.from_user.id, callback.message.chat.id
    )
    if context is None or key not in context.candidate_ids:
        await answer_callback_safely(
            callback, "Этот выбор устарел. Повторите запрос.", show_alert=True
        )
        return
    try:
        catalog = load_catalog(catalog_path)
    except CatalogStorageError:
        assistant_conversation_memory.clear(
            callback.from_user.id, callback.message.chat.id
        )
        await answer_callback_safely(
            callback, "Каталог временно недоступен.", show_alert=True
        )
        return
    result = AssistantToolRegistry(catalog).retriever.by_key(key)
    if result is None:
        assistant_conversation_memory.clear(
            callback.from_user.id, callback.message.chat.id
        )
        await answer_callback_safely(
            callback, "Карточка больше недоступна.", show_alert=True
        )
        return
    if not await answer_callback_safely(callback):
        return
    assistant_conversation_memory.remember(
        callback.from_user.id,
        chat_id=callback.message.chat.id,
        last_intent="find_tech_card",
        last_entity_type="tech_card",
        last_query=context.last_query or result.item.name,
        topic=result.item.name,
        last_entity_id=key,
        last_entity_label=result.item.name,
        last_requested_action="find_tech_card",
        requested_kind=context.requested_kind,
        relation=context.relation,
        relation_target=context.relation_target,
        parent_entity_id=context.parent_entity_id,
        related_entity_ids=context.related_entity_ids,
        selected_entity_id=key,
    )
    assistant_conversation_memory.append_turn(
        callback.from_user.id,
        chat_id=callback.message.chat.id,
        role="user",
        text=result.item.name,
        last_intent="find_tech_card",
    )
    assistant_conversation_memory.append_turn(
        callback.from_user.id,
        chat_id=callback.message.chat.id,
        role="assistant",
        text=f"Открыта техкарта: {result.item.name}",
        last_intent="find_tech_card",
    )
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
    assistant_access_policy: AssistantAccessPolicy,
    assistant_conversation_memory: AssistantConversationMemory | None = None,
    warehouse_service: WarehouseOperationsService | None = None,
    warehouse_attendance_service: WarehouseAttendanceService | None = None,
    assistant_capability_controller: AssistantCapabilityController | None = None,
) -> None:
    voice = message.voice
    if voice is None or assistant_speech_client is None or assistant_orchestrator is None:
        await message.answer(VOICE_UNAVAILABLE_TEXT)
        return
    if (
        message.from_user is None
        or not assistant_access_policy.can_enter_assistant(
            message.from_user.id,
            chat_type=message.chat.type,
        )
    ):
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
            warehouse_attendance_service,
            assistant_capability_controller,
            assistant_access_policy,
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
    warehouse_attendance_service: WarehouseAttendanceService | None,
    assistant_capability_controller: AssistantCapabilityController | None,
    assistant_access_policy: AssistantAccessPolicy | None,
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
    except asyncio.CancelledError:
        raise
    except OSError:
        logger.exception("Assistant voice temporary-file processing failed")
        await message.answer(VOICE_UNAVAILABLE_TEXT)
        return
    transcript = " ".join(transcript.split())
    await message.answer(f"🎙 <b>Распознано:</b>\n«{escape(transcript)}»")
    if len(transcript) < 2:
        if assistant_conversation_memory is not None and message.from_user is not None:
            assistant_conversation_memory.clear(
                message.from_user.id, message.chat.id
            )
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
        warehouse_attendance_service=warehouse_attendance_service,
        assistant_capability_controller=assistant_capability_controller,
        assistant_access_policy=assistant_access_policy,
    )


@text_router.message(StateFilter(None), F.text, AssistantTextEligibleFilter())
async def handle_assistant_text(
    message: Message,
    state: FSMContext,
    catalog_path: Path,
    auth_service: AuthService,
    assistant_orchestrator: AssistantOrchestrator | None,
    assistant_request_coordinator: AssistantRequestCoordinator,
    assistant_access_policy: AssistantAccessPolicy,
    assistant_conversation_memory: AssistantConversationMemory | None = None,
    warehouse_service: WarehouseOperationsService | None = None,
    warehouse_attendance_service: WarehouseAttendanceService | None = None,
    assistant_capability_controller: AssistantCapabilityController | None = None,
) -> None:
    if (
        assistant_orchestrator is None
        or message.from_user is None
        or not assistant_access_policy.can_enter_assistant(
            message.from_user.id,
            chat_type=message.chat.type,
        )
    ):
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
            warehouse_attendance_service=warehouse_attendance_service,
            assistant_capability_controller=assistant_capability_controller,
            assistant_access_policy=assistant_access_policy,
        )
