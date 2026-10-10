"""Conversational assistant routing, memory and safe response regressions."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers import assistant
from app.models.assistant import AssistantIntent, AssistantIntentName
from app.services.assistant import AssistantConversationMemory, AssistantOrchestrator
from app.services.auth_service import AuthService


class QueueClient:
    def __init__(self, *responses: AssistantIntent) -> None:
        self.responses = list(responses)
        self.calls = []

    async def classify(
        self,
        text,
        *,
        candidates=(),
        normalized_entity="",
        conversation=(),
    ):
        self.calls.append((text, tuple(conversation)))
        return self.responses.pop(0)


def _message(text: str, user_id: int = 1):
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=user_id),
        chat=SimpleNamespace(id=user_id, type="private"),
        answer=AsyncMock(),
    )


async def _run(message, client, memory, *, general=True):
    await assistant._run_assistant(
        message,
        SimpleNamespace(),
        text=message.text,
        catalog_path=Path("data/catalog.json"),
        auth_service=AuthService(frozenset({message.from_user.id})),
        orchestrator=AssistantOrchestrator(
            client,
            general_chat_enabled=general,
        ),
        warehouse_service=None,
        conversation_memory=memory,
    )


def test_general_chat_is_text_only_and_html_safe() -> None:
    client = QueueClient(
        AssistantIntent(
            intent="general_chat",
            answer_text="Лёд мутный из-за пузырьков <b>и примесей</b>.",
        )
    )
    message = _message("почему лёд мутный?")
    asyncio.run(_run(message, client, AssistantConversationMemory()))
    rendered = message.answer.await_args.args[0]
    assert "&lt;b&gt;" in rendered
    assert "<b>и примесей</b>" not in rendered


def test_followup_receives_prior_text_and_assistant_turns() -> None:
    client = QueueClient(
        AssistantIntent(intent="general_chat", answer_text="Три варианта."),
        AssistantIntent(intent="general_chat", answer_text="Второй — лучший."),
    )
    memory = AssistantConversationMemory()
    asyncio.run(_run(_message("назови три варианта"), client, memory))
    asyncio.run(_run(_message("а какой лучше?"), client, memory))
    assert client.calls[1][1] == (
        ("user", "назови три варианта"),
        ("assistant", "Три варианта."),
    )


def test_memory_isolated_bounded_and_expires() -> None:
    now = [1.0]
    memory = AssistantConversationMemory(
        ttl_seconds=10,
        max_turns=3,
        max_chars=12,
        clock=lambda: now[0],
    )
    for text in ("aaaa", "bbbb", "cccc", "dddd"):
        memory.append_turn(1, role="user", text=text)
    memory.append_turn(2, role="user", text="other")
    assert memory.provider_history(1) == (
        ("user", "bbbb"),
        ("user", "cccc"),
        ("user", "dddd"),
    )
    assert memory.provider_history(2) == (("user", "other"),)
    now[0] = 11.0
    assert memory.provider_history(1) == ()


@pytest.mark.parametrize(
    "phrase",
    ["забудь этот разговор", "начнём заново", "сбрось контекст"],
)
def test_reset_phrases_clear_only_conversation_without_touching_fsm(phrase) -> None:
    memory = AssistantConversationMemory()
    memory.append_turn(1, role="user", text="контекст")
    state = SimpleNamespace(clear=AsyncMock())
    message = _message(phrase)
    asyncio.run(
        assistant._run_assistant(
            message,
            state,
            text=phrase,
            catalog_path=Path("data/catalog.json"),
            auth_service=AuthService(frozenset({1})),
            orchestrator=AssistantOrchestrator(None),
            warehouse_service=None,
            conversation_memory=memory,
        )
    )
    assert memory.get(1) is None
    state.clear.assert_not_awaited()
    assert message.answer.await_args.args[0] == assistant.AI_CONTEXT_RESET_TEXT


def test_voice_transcript_and_text_share_same_history() -> None:
    client = QueueClient(
        AssistantIntent(intent="general_chat", answer_text="Первый ответ."),
        AssistantIntent(intent="general_chat", answer_text="Продолжение."),
    )
    memory = AssistantConversationMemory()
    # _run_assistant is the common post-STT path, so the transcript becomes a
    # normal user turn before the next text request.
    asyncio.run(_run(_message("голосовой вопрос"), client, memory))
    asyncio.run(_run(_message("уточни"), client, memory))
    assert client.calls[1][1][0] == ("user", "голосовой вопрос")


def test_general_chat_disabled_preserves_existing_opt_in_boundary() -> None:
    client = QueueClient(
        AssistantIntent(intent="general_chat", answer_text="Не показывать")
    )
    message = _message("привет")
    asyncio.run(_run(message, client, AssistantConversationMemory(), general=False))
    assert message.answer.await_args.args[0].startswith("Общий разговор пока отключён")


def test_intent_shape_rejects_false_tool_and_cross_intent_fields() -> None:
    with pytest.raises(ValueError):
        AssistantIntent.model_validate({"intent": "run_shell"})
    with pytest.raises(ValueError):
        AssistantIntent(
            intent=AssistantIntentName.GENERAL_CHAT,
            answer_text="hello",
            query="delete database",
        )


def test_long_html_heavy_answer_is_split_into_telegram_safe_chunks() -> None:
    message = _message("question")
    asyncio.run(assistant._answer_safe_text(message, "<tag>" * 1000))
    assert len(message.answer.await_args_list) > 1
    assert all(
        len(call.args[0]) <= 3500 and "<tag>" not in call.args[0]
        for call in message.answer.await_args_list
    )
