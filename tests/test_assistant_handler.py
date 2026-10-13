import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.handlers import assistant
from app.services.assistant import (
    AssistantRequestCoordinator,
    ProviderError,
    ProviderErrorKind,
)
from app.services.auth_service import AuthService


def settings(**updates) -> Settings:
    values = {
        "_env_file": None,
        "bot_token": "test",
        "ai_assistant_enabled": True,
        "ai_access_mode": "role_based",
        "ai_voice_enabled": True,
        "groq_api_key": "secret",
        "gemini_api_key": "gemini-secret",
    }
    values.update(updates)
    return Settings(**values)


class AllowPolicy:
    def can_enter_assistant(self, _user_id, *, chat_type):
        return chat_type == "private"


class DenyPolicy:
    def can_enter_assistant(self, _user_id, *, chat_type):
        del chat_type
        return False


def message(*, duration=10, file_size=100, mime_type="audio/ogg"):
    return SimpleNamespace(
        voice=SimpleNamespace(
            duration=duration,
            file_size=file_size,
            mime_type=mime_type,
        ),
        from_user=SimpleNamespace(id=1),
        chat=SimpleNamespace(id=1, type="private"),
        answer=AsyncMock(),
    )


def bot(observed_paths):
    async def download(_voice, destination: Path):
        observed_paths.append(destination)
        destination.write_bytes(b"safe fake ogg")

    return SimpleNamespace(download=AsyncMock(side_effect=download))


def test_voice_transcript_uses_shared_orchestrator_and_cleans_file(tmp_path, monkeypatch) -> None:
    observed_paths = []
    msg = message()
    run = AsyncMock()
    monkeypatch.setattr(assistant, "_run_assistant", run)
    client = SimpleNamespace(transcribe=AsyncMock(return_value="покажи техкарту Kiss Me"))
    orchestrator = SimpleNamespace()

    asyncio.run(
        assistant.handle_assistant_voice(
            msg,
            SimpleNamespace(),
            bot(observed_paths),
            tmp_path / "catalog.json",
            AuthService(frozenset({1})),
            settings(),
            orchestrator,
            client,
            AssistantRequestCoordinator(),
            AllowPolicy(),
        )
    )

    assert "Распознано" in msg.answer.await_args_list[0].args[0]
    run.assert_awaited_once()
    assert run.await_args.kwargs["text"] == "покажи техкарту Kiss Me"
    assert observed_paths and all(not path.exists() for path in observed_paths)


@pytest.mark.parametrize(
    ("updates", "voice_values"),
    [
        ({"ai_voice_max_duration_seconds": 3}, {"duration": 4}),
        ({"ai_voice_max_file_bytes": 10}, {"file_size": 11}),
    ],
)
def test_voice_limits_reject_before_download(updates, voice_values) -> None:
    observed_paths = []
    telegram = bot(observed_paths)
    client = SimpleNamespace(transcribe=AsyncMock())
    asyncio.run(
        assistant.handle_assistant_voice(
            message(**voice_values),
            SimpleNamespace(),
            telegram,
            Path("catalog.json"),
            AuthService(frozenset({1})),
            settings(**updates),
            SimpleNamespace(),
            client,
            AssistantRequestCoordinator(),
            AllowPolicy(),
        )
    )
    telegram.download.assert_not_awaited()
    client.transcribe.assert_not_awaited()


def test_voice_cancellation_propagates_after_tempfile_cleanup(tmp_path) -> None:
    observed_paths = []
    client = SimpleNamespace(transcribe=AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            assistant.handle_assistant_voice(
                message(),
                SimpleNamespace(),
                bot(observed_paths),
                tmp_path / "catalog.json",
                AuthService(frozenset({1})),
                settings(),
                SimpleNamespace(),
                client,
                AssistantRequestCoordinator(),
                AllowPolicy(),
            )
        )
    assert observed_paths and all(not path.exists() for path in observed_paths)


def test_voice_provider_error_is_safe_cleans_file_and_stops_pipeline(
    tmp_path, monkeypatch, caplog
) -> None:
    observed_paths = []
    msg = message()
    run = AsyncMock()
    monkeypatch.setattr(assistant, "_run_assistant", run)
    client = SimpleNamespace(
        transcribe=AsyncMock(
            side_effect=ProviderError("groq", ProviderErrorKind.SERVER)
        )
    )

    asyncio.run(
        assistant.handle_assistant_voice(
            msg,
            SimpleNamespace(),
            bot(observed_paths),
            tmp_path / "catalog.json",
            AuthService(frozenset({1})),
            settings(),
            SimpleNamespace(),
            client,
            AssistantRequestCoordinator(),
            AllowPolicy(),
        )
    )

    run.assert_not_awaited()
    assert msg.answer.await_args.args[0] == assistant.VOICE_UNAVAILABLE_TEXT
    assert observed_paths and all(not path.exists() for path in observed_paths)
    rendered = msg.answer.await_args.args[0] + caplog.text
    assert "secret" not in rendered
    assert "provider response" not in rendered


