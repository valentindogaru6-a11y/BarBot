"""Tests for deterministic cleanup of spoken search requests."""

import pytest

from app.services.voice_query_service import (
    normalize_voice_query,
    normalize_voice_text,
)


@pytest.mark.parametrize(
    ("transcript", "expected"),
    [
        ("Найди мне Old Fashioned, пожалуйста", "old fashioned"),
        ("Покажи тех карту киви панч", "киви панч"),
        ("Открой технологическую карту Негрони", "негрони"),
        ("Пожалуйста, покажи карточку Апероль Спритц", "апероль спритц"),
        ("Где рецепт ПФ клубника", "пф клубника"),
        ("Хочу посмотреть Kiss Me", "kiss me"),
    ],
)
def test_normalizes_command_phrases(transcript: str, expected: str) -> None:
    assert normalize_voice_query(transcript) == expected


def test_normalizes_dictated_initials_and_spacing() -> None:
    assert normalize_voice_text("  ПЭ   ЭФ — клубника! ") == "пф клубника"


def test_does_not_remove_meaningful_internal_words() -> None:
    assert normalize_voice_query("коктейль пожалуйста не надо") == (
        "коктейль пожалуйста не надо"
    )
