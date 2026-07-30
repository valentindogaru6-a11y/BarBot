"""Tests for text normalization."""

from app.utils.text import normalize_search_text


def test_normalize_search_text() -> None:
    assert normalize_search_text("  МАРАКУЙЯ   Сауэр \n") == "маракуйя сауэр"
