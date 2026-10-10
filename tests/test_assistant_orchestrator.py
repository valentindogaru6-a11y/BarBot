import asyncio
from pathlib import Path

import pytest

from app.handlers.common import load_catalog
from app.models.assistant import AssistantIntent, AssistantIntentName
from app.services.assistant.orchestrator import AssistantOrchestrator, AssistantToolRegistry
from app.services.warehouse.intake_parser import parse_intake_message


class NeverGemini:
    async def classify(self, text, *, candidates=(), normalized_entity=""):
        raise AssertionError("Gemini must not be called for deterministic requests")


def test_local_tech_card_command_does_not_call_gemini() -> None:
    intent = asyncio.run(
        AssistantOrchestrator(NeverGemini()).interpret("покажи техкарту Kiss Me")
    )
    assert intent.intent is AssistantIntentName.FIND_TECH_CARD
    assert intent.query == "Kiss Me"


def test_tool_registry_finds_catalog_card_without_internal_ids_in_model_output() -> None:
    catalog = load_catalog(Path("data/catalog.json"))
    action = AssistantToolRegistry(catalog).execute(
        AssistantIntent(intent="find_tech_card", query="Kiss Me")
    )
    assert action.kind == "find_tech_card"
    assert any(result.item.name == "Kiss Me" for result in action.search_results)


@pytest.mark.parametrize(
    ("intent", "expected_header", "expected_operation"),
    [
        (AssistantIntent(intent="prepare_writeoff", confidence=9000, items=[{"product_text": "морковь", "quantity_text": "0,5", "unit_text": "кг", "comment_text": "сгнила"}]), "списание:", "writeoff"),
        (AssistantIntent(intent="prepare_transfer", confidence=9000, items=[{"product_text": "молоко", "quantity_text": "1", "unit_text": "л", "destination_text": "кухня"}]), "перемещение:", "transfer"),
    ],
)
def test_warehouse_tools_only_create_canonical_existing_intake(intent, expected_header, expected_operation) -> None:
    catalog = load_catalog(Path("data/catalog.json"))
    action = AssistantToolRegistry(catalog).execute(intent)
    assert action.raw_intake.startswith(expected_header)
    parsed = parse_intake_message(action.raw_intake)
    assert parsed.operation_type.value == expected_operation
    assert len(parsed.lines) == 1


def test_missing_quantity_clarifies_without_guessing() -> None:
    catalog = load_catalog(Path("data/catalog.json"))
    action = AssistantToolRegistry(catalog).execute(
        AssistantIntent(intent="prepare_writeoff", confidence=9000, items=[{"product_text": "морковь"}])
    )
    assert action.kind == "clarify"
    assert action.raw_intake is None
    assert action.entity_query == "морковь"
    assert action.pending_slots == ("quantity",)


def test_partial_warehouse_commands_preserve_product_for_followup() -> None:
    writeoff = AssistantOrchestrator.local_intent("спиши морковь")
    transfer = AssistantOrchestrator.local_intent("перемести кокосовое молоко")
    assert writeoff is not None and writeoff.items[0].product_text == "морковь"
    assert transfer is not None
    assert transfer.items[0].product_text == "кокосовое молоко"


def test_real_missing_mojito_card_returns_honest_normalized_message() -> None:
    catalog = load_catalog(Path("data/catalog.json"))
    action = AssistantToolRegistry(catalog).execute(
        AssistantIntent(
            intent="find_tech_card",
            query="безалкогольного мохито",
        )
    )
    assert action.kind == "not_found"
    assert action.search_results == ()
    assert action.message == (
        "Точной техкарты «Безалкогольный мохито» нет.\n\n"
        "Попробуйте другое название или часть названия."
    )


def test_unsupported_request_is_blocked_locally() -> None:
    intent = AssistantOrchestrator.local_intent("перезапусти бота")
    assert intent is not None
    assert intent.intent is AssistantIntentName.UNSUPPORTED
