"""Handler-level follow-up regressions for structured assistant context."""

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers import assistant
from app.models.assistant import AssistantIntent
from app.models.catalog import Catalog
from app.services.assistant import (
    AssistantConversationMemory,
    AssistantOrchestrator,
    ProviderError,
    ProviderErrorKind,
)
from app.services.auth_service import AuthService


def _catalog_path(tmp_path: Path) -> Path:
    catalog = Catalog.model_validate(
        {
            "version": 1,
            "categories": [
                {
                    "id": "cocktails",
                    "name": "Коктейли",
                    "subcategories": [
                        {
                            "id": "nonalcoholic",
                            "name": "Безалкогольные коктейли",
                            "items": [
                                {
                                    "id": "mojito-zero",
                                    "name": "Безалкогольный мохито",
                                }
                            ],
                        }
                    ],
                    "items": [
                        {
                            "id": "mojito-classic",
                            "name": "Классический мохито",
                        }
                    ],
                }
            ],
        }
    )
    path = tmp_path / "catalog.json"
    path.write_text(catalog.model_dump_json(), encoding="utf-8")
    return path


def _message(user_id: int, text: str) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=user_id),
        chat=SimpleNamespace(id=user_id, type="private"),
        answer=AsyncMock(),
    )


class QueueIntentClient:
    def __init__(self, *intents: AssistantIntent) -> None:
        self.intents = list(intents)
        self.calls: list[tuple[str, tuple[str, ...], str]] = []

    async def classify(self, text, *, candidates=(), normalized_entity=""):
        self.calls.append((text, tuple(candidates), normalized_entity))
        return self.intents.pop(0)


def test_catalog_clarification_continues_with_same_user_and_clears_context(
    tmp_path, monkeypatch
) -> None:
    client = QueueIntentClient(
        AssistantIntent(intent="clarify", clarification="Какой вариант?")
    )
    memory = AssistantConversationMemory()
    shown = AsyncMock()
    monkeypatch.setattr(assistant, "_show_item", shown)
    first = _message(1, "покажи мохито")
    asyncio.run(
        assistant._run_assistant(
            first,
            SimpleNamespace(),
            text=first.text,
            catalog_path=_catalog_path(tmp_path),
            auth_service=AuthService(frozenset({1})),
            orchestrator=AssistantOrchestrator(client),
            warehouse_service=None,
            conversation_memory=memory,
        )
    )
    assert first.answer.await_args.args[0] == "Какой вариант?"
    assert memory.get(1) is not None

    followup = _message(1, "безалкогольный")
    asyncio.run(
        assistant._run_assistant(
            followup,
            SimpleNamespace(),
            text=followup.text,
            catalog_path=_catalog_path(tmp_path),
            auth_service=AuthService(frozenset({1})),
            orchestrator=AssistantOrchestrator(client),
            warehouse_service=None,
            conversation_memory=memory,
        )
    )
    assert shown.await_args.args[3] == "mojito-zero"
    assert memory.get(1) is None


def test_writeoff_followup_combines_only_structured_product_context(
    tmp_path, monkeypatch
) -> None:
    client = QueueIntentClient(
        AssistantIntent(
            intent="prepare_writeoff",
            confidence=9_000,
            items=[
                {
                    "product_text": "морковь",
                    "quantity_text": "500",
                    "unit_text": "грамм",
                    "comment_text": "сгнила",
                }
            ],
        )
    )
    memory = AssistantConversationMemory()
    prepare = AsyncMock()
    monkeypatch.setattr(assistant, "prepare_intake_message", prepare)
    path = _catalog_path(tmp_path)
    first = _message(1, "спиши морковь")
    asyncio.run(
        assistant._run_assistant(
            first,
            SimpleNamespace(),
            text=first.text,
            catalog_path=path,
            auth_service=AuthService(frozenset({1})),
            orchestrator=AssistantOrchestrator(client),
            warehouse_service=SimpleNamespace(),
            conversation_memory=memory,
        )
    )
    assert first.answer.await_args.args[0] == "Укажите количество."
    assert memory.get(1).last_query == "морковь"

    followup = _message(1, "500 грамм, сгнила")
    asyncio.run(
        assistant._run_assistant(
            followup,
            SimpleNamespace(),
            text=followup.text,
            catalog_path=path,
            auth_service=AuthService(frozenset({1})),
            orchestrator=AssistantOrchestrator(client),
            warehouse_service=SimpleNamespace(),
            conversation_memory=memory,
        )
    )
    assert client.calls[0][0] == "спиши морковь 500 грамм, сгнила"
    raw_intake = prepare.await_args.args[3]
    assert "морковь 500 грамм" in raw_intake
    assert "сгнила" in raw_intake
    assert memory.get(1) is None


