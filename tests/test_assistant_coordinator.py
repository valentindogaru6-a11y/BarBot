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


def _text_message(user_id: int):
    return SimpleNamespace(
        text="свободный AI запрос",
        from_user=SimpleNamespace(id=user_id),
        chat=SimpleNamespace(id=user_id, type="private"),
        answer=AsyncMock(),
    )


def _voice_message(user_id: int):
    return SimpleNamespace(
        voice=SimpleNamespace(
            duration=1,
            file_size=100,
            mime_type="audio/ogg",
        ),
        from_user=SimpleNamespace(id=user_id),
        chat=SimpleNamespace(id=user_id, type="private"),
        answer=AsyncMock(),
    )


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        bot_token="test",
        ai_assistant_enabled=True,
        ai_access_mode="role_based",
        ai_text_enabled=True,
        ai_voice_enabled=True,
        groq_api_key="groq-test",
        gemini_api_key="gemini-test",
    )


class AllowPolicy:
    def can_enter_assistant(self, _user_id, *, chat_type):
        return chat_type == "private"


def test_parallel_text_for_same_user_calls_provider_once_and_releases(
    monkeypatch,
) -> None:
    async def scenario() -> None:
        coordinator = AssistantRequestCoordinator()
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def run(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()

        monkeypatch.setattr(assistant, "_run_assistant", run)
        first = _text_message(10)
        rejected = _text_message(10)
        first_task = asyncio.create_task(
            assistant.handle_assistant_text(
                first,
                SimpleNamespace(),
                Path("catalog.json"),
                AuthService(frozenset({10})),
                SimpleNamespace(),
                coordinator,
                AllowPolicy(),
            )
        )
        await started.wait()
        await assistant.handle_assistant_text(
            rejected,
            SimpleNamespace(),
            Path("catalog.json"),
            AuthService(frozenset({10})),
            SimpleNamespace(),
            coordinator,
            AllowPolicy(),
        )
        assert calls == 1
        assert rejected.answer.await_args.args[0] == assistant.AI_REQUEST_BUSY_TEXT

        release.set()
        await first_task
        after = _text_message(10)
        await assistant.handle_assistant_text(
            after,
            SimpleNamespace(),
            Path("catalog.json"),
            AuthService(frozenset({10})),
            SimpleNamespace(),
            coordinator,
            AllowPolicy(),
        )
        assert calls == 2

    asyncio.run(scenario())


def test_text_and_voice_for_same_user_share_one_claim(monkeypatch) -> None:
    async def scenario() -> None:
        coordinator = AssistantRequestCoordinator()
        started = asyncio.Event()
        release = asyncio.Event()

        async def run(*_args, **_kwargs):
            started.set()
            await release.wait()

        monkeypatch.setattr(assistant, "_run_assistant", run)
        text_task = asyncio.create_task(
            assistant.handle_assistant_text(
                _text_message(11),
                SimpleNamespace(),
                Path("catalog.json"),
                AuthService(frozenset({11})),
                SimpleNamespace(),
                coordinator,
                AllowPolicy(),
            )
        )
        await started.wait()
        voice = _voice_message(11)
        speech = SimpleNamespace(transcribe=AsyncMock())
        telegram = SimpleNamespace(download=AsyncMock())
        await assistant.handle_assistant_voice(
            voice,
            SimpleNamespace(),
            telegram,
            Path("catalog.json"),
            AuthService(frozenset({11})),
            _settings(),
            SimpleNamespace(),
            speech,
            coordinator,
            AllowPolicy(),
        )
        speech.transcribe.assert_not_awaited()
        telegram.download.assert_not_awaited()
        assert voice.answer.await_args.args[0] == assistant.AI_REQUEST_BUSY_TEXT
        release.set()
        await text_task

    asyncio.run(scenario())


def test_different_users_do_not_block_each_other(monkeypatch) -> None:
    async def scenario() -> None:
        coordinator = AssistantRequestCoordinator()
        both_started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def run(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                both_started.set()
            await release.wait()

        monkeypatch.setattr(assistant, "_run_assistant", run)
        tasks = [
            asyncio.create_task(
                assistant.handle_assistant_text(
                    _text_message(user_id),
                    SimpleNamespace(),
                    Path("catalog.json"),
                    AuthService(frozenset({user_id})),
                    SimpleNamespace(),
                    coordinator,
                    AllowPolicy(),
                )
            )
            for user_id in (20, 21)
        ]
        await asyncio.wait_for(both_started.wait(), timeout=1)
        assert calls == 2
        release.set()
        await asyncio.gather(*tasks)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "error",
    [
        ProviderError("gemini", ProviderErrorKind.SERVER),
        TimeoutError("timeout"),
        ValueError("validation"),
        RuntimeError("unexpected"),
    ],
)
def test_errors_release_claim(error) -> None:
    async def scenario() -> None:
        coordinator = AssistantRequestCoordinator()
        with pytest.raises(type(error)):
            async with coordinator.claim(30) as acquired:
                assert acquired
                raise error
        async with coordinator.claim(30) as acquired_again:
            assert acquired_again

    asyncio.run(scenario())


def test_cancellation_releases_claim_and_propagates() -> None:
    async def scenario() -> None:
        coordinator = AssistantRequestCoordinator()
        started = asyncio.Event()

        async def wait_forever() -> None:
            async with coordinator.claim(40) as acquired:
                assert acquired
                started.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(wait_forever())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with coordinator.claim(40) as acquired_again:
            assert acquired_again

    asyncio.run(scenario())


def test_rejected_concurrent_request_never_reaches_warehouse(monkeypatch) -> None:
    async def scenario() -> None:
        coordinator = AssistantRequestCoordinator()
        prepare = AsyncMock()
        monkeypatch.setattr(assistant, "prepare_intake_message", prepare)
        async with coordinator.claim(50) as acquired:
            assert acquired
            message = _text_message(50)
            await assistant.handle_assistant_text(
                message,
                SimpleNamespace(),
                Path("catalog.json"),
                AuthService(frozenset({50})),
                SimpleNamespace(),
                coordinator,
                AllowPolicy(),
                warehouse_service=SimpleNamespace(),
            )
        prepare.assert_not_awaited()

    asyncio.run(scenario())
