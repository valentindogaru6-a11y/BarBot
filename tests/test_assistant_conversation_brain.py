from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from app.models.assistant_planner import PlannerCapability
from app.services.assistant.context import AssistantConversationMemory
from app.services.assistant.context import PersistentAssistantConversationMemory
from app.services.assistant.conversation_brain import (
    ContextReferenceResolver,
    ConversationToolLoop,
    ReferenceKind,
    build_context_snapshot,
)
from app.services.assistant.long_term_memory import (
    AssistantLongTermMemoryService,
    AssistantMemoryKind,
    AssistantMemoryScope,
    AssistantMemorySource,
    parse_memory_command,
)
from app.storage.assistant_memory_database import AssistantMemoryDatabase
from app.storage.assistant_conversation_database import AssistantConversationDatabase
from app.storage.user_database import UserDatabase
from app.keyboards.callbacks import AssistantMemoryCallback


def test_context_truncation_summary_and_reference_resolution() -> None:
    memory = AssistantConversationMemory(max_turns=3, max_chars=100)
    memory.remember(
        1, chat_id=10, last_intent="catalog.get", last_entity_type="catalog",
        last_query="облепиха", last_entity_id="catalog/sea", last_entity_label="Облепиха",
        selected_entity_id="catalog/sea", last_successful_capability="catalog.get",
    )
    for value in ("one", "two", "three", "four"):
        memory.append_turn(1, chat_id=10, role="user", text=value)
    context = memory.get(1, 10)
    assert context is not None and len(context.turns) == 3
    assert "Архивировано реплик" in context.summary_text
    resolver = ContextReferenceResolver()
    assert resolver.resolve("что входит в него?", context).stable_id == "catalog/sea"
    assert resolver.resolve("что входит в неё?", context).stable_id == "catalog/sea"
    assert resolver.resolve("этот рецепт", context).kind is ReferenceKind.SELECTED_ENTITY
    assert memory.get(2, 20) is None
    memory.clear(1, 10)
    assert memory.get(1, 10) is None


def test_snapshot_is_bounded_redacted_and_runtime_filtered() -> None:
    cap = PlannerCapability(
        capability_id="catalog.get", domain="catalog", read_only=True, mutation=False
    )
    snapshot = build_context_snapshot(
        context=None, actor_role="super_admin", access_mode="pilot_allowlist",
        capabilities=(cap,), feature_flags=("assistant",), owner_memories=("называй меня шеф",),
    )
    raw = snapshot.model_dump_json()
    assert snapshot.available_capability_ids == ("catalog.get",)
    assert "telegram" not in raw.casefold()
    assert len(raw.encode()) < 24_000


def test_tool_loop_reads_twice_but_mutation_only_previews() -> None:
    called: list[str] = []
    async def read(cap: str) -> str:
        called.append("read:" + cap); return "ok"
    async def preview(cap: str) -> str:
        called.append("preview:" + cap); return "opaque-proposal"
    loop = ConversationToolLoop()
    result = asyncio.run(loop.run(
        ("catalog.get", "warehouse.prepare_writeoff"),
        allowed={"catalog.get", "warehouse.prepare_writeoff"},
        is_read_only=lambda value: value == "catalog.get",
        execute_read=read, prepare_mutation=preview,
    ))
    assert called == ["read:catalog.get", "preview:warehouse.prepare_writeoff"]
    assert result[-1].proposal_ref == "opaque-proposal"
    with pytest.raises(ValueError):
        asyncio.run(loop.run(
            ("a", "b", "c"), allowed={"a", "b", "c"},
            is_read_only=lambda _: True, execute_read=read, prepare_mutation=preview,
        ))


