"""Regression coverage for local-first assistant catalog retrieval."""

from pathlib import Path

import pytest

from app.handlers.common import load_catalog
from app.keyboards.assistant import build_assistant_candidates_keyboard
from app.keyboards.callbacks import AssistantCandidateCallback
from app.models.catalog import Catalog
from app.services.assistant.retrieval import (
    AssistantCatalogRetriever,
    extract_catalog_entity,
    normalize_assistant_text,
)


@pytest.fixture(scope="module")
def production_retriever() -> AssistantCatalogRetriever:
    return AssistantCatalogRetriever(load_catalog(Path("data/catalog.json")))


@pytest.mark.parametrize(
    "query",
    [
        "каманинская тигрица",
        "Каманинская Тигрица",
        "найди каманинскую тигрицу",
        "покажи техкарту каманинской тигрицы",
        "как приготовить каманинскую тигрицу",
        "тигрица каманинская",
    ],
)
def test_real_card_is_found_across_case_commands_inflection_and_token_order(
    production_retriever: AssistantCatalogRetriever,
    query: str,
) -> None:
    result = production_retriever.retrieve(query)
    assert [match.item.name for match in result.exact] == [
        "Каманинская тигрица"
    ]


@pytest.mark.parametrize(
    "query",
    [
        "мохито",
        "найди техкарту мохито",
        "безалкогольное мохито",
        "найди техкарту безалкогольного мохито",
    ],
)
def test_mojito_expectation_follows_real_catalog_and_does_not_invent_card(
    production_retriever: AssistantCatalogRetriever,
    query: str,
) -> None:
    result = production_retriever.retrieve(query)
    assert result.exact == ()
    assert result.candidates == ()


def test_small_typo_is_a_candidate_not_silent_exact_selection(
    production_retriever: AssistantCatalogRetriever,
) -> None:
    result = production_retriever.retrieve("каманинская тигрицаа")
    assert result.exact == ()
    assert [candidate.result.item.name for candidate in result.candidates] == [
        "Каманинская тигрица"
    ]


def test_unknown_card_has_no_irrelevant_candidates(
    production_retriever: AssistantCatalogRetriever,
) -> None:
    result = production_retriever.retrieve("совершенно неизвестная карта квазара")
    assert result.exact == ()
    assert result.candidates == ()


def _ambiguous_catalog() -> Catalog:
    return Catalog.model_validate(
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
                                {"id": "mojito-zero", "name": "Безалкогольный мохито"},
                            ],
                        }
                    ],
                    "items": [
                        {
                            "id": "mojito-classic",
                            "name": "Классический мохито",
                            "voice_aliases": ["мохито классик"],
                            "tags": ["лайм", "мята"],
                        }
                    ],
                }
            ],
        }
    )


def test_ambiguous_partial_name_returns_bounded_candidates() -> None:
    result = AssistantCatalogRetriever(_ambiguous_catalog()).retrieve("мохито")
    assert result.exact == ()
    assert {candidate.result.item.id for candidate in result.candidates} == {
        "mojito-classic",
        "mojito-zero",
    }


def test_style_modifier_resolves_unambiguous_variant() -> None:
    result = AssistantCatalogRetriever(_ambiguous_catalog()).retrieve(
        "мохито безалкогольный"
    )
    assert [match.item.id for match in result.exact] == ["mojito-zero"]


def test_alias_and_existing_searchable_fields_are_used() -> None:
    retriever = AssistantCatalogRetriever(_ambiguous_catalog())
    assert [match.item.id for match in retriever.retrieve("мохито классик").exact] == [
        "mojito-classic"
    ]
    assert [
        candidate.result.item.id
        for candidate in retriever.retrieve("мята").candidates
    ] == ["mojito-classic"]


def test_unicode_punctuation_and_command_words_are_normalized() -> None:
    assert normalize_assistant_text("  Ёжик—БАР! ") == "ежик бар"
    assert extract_catalog_entity("Покажи технологическую карту: мохито") == "мохито"


def test_candidate_callbacks_are_bounded_and_contain_no_query_or_user_id() -> None:
    result = AssistantCatalogRetriever(_ambiguous_catalog()).retrieve("мохито")
    keyboard = build_assistant_candidates_keyboard(
        [candidate.result for candidate in result.candidates]
    )
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert callbacks
    assert all(value is not None and len(value.encode()) <= 64 for value in callbacks)
    assert all("мохито" not in value.casefold() for value in callbacks if value)
    maximum = AssistantCandidateCallback(
        category_id="a" * 18,
        item_id="b" * 18,
        subcategory_id="c" * 18,
    ).pack()
    assert len(maximum.encode()) <= 64
