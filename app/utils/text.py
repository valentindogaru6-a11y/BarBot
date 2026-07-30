"""Helpers for working with user-facing text."""


def normalize_search_text(value: str) -> str:
    """Normalize text for case-insensitive substring search."""

    return " ".join(value.strip().casefold().split())