def test_confirmed_memory_save_recall_supersede_delete(tmp_path) -> None:
    path = tmp_path / "memory.db"
    UserDatabase(path).initialize()
    service = AssistantLongTermMemoryService(
        AssistantMemoryDatabase(path), clock=lambda: datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert parse_memory_command("обычная фраза") is None
    assert parse_memory_command("запомни, что ПФ значит полуфабрикат").action == "preview_save"
    first = service.save_confirmed(
        1, scope=AssistantMemoryScope.USER, kind=AssistantMemoryKind.TERMINOLOGY,
        content="ПФ значит полуфабрикат", source=AssistantMemorySource.EXPLICIT_REMEMBER,
    )
    second = service.save_confirmed(
        1, scope=AssistantMemoryScope.USER, kind=AssistantMemoryKind.TERMINOLOGY,
        content="ПФ означает заготовку", source=AssistantMemorySource.CONFIRMED_PREVIEW,
        supersedes_token=first.token,
    )
    assert [item.content for item in service.list(1)] == ["ПФ означает заготовку"]
    assert service.list(2) == ()
    service.delete_confirmed(1, token=second.token, revision=second.revision)
    assert service.list(1) == ()


def test_summary_and_pending_state_survive_restart(tmp_path) -> None:
    path = tmp_path / "context.db"
    UserDatabase(path).initialize()
    clock = lambda: datetime(2026, 1, 1, tzinfo=UTC)
    first = PersistentAssistantConversationMemory(
        AssistantConversationDatabase(path), max_turns=2, max_chars=40, clock=clock
    )
    first.remember(
        1, chat_id=10, last_intent="catalog.manage", last_entity_type="catalog",
        last_query="облепиха", selected_entity_id="catalog/sea",
        last_entity_label="Облепиха", dialogue_state="clarifying",
        pending_task="catalog.manage", last_successful_capability="catalog.get",
    )
    for text in ("первая реплика", "вторая реплика", "третья реплика"):
        first.append_turn(1, chat_id=10, role="user", text=text)
    second = PersistentAssistantConversationMemory(
        AssistantConversationDatabase(path), max_turns=2, max_chars=40, clock=clock
    )
    loaded = second.get(1, 10)
    assert loaded is not None
    assert loaded.selected_entity_id == "catalog/sea"
    assert loaded.pending_task == "catalog.manage"
    assert loaded.dialogue_state == "clarifying"
    assert "Архивировано реплик" in loaded.summary_text
    assert second.get(1, 11) is None


def test_memory_preview_confirmation_cancel_restart_and_callback_limit(tmp_path) -> None:
    path = tmp_path / "preview.db"
    UserDatabase(path).initialize()
    clock = lambda: datetime(2026, 1, 1, tzinfo=UTC)
    first = AssistantLongTermMemoryService(AssistantMemoryDatabase(path), clock=clock)
    preview = first.create_preview(
        1, scope=AssistantMemoryScope.USER, kind=AssistantMemoryKind.TERMINOLOGY,
        content="для меня ПФ означает полуфабрикат",
        source=AssistantMemorySource.EXPLICIT_REMEMBER,
    )
    assert first.list(1) == ()
    assert len(AssistantMemoryCallback(action="c", token=preview.token, revision=preview.revision).pack().encode()) <= 64
    restarted = AssistantLongTermMemoryService(AssistantMemoryDatabase(path), clock=clock)
    saved = restarted.confirm_preview(1, token=preview.token, revision=preview.revision)
    duplicate = restarted.confirm_preview(1, token=preview.token, revision=preview.revision)
    assert saved.content == duplicate.content
    assert len(restarted.list(1)) == 1
    assert restarted.list(2) == ()
    cancelled = restarted.create_preview(
        1, scope=AssistantMemoryScope.USER, kind=AssistantMemoryKind.BUSINESS_FACT,
        content="тестовая запись", source=AssistantMemorySource.EXPLICIT_REMEMBER,
    )
    restarted.cancel_preview(1, token=cancelled.token, revision=cancelled.revision)
    assert [item.content for item in restarted.list(1)] == [saved.content]
    with pytest.raises(ValueError, match="Sensitive"):
        restarted.create_preview(
            1, scope=AssistantMemoryScope.USER, kind=AssistantMemoryKind.STANDING_INSTRUCTION,
            content="BOT_TOKEN=do-not-store", source=AssistantMemorySource.EXPLICIT_REMEMBER,
        )
