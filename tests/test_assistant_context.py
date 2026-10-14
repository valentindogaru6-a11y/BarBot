"""Tests for bounded process-local assistant conversation context."""

from app.services.assistant.context import AssistantConversationMemory


class Clock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


def test_context_is_per_user_structured_and_clearable() -> None:
    memory = AssistantConversationMemory()
    memory.remember(
        1,
        last_intent="find_tech_card",
        last_entity_type="tech_card",
        last_query="мохито",
        candidate_ids=("a/_/one",),
        pending_slots=("candidate",),
    )
    assert memory.get(1) is not None
    assert memory.get(2) is None
    memory.clear(1)
    assert memory.get(1) is None


def test_context_expires_and_is_not_reused() -> None:
    clock = Clock()
    memory = AssistantConversationMemory(ttl_seconds=10, clock=clock)
    memory.remember(
        1,
        last_intent="prepare_writeoff",
        last_entity_type="warehouse_product",
        last_query="морковь",
        pending_slots=("quantity",),
        authorized_action="prepare_writeoff",
    )
    clock.value = 110.0
    assert memory.get(1) is None
    assert len(memory) == 0


def test_memory_is_bounded_and_restart_starts_empty() -> None:
    memory = AssistantConversationMemory(max_entries=2)
    for user_id in (1, 2, 3):
        memory.remember(
            user_id,
            last_intent="find_tech_card",
            last_entity_type="tech_card",
            last_query=str(user_id),
        )
    assert len(memory) == 2
    assert memory.get(1) is None
    assert len(AssistantConversationMemory()) == 0


def test_memory_truncates_untrusted_structured_fields() -> None:
    memory = AssistantConversationMemory()
    context = memory.remember(
        1,
        last_intent="find_tech_card",
        last_entity_type="tech_card",
        last_query="x" * 500,
        candidate_ids=tuple(str(value) for value in range(20)),
        pending_slots=tuple(str(value) for value in range(20)),
    )
    assert len(context.last_query) == 200
    assert len(context.candidate_ids) == 5
    assert len(context.pending_slots) == 8


def test_completing_structured_context_revokes_callbacks_but_keeps_turns() -> None:
    memory = AssistantConversationMemory()
    memory.append_turn(1, role="user", text="покажи карту")
    memory.remember(
        1,
        last_intent="find_tech_card",
        last_entity_type="tech_card",
        last_query="мохито",
        candidate_ids=("a/_/one",),
        pending_slots=("candidate",),
        authorized_action="prepare_transfer",
    )
    memory.clear_structured(1, last_intent="find_tech_card")
    context = memory.get(1)
    assert context is not None
    assert context.candidate_ids == ()
    assert context.pending_slots == ()
    assert context.authorized_action == ""
    assert memory.provider_history(1) == (("user", "покажи карту"),)


def test_failed_attempt_does_not_replace_successful_entity_and_chats_are_isolated() -> None:
    memory = AssistantConversationMemory()
    memory.remember(
        1, chat_id=10, last_intent="find_tech_card",
        last_entity_type="tech_card", last_query="ягодное пюре",
        last_entity_id="prep/_/berry", last_entity_label="Ягодное пюре",
        selected_entity_id="prep/_/berry",
    )
    memory.remember(
        1, chat_id=10, last_intent="not_found",
        last_entity_type="catalog_attempt", last_query="неизвестно",
        last_attempt="неизвестно",
    )
    assert memory.get(1, 10).last_successful_entity_id == "prep/_/berry"
    assert memory.get(1, 11) is None


def test_reset_clears_last_successful_entity() -> None:
    memory = AssistantConversationMemory()
    memory.remember(
        1, last_intent="find_tech_card", last_entity_type="tech_card",
        last_query="ягодное пюре", last_entity_id="prep/_/berry",
        last_entity_label="Ягодное пюре", selected_entity_id="prep/_/berry",
    )
    memory.clear(1)
    assert memory.get(1) is None