def test_transcript_is_html_escaped_before_display(tmp_path, monkeypatch) -> None:
    msg = message()
    monkeypatch.setattr(assistant, "_run_assistant", AsyncMock())
    client = SimpleNamespace(transcribe=AsyncMock(return_value="покажи <b>секрет</b>"))
    asyncio.run(
        assistant.handle_assistant_voice(
            msg,
            SimpleNamespace(),
            bot([]),
            tmp_path / "catalog.json",
            AuthService(frozenset({1})),
            settings(),
            SimpleNamespace(),
            client,
            AssistantRequestCoordinator(),
            AllowPolicy(),
        )
    )
    rendered = msg.answer.await_args_list[0].args[0]
    assert "&lt;b&gt;" in rendered
    assert "<b>секрет</b>" not in rendered


def test_denied_voice_stops_before_download_and_provider(tmp_path) -> None:
    telegram = bot([])
    client = SimpleNamespace(transcribe=AsyncMock())

    asyncio.run(
        assistant.handle_assistant_voice(
            message(),
            SimpleNamespace(),
            telegram,
            tmp_path / "catalog.json",
            AuthService(frozenset({1})),
            settings(),
            SimpleNamespace(),
            client,
            AssistantRequestCoordinator(),
            DenyPolicy(),
        )
    )

    telegram.download.assert_not_awaited()
    client.transcribe.assert_not_awaited()


def test_denied_text_stops_before_orchestrator(monkeypatch) -> None:
    run = AsyncMock()
    monkeypatch.setattr(assistant, "_run_assistant", run)

    asyncio.run(
        assistant.handle_assistant_text(
            SimpleNamespace(
                text="что ты умеешь",
                from_user=SimpleNamespace(id=1),
                chat=SimpleNamespace(id=1, type="private"),
                answer=AsyncMock(),
            ),
            SimpleNamespace(),
            Path("catalog.json"),
            AuthService(frozenset({1})),
            SimpleNamespace(),
            AssistantRequestCoordinator(),
            DenyPolicy(),
        )
    )

    run.assert_not_awaited()


def test_denied_proposal_callback_stops_before_cancel_or_confirm(monkeypatch) -> None:
    class FakeMessage:
        def __init__(self) -> None:
            self.chat = SimpleNamespace(id=1, type="private")
            self.answer = AsyncMock()

    monkeypatch.setattr(assistant, "Message", FakeMessage)
    proposals = SimpleNamespace(cancel=AsyncMock(), confirm=AsyncMock())
    controller = SimpleNamespace(execute=AsyncMock())
    callback = SimpleNamespace(
        message=FakeMessage(),
        from_user=SimpleNamespace(id=1),
        answer=AsyncMock(),
    )

    asyncio.run(
        assistant.handle_assistant_proposal(
            callback,
            SimpleNamespace(action="c", token="opaque-token"),
            AuthService(frozenset({1})),
            proposals,
            controller,
            DenyPolicy(),
        )
    )

    proposals.cancel.assert_not_called()
    proposals.confirm.assert_not_called()
    controller.execute.assert_not_called()
    callback.answer.assert_awaited_once()


def test_denied_candidate_callback_stops_before_catalog_or_memory(monkeypatch) -> None:
    class FakeMessage:
        def __init__(self) -> None:
            self.chat = SimpleNamespace(id=1, type="private")
            self.answer = AsyncMock()

    monkeypatch.setattr(assistant, "Message", FakeMessage)
    load_catalog = AsyncMock()
    monkeypatch.setattr(assistant, "load_catalog", load_catalog)
    memory = SimpleNamespace(get=AsyncMock(), clear=AsyncMock())
    callback = SimpleNamespace(
        message=FakeMessage(),
        from_user=SimpleNamespace(id=1),
        answer=AsyncMock(),
    )

    asyncio.run(
        assistant.handle_assistant_candidate(
            callback,
            SimpleNamespace(
                category_id="category",
                subcategory_id="_",
                item_id="item",
            ),
            Path("catalog.json"),
            AuthService(frozenset({1})),
            memory,
            DenyPolicy(),
        )
    )

    load_catalog.assert_not_awaited()
    memory.get.assert_not_called()
    callback.answer.assert_awaited_once()
