import asyncio
from decimal import Decimal
from pathlib import Path

import pytest

from app.handlers.common import load_catalog
from app.models.assistant import AssistantIntent, AssistantIntentName
from app.services.assistant.orchestrator import AssistantOrchestrator, AssistantToolRegistry
from app.services.assistant.bar_language import parse_bar_request
from app.services.warehouse.intake_parser import parse_intake_message


class NeverGemini:
    async def classify(
        self,
        text,
        *,
        candidates=(),
        normalized_entity="",
        conversation=(),
        allowed_action_intents=(),
    ):
        raise AssertionError("Gemini must not be called for deterministic requests")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("спиши морковь", AssistantIntentName.PREPARE_WRITEOFF),
        ("СПИСАТЬ морковь", AssistantIntentName.PREPARE_WRITEOFF),
        ("сделай списание моркови", AssistantIntentName.PREPARE_WRITEOFF),
        ("добавь в списание морковь", AssistantIntentName.PREPARE_WRITEOFF),
        ("оформи списание: морковь", AssistantIntentName.PREPARE_WRITEOFF),
        ("нужно списать 0,2 кг апельсина", AssistantIntentName.PREPARE_WRITEOFF),
        ("убери в списание апельсин 200 г", AssistantIntentName.PREPARE_WRITEOFF),
        ("списание: морковь", AssistantIntentName.PREPARE_WRITEOFF),
        ("перемести молоко", AssistantIntentName.PREPARE_TRANSFER),
        ("перенеси молоко", AssistantIntentName.PREPARE_TRANSFER),
        ("отправь молоко на кухню", AssistantIntentName.PREPARE_TRANSFER),
        ("сделай перемещение молока", AssistantIntentName.PREPARE_TRANSFER),
        ("оформи перемещение молока", AssistantIntentName.PREPARE_TRANSFER),
        ("перемещение: молоко", AssistantIntentName.PREPARE_TRANSFER),
        ("перенеси 500 мл сиропа в кондитерку", AssistantIntentName.PREPARE_TRANSFER),
    ],
)
def test_local_action_gate_accepts_only_explicit_commands(text, expected) -> None:
    assert AssistantOrchestrator.local_action_evidence(text) is expected


@pytest.mark.parametrize(
    "text",
    [
        "расскажи про кокосовое молоко",
        "что такое списание",
        "как правильно оформлять перемещение",
        "можно ли перемещать молоко",
        "почему списывают морковь",
        "покажи операции перемещения",
        "как правильно списывать продукты",
        "можно ли списать апельсин",
        "расскажи про перемещение",
        "сколько апельсина обычно списывают",
        "покажи списания за сегодня",
        "почему вчера списали апельсин",
    ],
)
def test_local_action_gate_rejects_informational_phrases(text) -> None:
    assert AssistantOrchestrator.local_action_evidence(text) is None


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


@pytest.mark.parametrize(
    ("text", "intent_name", "product", "quantity", "unit", "destination"),
    [
        ("спиши апельсин 200 грамм", AssistantIntentName.PREPARE_WRITEOFF, "апельсин", "200", "грамм", None),
        ("списать апельсин 200 г", AssistantIntentName.PREPARE_WRITEOFF, "апельсин", "200", "г", None),
        ("перемести сахар 1 кг на кухню", AssistantIntentName.PREPARE_TRANSFER, "сахар", "1", "кг", "кухня"),
        ("перенеси 500 мл сиропа в кондитерку", AssistantIntentName.PREPARE_TRANSFER, "сиропа", "500", "мл", "кондитерка"),
    ],
)
def test_complete_natural_mutations_are_local_and_typed(
    text, intent_name, product, quantity, unit, destination
) -> None:
    parsed = parse_bar_request(text)
    assert parsed.quantity == Decimal(quantity)
    intent = asyncio.run(AssistantOrchestrator(NeverGemini()).interpret(text))
    assert intent.intent is intent_name
    assert intent.items[0].product_text == product
    assert intent.items[0].quantity_text == quantity
    assert intent.items[0].unit_text == unit
    assert intent.items[0].destination_text == destination


def test_local_mutation_comment_is_preserved_without_provider() -> None:
    intent = asyncio.run(
        AssistantOrchestrator(NeverGemini()).interpret(
            "спиши апельсин 200 г, испортился"
        )
    )
    assert intent.items[0].comment_text == "испортился"


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


def test_explicit_web_routes_to_search_but_action_keeps_priority() -> None:
    web = AssistantOrchestrator.local_intent(
        "найди в интернете свежую рецептуру настойки"
    )
    assert web is not None and web.intent is AssistantIntentName.WEB_SEARCH
    local = AssistantOrchestrator.local_intent(
        "перемести кокосовое молоко сейчас 1 л на кухню"
    )
    assert local is not None
    assert local.intent is AssistantIntentName.PREPARE_TRANSFER