def test_transfer_followup_preserves_product_and_requires_existing_preview(
    tmp_path, monkeypatch
) -> None:
    client = QueueIntentClient(
        AssistantIntent(
            intent="prepare_transfer",
            confidence=9_000,
            items=[
                {
                    "product_text": "кокосовое молоко",
                    "quantity_text": "1",
                    "unit_text": "литр",
                    "destination_text": "кухня",
                }
            ],
        )
    )
    memory = AssistantConversationMemory()
    prepare = AsyncMock()
    monkeypatch.setattr(assistant, "prepare_intake_message", prepare)
    path = _catalog_path(tmp_path)
    first = _message(1, "перемести кокосовое молоко")
    asyncio.run(
        assistant._run_assistant(
            first,
            SimpleNamespace(),
            text=first.text,
            catalog_path=path,
            auth_service=AuthService(frozenset({1})),
            orchestrator=AssistantOrchestrator(client),
            warehouse_service=SimpleNamespace(),
            conversation_memory=memory,
        )
    )
    assert memory.get(1).pending_slots == ("quantity", "destination")
    followup = _message(1, "литр на кухню")
    asyncio.run(
        assistant._run_assistant(
            followup,
            SimpleNamespace(),
            text=followup.text,
            catalog_path=path,
            auth_service=AuthService(frozenset({1})),
            orchestrator=AssistantOrchestrator(client),
            warehouse_service=SimpleNamespace(),
            conversation_memory=memory,
        )
    )
    assert client.calls[0][0] == "перемести кокосовое молоко литр на кухню"
    assert "-> кухня" in prepare.await_args.args[3]


def test_other_user_and_expired_context_are_never_reused(tmp_path) -> None:
    now = [10.0]
    memory = AssistantConversationMemory(ttl_seconds=5, clock=lambda: now[0])
    memory.remember(
        1,
        last_intent="prepare_writeoff",
        last_entity_type="warehouse_product",
        last_query="морковь",
        pending_slots=("quantity",),
    )
    client = QueueIntentClient(
        AssistantIntent(intent="unsupported"),
        AssistantIntent(intent="unsupported"),
    )
    path = _catalog_path(tmp_path)
    asyncio.run(
        assistant._run_assistant(
            _message(2, "500 грамм"),
            SimpleNamespace(),
            text="500 грамм",
            catalog_path=path,
            auth_service=AuthService(frozenset({1, 2})),
            orchestrator=AssistantOrchestrator(client),
            warehouse_service=SimpleNamespace(),
            conversation_memory=memory,
        )
    )
    assert client.calls[0][0] == "500 грамм"
    now[0] = 20.0
    asyncio.run(
        assistant._run_assistant(
            _message(1, "500 грамм"),
            SimpleNamespace(),
            text="500 грамм",
            catalog_path=path,
            auth_service=AuthService(frozenset({1})),
            orchestrator=AssistantOrchestrator(client),
            warehouse_service=SimpleNamespace(),
            conversation_memory=memory,
        )
    )
    assert client.calls[1][0] == "500 грамм"


