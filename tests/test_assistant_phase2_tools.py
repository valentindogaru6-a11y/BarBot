"""Local retrieval, follow-ups and read-only tool boundaries."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers.assistant import resolve_contextual_request
from app.handlers import assistant
from app.handlers.start import handle_start
from app.models.assistant_tools import SearchTechCardsInput
from app.services.access_service import AccessDeniedError, AccessService
from app.services.assistant.context import AssistantConversationContext
from app.services.assistant.retrieval import AssistantCatalogRetriever
from app.services.assistant.tools import AssistantReadOnlyTools
from app.services.assistant import AssistantOrchestrator, AssistantConversationMemory
from app.services.auth_service import AuthService
from app.storage.json_storage import JsonCatalogStorage


class FakeAttendance:
    def open_sessions(self, actor_user_id: int):
        return [{
            "display_name_snapshot": "Бармен", "username_snapshot": "bar",
            "source_location_name": "Бар", "business_date": "2026-10-12",
            "opened_at_utc": "2026-10-12T10:00:00Z", "status": "open",
        }]

    @staticmethod
    def duration_seconds(row):
        return 3600


def _context(**changes) -> AssistantConversationContext:
    data = dict(
        last_intent="find_tech_card", last_entity_type="tech_card",
        last_query="кислота", candidate_ids=(), pending_slots=(),
        topic="Кислотный раствор", turns=(), created_at=1, expires_at=2,
        last_entity_label="Кислотный раствор",
    )
    data.update(changes)
    return AssistantConversationContext(**data)


def test_contextual_similar_and_offline_followups() -> None:
    similar, ambiguous = resolve_contextual_request("найди что-то похожее", _context())
    assert similar == "покажи техкарту кислота"
    assert not ambiguous
    offline, ambiguous = resolve_contextual_request("ну выдай что знаешь без интернета", _context(topic="неклассические варианты Голубой лагуны"))
    assert "неклассические варианты Голубой лагуны" in offline
    assert not ambiguous
    assert resolve_contextual_request("а как сделать его прозрачнее?", None)[1]


def test_catalog_phase2_examples_and_stable_ranking() -> None:
    catalog = JsonCatalogStorage("data/catalog.json").load()
    retriever = AssistantCatalogRetriever(catalog)
    acid = retriever.retrieve("техкарта кислоты")
    assert acid.candidates[0].result.item.name == "Кислотный раствор"
    assert retriever.retrieve("найди кислотный раствор").exact[0].item.name == "Кислотный раствор"
    assert retriever.retrieve("каманинская тигрица").exact[0].item.name == "Каманинская тигрица"
    assert retriever.retrieve("тигрица каманинская").exact[0].item.name == "Каманинская тигрица"
    assert retriever.retrieve("кислотный раствр").candidates[0].result.item.name == "Кислотный раствор"
    ingredients = retriever.retrieve("что у нас есть с яблочной кислотой")
    assert any(candidate.reason == "metadata_tokens" for candidate in ingredients.candidates)
    assert retriever.retrieve("кислотный раствр") == retriever.retrieve("кислотный раствр")


def test_read_only_tools_recheck_super_admin_and_return_bounded_models() -> None:
    catalog = JsonCatalogStorage("data/catalog.json").load()
    access = AccessService(frozenset({1}))
    tools = AssistantReadOnlyTools(catalog, access, attendance=FakeAttendance())
    sessions = tools.list_open_attendance_sessions(1)
    assert sessions[0].employee == "Бармен"
    assert sessions[0].duration_seconds == 3600
    assert tools.search_tech_cards(1, SearchTechCardsInput(query="кислота"))[0].name == "Кислотный раствор"
    with pytest.raises(AccessDeniedError):
        tools.list_open_attendance_sessions(2)


def test_open_attendance_question_uses_local_tool_without_provider() -> None:
    message = SimpleNamespace(
        text="Посмотри, чьи смены до сих пор открыты у нас",
        from_user=SimpleNamespace(id=1),
        chat=SimpleNamespace(id=1, type="private"),
        answer=AsyncMock(),
    )
    asyncio.run(
        assistant._run_assistant(
            message,
            SimpleNamespace(),
            text=message.text,
            catalog_path=JsonCatalogStorage().path,
            auth_service=AuthService(frozenset({1})),
            orchestrator=AssistantOrchestrator(None),
            warehouse_service=None,
            conversation_memory=AssistantConversationMemory(),
            warehouse_attendance_service=FakeAttendance(),
        )
    )
    assert "Бармен" in message.answer.await_args.args[0]


def test_start_clears_only_assistant_context_before_main_menu() -> None:
    memory = AssistantConversationMemory()
    memory.append_turn(1, chat_id=1, role="user", text="old AI candidate")
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        chat=SimpleNamespace(id=1, type="private"),
        answer=AsyncMock(),
    )
    state = SimpleNamespace(get_state=AsyncMock(return_value=None), clear=AsyncMock())
    bot = SimpleNamespace(set_my_commands=AsyncMock())
    asyncio.run(
        handle_start(
            message, state, AuthService(frozenset({1})), bot,
            assistant_conversation_memory=memory,
        )
    )
    assert memory.get(1, 1) is None
    state.clear.assert_awaited_once()
    assert "BarBot" in message.answer.await_args.args[0]
