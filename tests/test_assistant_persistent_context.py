"""Bounded persistent Bar Jarvis context."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.services.assistant.context import PersistentAssistantConversationMemory
from app.models.assistant import AssistantRelation, AssistantRequestedKind
from app.storage.assistant_conversation_database import AssistantConversationDatabase
from app.storage.assistant_conversation_database import AssistantConversationConflict
from app.storage.user_database import UserDatabase


def _memory(path: Path, now: list[datetime], **limits) -> PersistentAssistantConversationMemory:
    return PersistentAssistantConversationMemory(
        AssistantConversationDatabase(path), clock=lambda: now[0], **limits
    )


def test_context_survives_repository_reopen_and_persists_structure(tmp_path: Path) -> None:
    path = tmp_path / "context.db"
    UserDatabase(path).initialize()
    now = [datetime(2026, 10, 12, tzinfo=UTC)]
    first = _memory(path, now)
    first.append_turn(1, chat_id=10, role="user", text="кислота", topic="Кислотный раствор")
    first.remember(
        1, chat_id=10, last_intent="find_tech_card", last_entity_type="tech_card",
        last_query="кислота", candidate_ids=("a/_/b",), topic="Кислотный раствор",
        summary_text="Ищем кислоту", last_entity_id="a/_/b",
        last_entity_label="Кислотный раствор",
        requested_kind=AssistantRequestedKind.PREPARATION,
        relation=AssistantRelation.PREPARATION_FOR,
        relation_target="ягодный чай",
        parent_entity_id="tea/_/berry",
        related_entity_ids=("prep/_/base",),
        selected_entity_id="a/_/b",
        last_attempt="ягодная основа",
    )
    restored = _memory(path, now).get(1, 10)
    assert restored is not None
    assert restored.topic == "Кислотный раствор"
    assert restored.summary_text == "Ищем кислоту"
    assert restored.last_entity_id == "a/_/b"
    assert restored.requested_kind is AssistantRequestedKind.PREPARATION
    assert restored.relation is AssistantRelation.PREPARATION_FOR
    assert restored.relation_target == "ягодный чай"
    assert restored.parent_entity_id == "tea/_/berry"
    assert restored.related_entity_ids == ("prep/_/base",)
    assert restored.selected_entity_id == "a/_/b"
    assert restored.last_attempt == "ягодная основа"
    assert restored.turns[0].text == "кислота"

    first.clear_structured(1, chat_id=10)
    cleared = first.get(1, 10)
    assert cleared is not None
    assert cleared.relation is AssistantRelation.UNSPECIFIED
    assert cleared.related_entity_ids == ()
    assert cleared.last_successful_entity_id == "a/_/b"


def test_ttl_reset_and_user_chat_isolation(tmp_path: Path) -> None:
    path = tmp_path / "context.db"
    UserDatabase(path).initialize()
    now = [datetime(2026, 10, 12, tzinfo=UTC)]
    memory = _memory(path, now, ttl_seconds=10)
    memory.append_turn(1, chat_id=10, role="user", text="one")
    memory.append_turn(1, chat_id=11, role="user", text="other chat")
    memory.append_turn(2, chat_id=10, role="user", text="other user")
    memory.clear(1, 10)
    assert memory.get(1, 10) is None
    assert memory.get(1, 11) is not None
    assert memory.get(2, 10) is not None
    now[0] += timedelta(seconds=11)
    assert memory.get(1, 11) is None


def test_oldest_turns_evicted_by_count_and_characters(tmp_path: Path) -> None:
    path = tmp_path / "context.db"
    UserDatabase(path).initialize()
    now = [datetime(2026, 10, 12, tzinfo=UTC)]
    memory = _memory(path, now, max_turns=3, max_chars=9)
    for text in ("111", "222", "333", "444"):
        memory.append_turn(1, chat_id=1, role="user", text=text)
        now[0] += timedelta(seconds=1)
    context = memory.get(1, 1)
    assert context is not None
    assert [turn.text for turn in context.turns] == ["222", "333", "444"]
    assert len(context.turns) <= 3
    assert sum(len(turn.text) for turn in context.turns) <= 9


def test_optimistic_revision_rejects_lost_update(tmp_path: Path) -> None:
    path = tmp_path / "context.db"
    UserDatabase(path).initialize()
    now = [datetime(2026, 10, 12, tzinfo=UTC)]
    memory = _memory(path, now)
    saved = memory.append_turn(1, chat_id=1, role="user", text="first")
    repository = AssistantConversationDatabase(path)
    loaded = repository.load(1, 1, "2026-10-12T00:00:00Z")
    assert loaded is not None
    row, turns = loaded
    data = {
        "started_at_utc": row["started_at_utc"],
        "last_activity_at_utc": row["last_activity_at_utc"],
        "expires_at_utc": row["expires_at_utc"],
        "summary_text": "", "current_topic": "", "current_intent": "",
        "last_requested_action": "", "last_catalog_query": "",
        "last_entity_type": "conversation", "last_entity_id": "",
        "last_entity_label": "", "candidate_ids": (), "last_tool_name": "",
        "pending_clarification": "", "authorized_action": "",
    }
    repository.save(1, 1, data, turns, expected_revision=saved.revision, now="2026-10-12T00:00:01Z")
    with pytest.raises(AssistantConversationConflict):
        repository.save(1, 1, data, turns, expected_revision=saved.revision, now="2026-10-12T00:00:02Z")
