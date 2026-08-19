"""Deterministic cleanup of spoken catalog-search phrases."""

from __future__ import annotations

import re

_PUNCTUATION_RE = re.compile(r"[^\w\s-]+", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")

LEADING_POLITE_WORDS = ("пожалуйста", "плиз")
COMMAND_PREFIXES = (
    "хочу посмотреть",
    "покажи мне",
    "открой мне",
    "найди мне",
    "мне нужно",
    "покажи",
    "открой",
    "найди",
    "где",
)
OBJECT_PREFIXES = (
    "технологическую карту",
    "технологическая карта",
    "тех карту",
    "техкарту",
    "карточку",
    "карточка",
    "рецепт",
)
TRAILING_POLITE_WORDS = ("пожалуйста", "спасибо")


def normalize_voice_text(value: str) -> str:
    """Normalize punctuation, case, spacing, and common dictated initials."""

    normalized = value.casefold().replace("ё", "е")
    normalized = _PUNCTUATION_RE.sub(" ", normalized)
    normalized = _SPACE_RE.sub(" ", normalized).strip()
    replacements = {
        "пэ эф": "пф",
        "пэ-эф": "пф",
        "би пятьдесят два": "б 52",
        "бэ пятьдесят два": "б 52",
    }
    for source, target in replacements.items():
        normalized = normalized.replace(source, target)
    return _SPACE_RE.sub(" ", normalized).strip()


def _strip_prefix(value: str, prefixes: tuple[str, ...]) -> tuple[str, bool]:
    for prefix in prefixes:
        if value == prefix:
            return "", True
        marker = f"{prefix} "
        if value.startswith(marker):
            return value[len(marker) :].strip(), True
    return value, False


def normalize_voice_query(transcript: str) -> str:
    """Remove only well-known command framing and keep the catalog phrase."""

    value = normalize_voice_text(transcript)
    for _ in range(3):
        value, removed_polite = _strip_prefix(value, LEADING_POLITE_WORDS)
        value, removed_command = _strip_prefix(value, COMMAND_PREFIXES)
        if not (removed_polite or removed_command):
            break
    value, _ = _strip_prefix(value, OBJECT_PREFIXES)
    for suffix in TRAILING_POLITE_WORDS:
        if value == suffix:
            return ""
        marker = f" {suffix}"
        if value.endswith(marker):
            value = value[: -len(marker)].strip()
            break
    return normalize_voice_text(value)
