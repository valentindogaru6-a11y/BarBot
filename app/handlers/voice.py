"""Protected Telegram voice-message search using local recognition only."""

from __future__ import annotations

import logging
from html import escape
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.config import Settings
from app.handlers.catalog import _show_item
from app.handlers.common import catalog_error_message, edit_or_send_text, load_catalog
from app.keyboards.callbacks import SearchCallback
from app.keyboards.catalog import build_search_results_keyboard
from app.keyboards.main_menu import (
    build_main_menu_keyboard,
    build_voice_retry_keyboard,
)
from app.middlewares.access import CatalogAccessFilter
from app.services.audio_service import (
    AudioServiceError,
    AudioConversionTimeoutError,
    FFmpegUnavailableError,
    temporary_audio_files,
)
from app.services.auth_service import AuthService
from app.services.speech_service import (
    SpeechModelNotFoundError,
    SpeechRecognitionError,
    SpeechUnavailableError,
)
from app.services.voice_matcher import (
    MatchConfidence,
    VoiceCatalogMatcher,
    build_voice_vocabulary,
)
from app.services.voice_processing_service import VoiceProcessingService
from app.services.voice_query_service import normalize_voice_query
from app.storage.json_storage import CatalogStorageError

router = Router(name=__name__)
router.message.filter(CatalogAccessFilter())
router.callback_query.filter(CatalogAccessFilter())
logger = logging.getLogger(__name__)

VOICE_UNAVAILABLE_TEXT = (
    "Голосовой поиск временно недоступен. Используйте обычный поиск."
)
VOICE_RETRY_TEXT = (
    "Отправьте голосовое сообщение с названием позиции. Например: "
    "«Покажи Old Fashioned» или «Найди техкарту киви панч»."
)


def _is_too_large(file_size: int | None, settings: Settings) -> bool:
    return bool(
        file_size is not None
        and file_size > settings.voice_max_file_size_bytes
    )


@router.callback_query(SearchCallback.filter(F.action == "voice_retry"))
async def handle_voice_retry(callback: CallbackQuery) -> None:
    """Explain how to repeat a voice query without starting polling modes."""

    await callback.answer()
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message,
            VOICE_RETRY_TEXT,
            build_voice_retry_keyboard(),
        )


@router.message(F.voice)
async def handle_voice_search(
    message: Message,
    state: FSMContext,
    bot: Bot,
    catalog_path: Path,
    auth_service: AuthService,
    settings: Settings,
    voice_processing_service: VoiceProcessingService,
) -> None:
    """Download, recognize, match, and render one permitted voice request."""

    voice = message.voice
    if voice is None:
        return
    if not settings.voice_search_enabled:
        await message.answer(
            VOICE_UNAVAILABLE_TEXT,
            reply_markup=build_voice_retry_keyboard(),
        )
        return
    if voice.duration > settings.voice_max_duration_seconds:
        await message.answer(
            "Голосовое сообщение слишком длинное. "
            f"Максимум — {settings.voice_max_duration_seconds} сек.",
            reply_markup=build_voice_retry_keyboard(),
        )
        return
    if _is_too_large(voice.file_size, settings):
        await message.answer(
            "Голосовой файл слишком большой. "
            f"Максимум — {settings.voice_max_file_size_mb} МБ.",
            reply_markup=build_voice_retry_keyboard(),
        )
        return

    try:
        catalog = load_catalog(catalog_path)
    except CatalogStorageError as exc:
        logger.error("Could not load catalog for voice search: %s", type(exc).__name__)
        await message.answer(
            catalog_error_message(exc),
            reply_markup=build_main_menu_keyboard(),
        )
        return

    await state.clear()
    await bot.send_chat_action(
        chat_id=message.chat.id,
        action=ChatAction.TYPING,
    )
    try:
        with temporary_audio_files() as files:
            await bot.download(voice, destination=files.source)
            recognition = await voice_processing_service.recognize(
                files.source,
                files.wav,
                build_voice_vocabulary(catalog),
            )
    except (
        FFmpegUnavailableError,
        SpeechModelNotFoundError,
        SpeechUnavailableError,
    ) as exc:
        logger.warning("Voice search dependency unavailable: %s", type(exc).__name__)
        await message.answer(
            VOICE_UNAVAILABLE_TEXT,
            reply_markup=build_voice_retry_keyboard(),
        )
        return
    except AudioConversionTimeoutError:
        logger.warning("Voice audio conversion timed out")
        await message.answer(
            "Не удалось вовремя обработать голосовое сообщение. "
            "Попробуйте короче или используйте обычный поиск.",
            reply_markup=build_voice_retry_keyboard(),
        )
        return
    except (AudioServiceError, SpeechRecognitionError) as exc:
        logger.warning("Voice processing failed: %s", type(exc).__name__)
        await message.answer(
            "Не удалось распознать голосовое сообщение. Попробуйте ещё раз.",
            reply_markup=build_voice_retry_keyboard(),
        )
        return
    except Exception as exc:
        logger.error(
            "Voice download or processing failed: %s",
            type(exc).__name__,
        )
        await message.answer(
            "Не удалось обработать голосовое сообщение. Попробуйте позже.",
            reply_markup=build_voice_retry_keyboard(),
        )
        return

    transcript = recognition.text
    if settings.voice_debug_transcripts:
        logger.info("Voice transcript: %s", transcript)
    query = normalize_voice_query(transcript)
    if not query:
        await message.answer(
            "Не удалось расслышать название позиции. Попробуйте ещё раз.",
            reply_markup=build_voice_retry_keyboard(),
        )
        return

    outcome = VoiceCatalogMatcher(catalog).match(query)
    heard_text = escape(query)
    if outcome.confidence is MatchConfidence.HIGH and outcome.matches:
        match = outcome.matches[0].result
        await message.answer(f"🎙 Услышал: «{heard_text}»")
        await _show_item(
            message,
            catalog_path,
            match.category.id,
            match.item.id,
            match.subcategory.id if match.subcategory else None,
            is_admin=bool(
                message.from_user
                and auth_service.is_admin(message.from_user.id)
            ),
        )
        return

    if outcome.confidence is MatchConfidence.MEDIUM and outcome.matches:
        results = [match.result for match in outcome.matches]
        await message.answer(
            f"🎙 Услышал: «{heard_text}»\n\n"
            "Нашёл несколько похожих вариантов:",
            reply_markup=build_search_results_keyboard(results),
        )
        return

    await message.answer(
        f"🎙 Услышал: «{heard_text}»\n\n"
        "Точного совпадения не нашлось.",
        reply_markup=build_voice_retry_keyboard(),
    )
