"""Voice-handler tests with no Telegram API, ffmpeg, or Vosk calls."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.handlers import voice as voice_handlers
from app.middlewares import access as access_middleware
from app.middlewares.access import BLOCKED_TEXT, CatalogAccessFilter
from app.models.catalog import Catalog
from app.services.auth_service import AuthService
from app.services.speech_service import (
    SpeechModelNotFoundError,
    SpeechRecognitionResult,
)


def _catalog(*, similar: bool = False) -> Catalog:
    items = [
        {
            "id": "old-fashioned",
            "name": "Old Fashioned",
            "voice_aliases": ["олд фэшн"],
            "photo_file_id": "safe-photo-id",
        }
    ]
    if similar:
        items = [
            {"id": "berry-red", "name": "Berry Red"},
            {"id": "berry-blue", "name": "Berry Blue"},
        ]
    return Catalog.model_validate(
        {
            "version": 1,
            "categories": [
                {
                    "id": "cocktails",
                    "name": "Коктейли",
                    "subcategories": [
                        {
                            "id": "classic",
                            "name": "Классические",
                            "items": items,
                        }
                    ],
                }
            ],
        }
    )


def _settings(tmp_path: Path, **updates) -> Settings:
    values = {
        "_env_file": None,
        "bot_token": "test-token",
    }
    values.update(updates)
    return Settings(**values)


def _message(*, duration: int = 4, file_size: int = 1000):
    return SimpleNamespace(
        voice=SimpleNamespace(
            file_id="voice-id",
            duration=duration,
            file_size=file_size,
        ),
        chat=SimpleNamespace(id=77),
        from_user=SimpleNamespace(id=10),
        answer=AsyncMock(),
    )


def _bot():
    bot = SimpleNamespace(send_chat_action=AsyncMock())

    async def download(voice, destination: Path) -> None:
        destination.write_bytes(b"telegram-voice")

    bot.download = AsyncMock(side_effect=download)
    return bot


def _state():
    return SimpleNamespace(clear=AsyncMock())


def test_high_confidence_reuses_existing_item_renderer(
    tmp_path: Path,
    monkeypatch,
) -> None:
    message = _message()
    bot = _bot()
    processor = SimpleNamespace(
        recognize=AsyncMock(
            return_value=SpeechRecognitionResult("найди олд фэшн", "ru", 0.9)
        )
    )
    rendered = AsyncMock()
    monkeypatch.setattr(voice_handlers, "load_catalog", lambda _: _catalog())
    monkeypatch.setattr(voice_handlers, "_show_item", rendered)

    asyncio.run(
        voice_handlers.handle_voice_search(
            message,
            _state(),
            bot,
            tmp_path / "catalog.json",
            AuthService(frozenset({10})),
            _settings(tmp_path),
            processor,
        )
    )

    bot.download.assert_awaited_once()
    rendered.assert_awaited_once_with(
        message,
        tmp_path / "catalog.json",
        "cocktails",
        "old-fashioned",
        "classic",
        is_admin=True,
    )
    assert "олд фэшн" in message.answer.await_args_list[0].args[0]


def test_medium_confidence_shows_existing_search_result_buttons(
    tmp_path: Path,
    monkeypatch,
) -> None:
    message = _message()
    processor = SimpleNamespace(
        recognize=AsyncMock(
            return_value=SpeechRecognitionResult("berry", "ru")
        )
    )
    monkeypatch.setattr(
        voice_handlers,
        "load_catalog",
        lambda _: _catalog(similar=True),
    )
    rendered = AsyncMock()
    monkeypatch.setattr(voice_handlers, "_show_item", rendered)

    asyncio.run(
        voice_handlers.handle_voice_search(
            message,
            _state(),
            _bot(),
            tmp_path / "catalog.json",
            AuthService(frozenset()),
            _settings(tmp_path),
            processor,
        )
    )

    rendered.assert_not_awaited()
    markup = message.answer.await_args.kwargs["reply_markup"]
    item_buttons = [
        button
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith("i:")
    ]
    assert len(item_buttons) == 2


def test_low_confidence_offers_voice_and_text_retry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    message = _message()
    monkeypatch.setattr(voice_handlers, "load_catalog", lambda _: _catalog())
    processor = SimpleNamespace(
        recognize=AsyncMock(
            return_value=SpeechRecognitionResult("абракадабра", "ru")
        )
    )

    asyncio.run(
        voice_handlers.handle_voice_search(
            message,
            _state(),
            _bot(),
            tmp_path / "catalog.json",
            AuthService(frozenset()),
            _settings(tmp_path),
            processor,
        )
    )

    markup = message.answer.await_args.kwargs["reply_markup"]
    texts = {button.text for row in markup.inline_keyboard for button in row}
    assert texts == {
        "🎙 Повторить голосом",
        "🔍 Обычный поиск",
        "⬅️ Главное меню",
    }


@pytest.mark.parametrize(
    "settings_updates,message_kwargs,expected",
    [
        ({"voice_search_enabled": False}, {}, "временно недоступен"),
        ({"voice_max_duration_seconds": 3}, {"duration": 4}, "слишком длинное"),
        (
            {"voice_max_file_size_mb": 1},
            {"file_size": 2 * 1024 * 1024},
            "слишком большой",
        ),
    ],
)
def test_disabled_or_oversized_voice_is_rejected_before_download(
    tmp_path: Path,
    settings_updates: dict,
    message_kwargs: dict,
    expected: str,
) -> None:
    message = _message(**message_kwargs)
    bot = _bot()
    processor = SimpleNamespace(recognize=AsyncMock())

    asyncio.run(
        voice_handlers.handle_voice_search(
            message,
            _state(),
            bot,
            tmp_path / "catalog.json",
            AuthService(frozenset()),
            _settings(tmp_path, **settings_updates),
            processor,
        )
    )

    bot.download.assert_not_awaited()
    processor.recognize.assert_not_awaited()
    assert expected in message.answer.await_args.args[0]


def test_missing_model_has_safe_fallback_and_tempfiles_are_removed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    message = _message()
    bot = _bot()
    observed_paths: list[Path] = []

    async def fail(source: Path, wav: Path, vocabulary):
        observed_paths.extend((source, wav))
        assert source.exists()
        raise SpeechModelNotFoundError("missing")

    processor = SimpleNamespace(recognize=AsyncMock(side_effect=fail))
    monkeypatch.setattr(voice_handlers, "load_catalog", lambda _: _catalog())

    asyncio.run(
        voice_handlers.handle_voice_search(
            message,
            _state(),
            bot,
            tmp_path / "catalog.json",
            AuthService(frozenset()),
            _settings(tmp_path),
            processor,
        )
    )

    assert "временно недоступен" in message.answer.await_args.args[0]
    assert observed_paths
    assert all(not path.exists() for path in observed_paths)


def test_access_filter_rejects_blocked_user_before_handler(monkeypatch) -> None:
    class FakeMessage:
        def __init__(self) -> None:
            self.from_user = SimpleNamespace(id=10)
            self.answer = AsyncMock()

    event = FakeMessage()
    monkeypatch.setattr(access_middleware, "Message", FakeMessage)
    auth = SimpleNamespace(can_view_catalog=lambda _: False)

    allowed = asyncio.run(CatalogAccessFilter()(event, auth))

    assert allowed is False
    event.answer.assert_awaited_once_with(BLOCKED_TEXT)
