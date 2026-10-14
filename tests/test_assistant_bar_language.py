"""Local-first regressions for practical bar vocabulary and routing."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers import assistant
from app.handlers.common import load_catalog
from app.services.assistant import (
    AssistantCatalogRetriever,
    AssistantConversationMemory,
    AssistantOrchestrator,
    AssistantToolRegistry,
    ProviderError,
    ProviderErrorKind,
    capability_ids_for_domain,
    parse_bar_request,
)
from app.services.auth_service import AuthService
from app.models.catalog import Catalog
from app.models.assistant import (
    AssistantIntent,
    AssistantRelation,
    AssistantRequestedKind,
)
from pydantic import ValidationError


class NeverProvider:
    def __init__(self, error: BaseException | None = None) -> None:
        self.calls = 0
        self.error = error

    async def classify(self, *args, **kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        raise AssertionError("local catalog language must not call the provider")


@pytest.mark.parametrize(
    ("text", "entity"),
    [
        ("найди мне пфку для ягодного чая", "ягодный чай"),
        ("покажи П/Ф для ягодного чая", "ягодный чай"),
        ("где полуфабрикат для ягодного чая", "ягодный чай"),
        ("нужна заготовка для ягодного чая", "ягодный чай"),
    ],
)
def test_preparation_language_is_structured_without_provider(text, entity) -> None:
    parsed = parse_bar_request(text)
    assert parsed.action == "search"
    assert parsed.domain == "catalog"
    assert parsed.entity == entity
    assert parsed.relation == "preparation_for"
    assert parsed.requested_kind == "preparation"
    assert parsed.mutation is False


@pytest.mark.parametrize(
    "text",
    [
        "найди мне пфку для ягодного чая",
        "найди пфку для ягоднаго чая",
        "покажи заготовку на ягодный чай",
    ],
)
def test_real_catalog_preparation_query_returns_relevant_local_candidate(text) -> None:
    catalog = load_catalog(Path("data/catalog.json"))
    intent = AssistantOrchestrator.local_intent(text)
    assert intent is not None
    assert intent.query in {"ягодный чай", "ягоднаго чай"}
    action = AssistantToolRegistry(catalog).execute(intent)
    names = [result.item.name for result in action.search_results]
    assert "Ягодная основа для чая" in names


def test_prep_query_never_becomes_mutation() -> None:
    assert AssistantOrchestrator.local_action_evidence(
        "найди мне пфку для ягодного чая"
    ) is None
    assert parse_bar_request("что списывают в ягодный чай").mutation is False


def test_structured_mutation_fields_do_not_weaken_explicit_action_gate() -> None:
    parsed = parse_bar_request("перемести молоко 0,25 л -> кухня")
    assert parsed.action == "transfer"
    assert parsed.entity == "молоко"
    assert parsed.quantity == "0.25"
    assert parsed.unit == "л"
    assert parsed.destination == "кухня"
    assert parsed.mutation is True


def test_preparation_relation_resolves_parent_card_ingredient() -> None:
    retriever = AssistantCatalogRetriever(load_catalog(Path("data/catalog.json")))
    result = retriever.retrieve("найди пф для Berry radiance")
    assert [item.item.name for item in result.exact] == ["Ягодное пюре"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("найди пф для Berry radiance", {"Ягодное пюре"}),
        ("покажи полуфабрикат для Berry radiance", {"Ягодное пюре"}),
        ("покажи что входит в ягодный чай", {"Ягодная основа для чая"}),
        ("что в составе ягодной основы для чая", {"Ягодная основа для чая"}),
        ("найди техничку ягодного чая", {"Ягодная основа для чая"}),
        ("покажи тех карту ягодной основы", {"Ягодная основа для чая"}),
        ("найди мне пфку для ягодного чая", {"Ягодная основа для чая"}),
        ("покажи заготовку для ягодного чая", {"Ягодная основа для чая"}),
        ("где рецепт п/ф для ягодного чая", {"Ягодная основа для чая"}),
    ],
)
def test_live_relation_phrases_keep_semantics_locally(text, expected) -> None:
    catalog = load_catalog(Path("data/catalog.json"))
    intent = AssistantOrchestrator.local_intent(text)
    assert intent is not None
    action = AssistantToolRegistry(catalog).execute(intent)
    names = {result.item.name for result in action.search_results}
    assert expected <= names


@pytest.mark.parametrize(
    "text",
    [
        "найди пф для Berry radiance",
        "покажи полуфабрикат для Berry radiance",
        "покажи что входит в ягодный чай",
        "что в составе ягодной основы для чая",
        "найди техничку ягодного чая",
        "покажи тех карту ягодной основы",
    ],
)
def test_live_relation_phrases_never_call_provider(text) -> None:
    provider = NeverProvider()
    intent = asyncio.run(AssistantOrchestrator(provider).interpret(text))
    assert intent.intent.value == "find_tech_card"
    assert provider.calls == 0


def _relation_catalog(*, second_child: bool = False, missing_child: bool = False) -> Catalog:
    ingredients = [{"name": "Пряная основа п/ф", "amount": 10, "unit": "мл"}]
    children = [{"id": "spice-base", "name": "Пряная основа", "tags": ["полуфабрикат"]}]
    if second_child:
        ingredients.append({"name": "Чайный сироп пф", "amount": 5, "unit": "мл"})
        children.append({"id": "tea-syrup", "name": "Чайный сироп", "tags": ["заготовка"]})
    if missing_child:
        ingredients = [{"name": "Несуществующая основа пф", "amount": 10, "unit": "мл"}]
    return Catalog.model_validate({
        "version": 1,
        "categories": [
            {"id": "drinks", "name": "Напитки", "items": [{"id": "parent", "name": "Пряный чай", "ingredients": ingredients}]},
            {"id": "prep", "name": "Полуфабрикаты", "items": children},
        ],
    })


@pytest.mark.parametrize(("second_child", "count"), [(False, 1), (True, 2)])
def test_generic_parent_relation_returns_one_or_multiple_children(second_child, count) -> None:
    retrieval = AssistantCatalogRetriever(_relation_catalog(second_child=second_child)).retrieve(
        "пряный чай",
        requested_kind=AssistantRequestedKind.PREPARATION,
        relation=AssistantRelation.PREPARATION_FOR,
        relation_target="пряный чай",
    )
    assert len(retrieval.exact) == count
    assert retrieval.parent_entity_ids == ("drinks/_/parent",)


def test_missing_or_fuzzy_relation_child_is_never_selected_silently() -> None:
    retrieval = AssistantCatalogRetriever(_relation_catalog(missing_child=True)).retrieve(
        "пряный чай",
        requested_kind=AssistantRequestedKind.PREPARATION,
        relation=AssistantRelation.PREPARATION_FOR,
        relation_target="пряный чай",
    )
    assert retrieval.exact == ()
    assert retrieval.candidates == ()
    assert retrieval.parent_entity_ids == ("drinks/_/parent",)


def test_assistant_intent_relation_contract_is_strict() -> None:
    intent = AssistantIntent(
        intent="find_tech_card", query="чай", domain="catalog",
        requested_kind="preparation", relation="preparation_for",
        relation_target="чай",
    )
    assert intent.relation is AssistantRelation.PREPARATION_FOR
    with pytest.raises(ValidationError):
        AssistantIntent(intent="find_tech_card", query="чай", relation="invented")
    with pytest.raises(ValidationError):
        AssistantIntent(intent="find_tech_card", query="чай", invented=True)


def test_explanatory_preparation_phrase_is_not_a_catalog_lookup() -> None:
    assert AssistantOrchestrator.local_intent("расскажи, что такое ПФ") is None


def test_domain_capabilities_are_minimal_and_keep_runtime_filtering() -> None:
    available = (
        "catalog.search", "catalog.get", "catalog.manage",
        "warehouse.prepare_writeoff", "users.list", "capabilities.list",
    )
    assert capability_ids_for_domain(available, "catalog") == (
        "catalog.search", "catalog.get", "catalog.manage",
    )
    assert capability_ids_for_domain(available, "general") == (
        "capabilities.list",
    )


def _message(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        message_id=1,
        from_user=SimpleNamespace(id=1),
        chat=SimpleNamespace(id=1, type="private"),
        answer=AsyncMock(),
    )


@pytest.mark.parametrize(
    "error_kind",
    [
        ProviderErrorKind.TIMEOUT,
        ProviderErrorKind.RATE_LIMIT,
        ProviderErrorKind.SERVER,
        ProviderErrorKind.MALFORMED_RESPONSE,
    ],
)
def test_confident_local_read_survives_provider_failures(error_kind, monkeypatch) -> None:
    provider = NeverProvider(ProviderError("gemini", error_kind))
    message = _message("найди мне пфку для ягодного чая")
    shown = AsyncMock()
    monkeypatch.setattr(assistant, "_show_item", shown)
    asyncio.run(
        assistant._run_assistant(
            message,
            SimpleNamespace(),
            text=message.text,
            catalog_path=Path("data/catalog.json"),
            auth_service=AuthService(frozenset({1})),
            orchestrator=AssistantOrchestrator(provider),
            warehouse_service=None,
            conversation_memory=AssistantConversationMemory(),
        )
    )
    assert provider.calls == 0
    shown.assert_awaited_once()
    assert shown.await_args.args[3] == "yagodnyy_p_f"


@pytest.mark.parametrize(
    "error_kind",
    [
        ProviderErrorKind.TIMEOUT,
        ProviderErrorKind.RATE_LIMIT,
        ProviderErrorKind.SERVER,
        ProviderErrorKind.MALFORMED_RESPONSE,
    ],
)
def test_provider_failure_keeps_confident_read_only_candidates(error_kind) -> None:
    provider = NeverProvider(ProviderError("gemini", error_kind))
    message = _message("кислотный раствр")
    asyncio.run(
        assistant._run_assistant(
            message,
            SimpleNamespace(),
            text=message.text,
            catalog_path=Path("data/catalog.json"),
            auth_service=AuthService(frozenset({1})),
            orchestrator=AssistantOrchestrator(provider),
            warehouse_service=None,
            conversation_memory=AssistantConversationMemory(),
        )
    )
    assert provider.calls == 1
    assert message.answer.await_args.args[0] == "Возможно, вы имели в виду:"


def test_context_keeps_stable_candidate_ids_for_followup(monkeypatch) -> None:
    provider = NeverProvider()
    memory = AssistantConversationMemory()
    message = _message("найди мне пфку для ягодного чая")
    monkeypatch.setattr(assistant, "_show_item", AsyncMock())
    asyncio.run(
        assistant._run_assistant(
            message,
            SimpleNamespace(),
            text=message.text,
            catalog_path=Path("data/catalog.json"),
            auth_service=AuthService(frozenset({1})),
            orchestrator=AssistantOrchestrator(provider),
            warehouse_service=None,
            conversation_memory=memory,
        )
    )
    context = memory.get(1)
    assert context is not None
    assert context.last_entity_id.endswith("/yagodnyy_p_f")
    assert context.last_entity_label == "Ягодная основа для чая"
    assert provider.calls == 0


def test_moderate_typo_stays_explicit_candidate_not_silent_exact() -> None:
    retriever = AssistantCatalogRetriever(load_catalog(Path("data/catalog.json")))
    result = retriever.retrieve("ягоднаго чая")
    assert result.exact == ()
    assert any(
        candidate.result.item.name == "Ягодная основа для чая"
        for candidate in result.candidates
    )


def _followup_catalog(tmp_path: Path) -> Path:
    catalog = Catalog.model_validate({
        "version": 1,
        "categories": [{
            "id": "tea",
            "name": "Чай",
            "items": [
                {"id": "berry-one", "name": "Ягодный чай первый"},
                {"id": "berry-two", "name": "Ягодный чай второй"},
            ],
        }],
    })
    path = tmp_path / "catalog.json"
    path.write_text(catalog.model_dump_json(), encoding="utf-8")
    return path


def test_ordinal_followup_uses_stable_candidate_id_without_provider(
    tmp_path, monkeypatch
) -> None:
    provider = NeverProvider()
    memory = AssistantConversationMemory()
    shown = AsyncMock()
    monkeypatch.setattr(assistant, "_show_item", shown)
    path = _followup_catalog(tmp_path)

    first = _message("покажи ягодный чай")
    asyncio.run(assistant._run_assistant(
        first, SimpleNamespace(), text=first.text, catalog_path=path,
        auth_service=AuthService(frozenset({1})),
        orchestrator=AssistantOrchestrator(provider), warehouse_service=None,
        conversation_memory=memory,
    ))
    candidate_ids = memory.get(1).candidate_ids
    assert len(candidate_ids) == 2
    expected_item_id = candidate_ids[1].rsplit("/", 1)[-1]

    second = _message("покажи вторую")
    asyncio.run(assistant._run_assistant(
        second, SimpleNamespace(), text=second.text, catalog_path=path,
        auth_service=AuthService(frozenset({1})),
        orchestrator=AssistantOrchestrator(provider), warehouse_service=None,
        conversation_memory=memory,
    ))
    assert shown.await_args.args[3] == expected_item_id
    assert provider.calls == 0


def test_card_contents_followup_reuses_stable_entity_without_provider(
    tmp_path, monkeypatch
) -> None:
    provider = NeverProvider()
    memory = AssistantConversationMemory()
    shown = AsyncMock()
    monkeypatch.setattr(assistant, "_show_item", shown)
    path = _followup_catalog(tmp_path)

    first = _message("покажи техкарту ягодный чай первый")
    asyncio.run(assistant._run_assistant(
        first, SimpleNamespace(), text=first.text, catalog_path=path,
        auth_service=AuthService(frozenset({1})),
        orchestrator=AssistantOrchestrator(provider), warehouse_service=None,
        conversation_memory=memory,
    ))
    second = _message("что входит в нее?")
    asyncio.run(assistant._run_assistant(
        second, SimpleNamespace(), text=second.text, catalog_path=path,
        auth_service=AuthService(frozenset({1})),
        orchestrator=AssistantOrchestrator(provider), warehouse_service=None,
        conversation_memory=memory,
    ))
    assert shown.await_count == 2
    assert shown.await_args.args[3] == "berry-one"
    assert provider.calls == 0
