import asyncio
from pathlib import Path

import pytest

from app.handlers.common import load_catalog
from app.models.assistant import AssistantIntent, AssistantIntentName
from app.services.assistant.orchestrator import AssistantOrchestrator, AssistantToolRegistry
from app.services.warehouse.intake_parser import parse_intake_message


class NeverGemini:
    async def classify(self, text, *, candidates=()):
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


def test_unsupported_request_is_blocked_locally() -> None:
    intent = AssistantOrchestrator.local_intent("перезапусти бота")
    assert intent is not None
    assert intent.intent is AssistantIntentName.UNSUPPORTED
