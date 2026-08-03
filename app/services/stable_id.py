"""Deterministic stable-ID generation for administrator-created entities."""

import re
import unicodedata
from collections.abc import Collection

_SEPARATOR_RE = re.compile(r"[^a-z0-9]+")
_CYRILLIC_DIGRAPHS = {
    "дж": "g",
    "дз": "z",
}
_CYRILLIC_TRANSLITERATION = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "ґ": "g",
    "д": "d",
    "е": "e",
    "ё": "yo",
    "є": "ye",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "і": "i",
    "ї": "yi",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "kh",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "shch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}


class StableIdGenerationError(ValueError):
    """Raised when a human-readable name cannot produce a valid ID."""


def _transliterate(value: str) -> str:
    lowered = value.casefold()
    result: list[str] = []
    index = 0
    while index < len(lowered):
        pair = lowered[index : index + 2]
        if pair in _CYRILLIC_DIGRAPHS:
            result.append(_CYRILLIC_DIGRAPHS[pair])
            index += 2
            continue

        character = lowered[index]
        transliterated = _CYRILLIC_TRANSLITERATION.get(character)
        if transliterated is not None:
            result.append(transliterated)
        else:
            decomposed = unicodedata.normalize("NFKD", character)
            result.extend(
                entry
                for entry in decomposed
                if not unicodedata.combining(entry)
            )
        index += 1
    return "".join(result)


def _truncate_at_boundary(value: str, limit: int) -> str:
    candidate = value[:limit].rstrip("_")
    if len(value) <= limit or "_" not in candidate:
        return candidate

    complete_words = candidate.rsplit("_", 1)[0].rstrip("_")
    return complete_words or candidate


def generate_stable_id(
    name: str,
    existing_ids: Collection[str],
    max_length: int,
) -> str:
    """Generate a unique lowercase ASCII ID from a display name."""

    if max_length < 1:
        raise StableIdGenerationError("Максимальная длина ID должна быть положительной")

    slug = _SEPARATOR_RE.sub("_", _transliterate(name)).strip("_")
    if not slug:
        raise StableIdGenerationError(
            "Не удалось создать технический ID. Введите другое название."
        )
    if not slug[0].isalpha():
        slug = f"id_{slug}"

    occupied = set(existing_ids)
    candidate = _truncate_at_boundary(slug, max_length)
    if candidate and candidate not in occupied:
        return candidate

    counter = 2
    while True:
        suffix = f"_{counter}"
        base_limit = max_length - len(suffix)
        if base_limit < 1:
            raise StableIdGenerationError(
                "Слишком мала максимальная длина для уникального ID."
            )
        base = _truncate_at_boundary(slug, base_limit)
        candidate = f"{base}{suffix}" if base else ""
        if candidate and candidate[0].isalpha() and candidate not in occupied:
            return candidate
        counter += 1
