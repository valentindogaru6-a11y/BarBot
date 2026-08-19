"""Lightweight runtime aliases for catalog names pronounced in Russian."""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from itertools import product

from app.models.catalog_item import CatalogItem
from app.services.voice_query_service import normalize_voice_text

MAX_AUTOMATIC_ALIASES = 16
_ASCII_LATIN_RE = re.compile(r"[a-z]+", re.IGNORECASE)

# These are vocabulary exceptions, never catalog IDs. Values are normalized
# later, so spelling variants that differ only by "ё" are safely deduplicated.
WORD_EXCEPTIONS: dict[str, tuple[str, ...]] = {
    "allure": ("аллюр", "алюр"),
    "aperol": ("апероль",),
    "black": ("блэк", "блек"),
    "boulevardier": ("булевардиер", "булевардье"),
    "boy": ("бой",),
    "butterfly": ("баттерфляй", "батерфляй"),
    "cabernet": ("каберне",),
    "chardonnay": ("шардоне",),
    "coffee": ("кофе",),
    "fashioned": ("фэшн", "фешн", "фэшнд", "фешенд"),
    "flat": ("флэт", "флат"),
    "girl": ("герл", "гёрл"),
    "gin": ("джин",),
    "grigio": ("гриджо", "гриджио"),
    "hive": ("хайв",),
    "kiwi": ("киви",),
    "margarita": ("маргарита",),
    "negroni": ("негрони",),
    "old": ("олд",),
    "pinot": ("пино",),
    "pornstar": ("порнстар", "порн стар"),
    "punch": ("панч", "пунч", "панц"),
    "red": ("ред",),
    "rose": ("розе", "роуз"),
    "sauvignon": ("совиньон", "совиньен"),
    "smash": ("смэш", "смеш"),
    "sour": ("сауэр", "сауер"),
    "spritz": ("шприц", "спритц", "сприц"),
    "strawberry": ("строберри", "строуберри"),
    "whiskey": ("виски",),
    "white": ("уайт", "вайт"),
    "wine": ("вайн",),
}

PHRASE_EXCEPTIONS: dict[str, tuple[str, ...]] = {
    "aperol spritz": (
        "апероль шприц",
        "апероль спритц",
        "апероль сприц",
    ),
    "gin sour": ("джин сауэр", "джин сауер"),
    "old fashioned": (
        "олд фэшн",
        "олд фешн",
        "олд фэшнд",
        "олд фешенд",
    ),
    "whiskey sour": ("виски сауэр", "виски сауер"),
}

_MULTI_RULES: tuple[tuple[str, tuple[str, str]], ...] = (
    ("sh", ("ш", "ш")),
    ("ch", ("ч", "ч")),
    ("th", ("т", "с")),
    ("ph", ("ф", "ф")),
    ("oo", ("у", "у")),
    ("ee", ("и", "и")),
    ("ea", ("и", "э")),
    ("ai", ("эй", "ай")),
    ("ay", ("эй", "эй")),
    ("ou", ("ау", "у")),
    ("ow", ("ау", "оу")),
    ("ck", ("к", "к")),
    ("qu", ("кв", "кв")),
    ("wh", ("в", "у")),
)

_LETTER_MAP = {
    "a": "а",
    "b": "б",
    "d": "д",
    "e": "е",
    "f": "ф",
    "h": "х",
    "i": "и",
    "j": "дж",
    "k": "к",
    "l": "л",
    "m": "м",
    "n": "н",
    "o": "о",
    "p": "п",
    "q": "к",
    "r": "р",
    "s": "с",
    "t": "т",
    "u": "у",
    "v": "в",
    "w": "в",
    "x": "кс",
    "y": "й",
    "z": "з",
}


def contains_latin(value: str) -> bool:
    """Return whether text contains any Unicode Latin letter."""

    return any(
        char.isalpha() and "LATIN" in unicodedata.name(char, "")
        for char in value
    )


