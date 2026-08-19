"""Tests for voice aliases, fuzzy ranking, thresholds, and active filtering."""

from app.models.catalog import Catalog
from app.services.voice_matcher import (
    HIGH_CONFIDENCE_THRESHOLD,
    MEDIUM_CONFIDENCE_THRESHOLD,
    MatchConfidence,
    VoiceCatalogMatcher,
    build_voice_vocabulary,
)


def _catalog() -> Catalog:
    return Catalog.model_validate(
        {
            "version": 1,
            "categories": [
                {
                    "id": "cocktails",
                    "name": "Коктейли",
                    "subcategories": [
                        {
                            "id": "classic",
                            "name": "Классические",
                            "items": [
                                {
                                    "id": "old-fashioned",
                                    "name": "Old Fashioned",
                                    "voice_aliases": ["олд фешн"],
                                    "tags": ["виски"],
                                },
                                {
                                    "id": "kiwi-punch",
                                    "name": "Kiwi Punch",
                                    "tags": ["тропический"],
                                },
                                {
                                    "id": "pf-strawberry",
                                    "name": "ПФ Клубника",
                                    "voice_aliases": ["пэ эф клубника"],
                                },
                                {
                                    "id": "hidden",
                                    "name": "Скрытый коктейль",
                                    "voice_aliases": ["секретный голос"],
                                    "is_active": False,
                                },
                            ],
                        }
                    ],
                }
            ],
        }
    )


def test_catalog_item_voice_aliases_are_optional() -> None:
    catalog = Catalog.model_validate(
        {
            "version": 1,
            "categories": [
                {
                    "id": "coffee",
                    "name": "Кофе",
                    "items": [{"id": "espresso", "name": "Эспрессо"}],
                }
            ],
        }
    )

    assert catalog.categories[0].items[0].voice_aliases == []


def test_exact_manual_alias_is_high_confidence() -> None:
    outcome = VoiceCatalogMatcher(_catalog()).match("олд фешн")

    assert outcome.confidence is MatchConfidence.HIGH
    assert outcome.matches[0].result.item.id == "old-fashioned"


def test_runtime_phonetic_aliases_cover_mixed_english_names() -> None:
    outcome = VoiceCatalogMatcher(_catalog()).match("киви панч")

    assert outcome.confidence is MatchConfidence.HIGH
    assert outcome.matches[0].result.item.id == "kiwi-punch"


def test_dictated_initial_alias_is_normalized() -> None:
    outcome = VoiceCatalogMatcher(_catalog()).match("пэ эф клубника")

    assert outcome.confidence is MatchConfidence.HIGH
    assert outcome.matches[0].result.item.id == "pf-strawberry"


def test_partial_phrase_is_medium_confidence() -> None:
    outcome = VoiceCatalogMatcher(_catalog()).match("олд")

    assert outcome.confidence is MatchConfidence.MEDIUM
    assert outcome.matches[0].result.item.id == "old-fashioned"
    assert MEDIUM_CONFIDENCE_THRESHOLD <= outcome.matches[0].score
    assert outcome.matches[0].score < HIGH_CONFIDENCE_THRESHOLD


def test_unrelated_phrase_is_low_confidence() -> None:
    outcome = VoiceCatalogMatcher(_catalog()).match("абракадабра")

    assert outcome.confidence is MatchConfidence.LOW
    assert outcome.matches == ()


def test_inactive_items_are_never_matched_or_added_to_grammar() -> None:
    catalog = _catalog()

    assert VoiceCatalogMatcher(catalog).match("секретный голос").matches == ()
    assert "секретный голос" not in build_voice_vocabulary(catalog)


def test_vocabulary_contains_commands_names_and_aliases() -> None:
    vocabulary = build_voice_vocabulary(_catalog())

    assert "найди мне" in vocabulary
    assert "old fashioned" in vocabulary
    assert "олд фешн" in vocabulary
    assert "олд фэшн" in vocabulary
    assert "[unk]" in vocabulary


def test_high_threshold_boundary_is_inclusive(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.voice_matcher._token_score",
        lambda query, candidate: (
            HIGH_CONFIDENCE_THRESHOLD if "олд" in candidate else 0.0
        ),
    )

    outcome = VoiceCatalogMatcher(_catalog()).match("граница")

    assert outcome.confidence is MatchConfidence.HIGH


def test_medium_threshold_boundary_is_inclusive(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.voice_matcher._token_score",
        lambda query, candidate: (
            MEDIUM_CONFIDENCE_THRESHOLD if "олд" in candidate else 0.0
        ),
    )

    outcome = VoiceCatalogMatcher(_catalog()).match("граница")

    assert outcome.confidence is MatchConfidence.MEDIUM
    assert outcome.matches