def test_cancel_clears_context_without_provider_or_warehouse(tmp_path, monkeypatch) -> None:
    memory = AssistantConversationMemory()
    memory.remember(
        1,
        last_intent="prepare_writeoff",
        last_entity_type="warehouse_product",
        last_query="морковь",
    )
    client = QueueIntentClient()
    prepare = AsyncMock()
    monkeypatch.setattr(assistant, "prepare_intake_message", prepare)
    message = _message(1, "отмена")
    asyncio.run(
        assistant._run_assistant(
            message,
            SimpleNamespace(),
            text=message.text,
            catalog_path=_catalog_path(tmp_path),
            auth_service=AuthService(frozenset({1})),
            orchestrator=AssistantOrchestrator(client),
            warehouse_service=SimpleNamespace(),
            conversation_memory=memory,
        )
    )
    assert memory.get(1) is None
    assert client.calls == []
    prepare.assert_not_awaited()


def test_real_missing_mojito_reply_is_honest_without_provider(tmp_path) -> None:
    client = QueueIntentClient()
    message = _message(1, "найди техкарту безалкогольного мохито")
    asyncio.run(
        assistant._run_assistant(
            message,
            SimpleNamespace(),
            text=message.text,
            catalog_path=Path("data/catalog.json"),
            auth_service=AuthService(frozenset({1})),
            orchestrator=AssistantOrchestrator(client),
            warehouse_service=None,
            conversation_memory=AssistantConversationMemory(),
        )
    )
    assert client.calls == []
    assert message.answer.await_args.args[0] == (
        "Точной техкарты «Безалкогольный мохито» нет.\n\n"
        "Попробуйте другое название или часть названия."
    )


class RaisingIntentClient:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    async def classify(self, text, *, candidates=(), normalized_entity=""):
        raise self.error


def _remember(memory: AssistantConversationMemory) -> None:
    memory.remember(
        1,
        last_intent="prepare_writeoff",
        last_entity_type="warehouse_product",
        last_query="морковь",
        pending_slots=("quantity",),
    )


def test_provider_timeout_clears_context(tmp_path) -> None:
    memory = AssistantConversationMemory()
    _remember(memory)
    message = _message(1, "500 грамм")
    asyncio.run(
        assistant._run_assistant(
            message,
            SimpleNamespace(),
            text=message.text,
            catalog_path=_catalog_path(tmp_path),
            auth_service=AuthService(frozenset({1})),
            orchestrator=AssistantOrchestrator(
                RaisingIntentClient(
                    ProviderError("gemini", ProviderErrorKind.TIMEOUT)
                )
            ),
            warehouse_service=SimpleNamespace(),
            conversation_memory=memory,
        )
    )
    assert memory.get(1) is None
    assert message.answer.await_args.args[0] == assistant.AI_UNAVAILABLE_TEXT


def test_cancellation_clears_context_and_propagates(tmp_path) -> None:
    memory = AssistantConversationMemory()
    _remember(memory)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            assistant._run_assistant(
                _message(1, "500 грамм"),
                SimpleNamespace(),
                text="500 грамм",
                catalog_path=_catalog_path(tmp_path),
                auth_service=AuthService(frozenset({1})),
                orchestrator=AssistantOrchestrator(
                    RaisingIntentClient(asyncio.CancelledError())
                ),
                warehouse_service=SimpleNamespace(),
                conversation_memory=memory,
            )
        )
    assert memory.get(1) is None


def test_safe_observability_has_no_query_or_user_id(
    tmp_path, monkeypatch, caplog
) -> None:
    shown = AsyncMock()
    monkeypatch.setattr(assistant, "_show_item", shown)
    message = _message(987654, "Каманинская тигрица")
    with caplog.at_level(logging.INFO, logger="app.handlers.assistant"):
        asyncio.run(
            assistant._run_assistant(
                message,
                SimpleNamespace(),
                text=message.text,
                catalog_path=Path("data/catalog.json"),
                auth_service=AuthService(frozenset({987654})),
                orchestrator=AssistantOrchestrator(QueueIntentClient()),
                warehouse_service=None,
                conversation_memory=AssistantConversationMemory(),
            )
        )
    assert shown.await_count == 1
    assert "intent=find_tech_card" in caplog.text
    assert "local_exact_hit=True" in caplog.text
    assert "Каманинская" not in caplog.text
    assert "987654" not in caplog.text