def contains_cyrillic(value: str) -> bool:
    """Return whether text contains any Unicode Cyrillic letter."""

    return any(
        char.isalpha() and "CYRILLIC" in unicodedata.name(char, "")
        for char in value
    )


def _fold_latin_diacritics(value: str) -> str:
    result: list[str] = []
    for char in value:
        if "LATIN" not in unicodedata.name(char, ""):
            result.append(char)
            continue
        decomposed = unicodedata.normalize("NFKD", char)
        replacement = next(
            (
                part
                for part in decomposed
                if part.isascii() and part.isalpha()
            ),
            char,
        )
        result.append(replacement)
    return "".join(result)


def _convert_word(word: str, variant: int) -> str:
    output: list[str] = []
    position = 0
    while position < len(word):
        matched = False
        for source, replacements in _MULTI_RULES:
            if word.startswith(source, position):
                output.append(replacements[variant])
                position += len(source)
                matched = True
                break
        if matched:
            continue
        char = word[position]
        following = word[position + 1] if position + 1 < len(word) else ""
        if char == "c":
            output.append("с" if following in "eiy" else "к")
        elif char == "g":
            output.append("дж" if variant and following in "eiy" else "г")
        elif char == "y":
            output.append("и" if variant else "й")
        else:
            output.append(_LETTER_MAP.get(char, char))
        position += 1
    return "".join(output)


def _word_options(word: str) -> tuple[str, ...]:
    exceptions = WORD_EXCEPTIONS.get(word)
    if exceptions is not None:
        return tuple(
            dict.fromkeys(normalize_voice_text(value) for value in exceptions)
        )
    return tuple(
        dict.fromkeys(
            normalize_voice_text(_convert_word(word, variant))
            for variant in (0, 1)
        )
    )


def _token_options(token: str) -> tuple[str, ...]:
    latin_parts = list(_ASCII_LATIN_RE.finditer(token))
    if not latin_parts:
        return (token,)
    variants = [token]
    for match in reversed(latin_parts):
        expanded: list[str] = []
        for current in variants:
            for replacement in _word_options(match.group(0)):
                expanded.append(
                    current[: match.start()]
                    + replacement
                    + current[match.end() :]
                )
        variants = list(dict.fromkeys(expanded))[:MAX_AUTOMATIC_ALIASES]
    return tuple(variants)


@lru_cache(maxsize=4096)
def _automatic_aliases(name: str) -> frozenset[str]:
    normalized = normalize_voice_text(_fold_latin_diacritics(name))
    if not contains_latin(normalized):
        return frozenset()
    token_variants = [_token_options(token) for token in normalized.split()]
    aliases: set[str] = set()
    for combination in product(*token_variants):
        alias = normalize_voice_text(" ".join(combination))
        if alias and alias != normalized and not contains_latin(alias):
            aliases.add(alias)
        if len(aliases) >= MAX_AUTOMATIC_ALIASES:
            break
    aliases.update(
        normalize_voice_text(alias)
        for alias in PHRASE_EXCEPTIONS.get(normalized, ())
    )
    return frozenset(alias for alias in aliases if alias)


@lru_cache(maxsize=4096)
def _all_aliases(
    item_id: str,
    name: str,
    manual_aliases: tuple[str, ...],
) -> frozenset[str]:
    del item_id  # The stable ID is part of the cache key, never alias logic.
    aliases = {normalize_voice_text(name)}
    aliases.update(_automatic_aliases(name))
    aliases.update(normalize_voice_text(alias) for alias in manual_aliases)
    return frozenset(alias for alias in aliases if alias)


class VoiceAliasService:
    """Build searchable aliases without mutating a catalog item or its JSON."""

    def build_aliases(self, item: CatalogItem) -> set[str]:
        return set(_all_aliases(item.id, item.name, tuple(item.voice_aliases)))

    def build_automatic_aliases(self, name: str) -> set[str]:
        return set(_automatic_aliases(name))


def build_aliases(item: CatalogItem) -> set[str]:
    """Convenience function for one-off alias generation."""

    return VoiceAliasService().build_aliases(item)
