"""Deterministic normalization and domain parsing for everyday bar language."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal, Sequence

from app.models.assistant import (
    AssistantDomain,
    AssistantRelation,
    AssistantRequestedKind,
)


BarAction = Literal["search", "writeoff", "transfer", "read", "unknown"]


def normalize_bar_text(value: str) -> str:
    """Normalize user language without retaining formatting or markup."""

    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = normalized.replace("ё", "е").replace("і", "и")
    normalized = re.sub(r"[^\w\s/]+", " ", normalized, flags=re.UNICODE)
    return " ".join(normalized.replace("_", " ").split())


@dataclass(frozen=True, slots=True)
class BarLexicon:
    """Small, explicit vocabulary that can grow without handler regex sprawl."""

    preparation_patterns: tuple[re.Pattern[str], ...] = (
        re.compile(r"\bп\s*/\s*ф\b"),
        re.compile(r"\bпф(?:к(?:а|у|и|ой)?|у|ы|ом|а)?\b"),
        re.compile(r"\bполуфабрикат\w*\b"),
        re.compile(r"\bзаготовк\w*\b"),
    )
    tech_card_patterns: tuple[re.Pattern[str], ...] = (
        re.compile(r"\bтех\s*карт\w*\b"),
        re.compile(r"\bтехнологическ\w*\s+карт\w*\b"),
        re.compile(r"\bтехничк\w*\b"),
        re.compile(r"\bрецепт\w*\b"),
    )
    ingredients_patterns: tuple[re.Pattern[str], ...] = (
        re.compile(r"\b(?:покажи\s+)?что\s+входит\s+в\b"),
        re.compile(r"\bчто\s+в\s+составе\b"),
        re.compile(r"\b(?:покажи\s+)?состав\b"),
        re.compile(r"\bиз\s+чего\s+(?:делается|сделан\w*)?\b"),
    )
    search_patterns: tuple[re.Pattern[str], ...] = (
        re.compile(r"\bнайд\w*\b"),
        re.compile(r"\bпокаж\w*\b"),
        re.compile(r"\bгде\b"),
        re.compile(r"\bиз\s+чего\b"),
        re.compile(r"\bкак\s+(?:делать|готовить|готовится)\b"),
    )

    @staticmethod
    def _matches(patterns: Sequence[re.Pattern[str]], text: str) -> bool:
        return any(pattern.search(text) is not None for pattern in patterns)

    def has_preparation(self, text: str) -> bool:
        return self._matches(self.preparation_patterns, text)

    def has_tech_card(self, text: str) -> bool:
        return self._matches(self.tech_card_patterns, text)

    def has_search(self, text: str) -> bool:
        return self._matches(self.search_patterns, text)

    def has_ingredients_request(self, text: str) -> bool:
        return self._matches(self.ingredients_patterns, text)

    def strip_catalog_language(self, text: str) -> str:
        cleaned = text
        for pattern in (
            *self.ingredients_patterns,
            *self.search_patterns,
            *self.tech_card_patterns,
            *self.preparation_patterns,
        ):
            cleaned = pattern.sub(" ", cleaned)
        cleaned = re.sub(
            r"\b(?:мне|нужн\w*|пожалуйста|карточк\w*|позици\w*)\b",
            " ",
            cleaned,
        )
        cleaned = re.sub(r"^\s*(?:для|на|по|в)\s+", " ", cleaned)
        return " ".join(cleaned.split())


BAR_LEXICON = BarLexicon()

_CONTEXT_REFERENCE_RE = re.compile(
    r"\b(?:он|она|оно|его|ее|него|нее|нем|ней|"
    r"этот|эта|это|эту|данная\s+позиция|"
    r"эта\s+карточка|эта\s+заготовка|этот\s+рецепт)\b"
)
_SIMILAR_REFERENCE_RE = re.compile(
    r"\b(?:покажи|найди)?\s*(?:что\s*то|чтонибудь)?\s*"
    r"похож\w*\b|\bа\s+есть\s+похож\w*\b|"
    r"\bеще\s+вариант\w*\b|\bпокажи\s+друг\w*\b"
)
_REFERENCE_CONTENT_RE = re.compile(
    r"\b(?:что\s+входит|состав|ингредиент\w*|как\s+(?:делать|готовить))\b"
)


def has_context_reference(value: str) -> bool:
    """Return whether text refers to prior structured catalog context."""

    text = normalize_bar_text(value)
    return bool(_CONTEXT_REFERENCE_RE.search(text) or _SIMILAR_REFERENCE_RE.search(text))


def context_reference_relation(value: str) -> AssistantRelation | None:
    """Classify a follow-up relation without inventing a catalog entity."""

    text = normalize_bar_text(value)
    if _SIMILAR_REFERENCE_RE.search(text):
        return AssistantRelation.SIMILAR_TO
    if _CONTEXT_REFERENCE_RE.search(text) and _REFERENCE_CONTENT_RE.search(text):
        return AssistantRelation.INGREDIENTS_OF
    if _CONTEXT_REFERENCE_RE.search(text):
        return AssistantRelation.DIRECT_LOOKUP
    return None


@dataclass(frozen=True, slots=True)
class ParsedBarRequest:
    """Safe structured evidence produced before any provider is considered."""

    action: BarAction
    domain: AssistantDomain
    entity: str = ""
    relation: AssistantRelation = AssistantRelation.UNSPECIFIED
    requested_kind: AssistantRequestedKind = AssistantRequestedKind.UNSPECIFIED
    relation_target: str = ""
    quantity: Decimal | None = None
    unit: str = ""
    destination: str = ""
    comment: str = ""
    period: str = ""
    person: str = ""
    confidence: int = 0
    evidence: tuple[str, ...] = ()
    mutation: bool = False


_WRITEOFF_RE = re.compile(
    r"^(?:спиши(?:те)?|списать|нужно\s+списать|"
    r"(?:убери(?:те)?|добавь(?:те)?)\s+в\s+списание|"
    r"(?:сделай|оформи)\s+списани\w*|списание\s*:?)",
)
_TRANSFER_RE = re.compile(
    r"^(?:перемести(?:те)?|переместить|перенеси(?:те)?|перенести|"
    r"отправь(?:те)?|(?:сделай|оформи)\s+перемещени\w*|"
    r"перемещение(?:\s*:)? )",
    re.VERBOSE,
)
_CATALOG_HINT_RE = re.compile(
    r"\b(?:ингредиент\w*|состав\w*|напит\w*|коктейл\w*|чай\w*|сироп\w*)\b"
)
_ATTENDANCE_RE = re.compile(r"\b(?:табел\w*|персональн\w*\s+смен\w*|attendance)\b")
_EMPLOYEE_RE = re.compile(r"\b(?:бармен\w*|сотрудник\w*|пользовател\w*|роль\w*)\b")
_REPORT_RE = re.compile(r"\b(?:отчет\w*|xlsx|excel|получател\w*|расписан\w*|напоминан\w*)\b")
_WAREHOUSE_RE = re.compile(r"\b(?:склад\w*|списани\w*|перемещени\w*|остат\w*)\b")
_QUANTITY_RE = re.compile(r"\b(?P<quantity>\d+(?:[.,]\d+)?)\b")
_UNIT_RE = re.compile(
    r"\b(?P<unit>кг|килограмм\w*|г|гр|грамм\w*|л|литр\w*|мл|миллилитр\w*|шт|штук\w*)\b"
)
_DESTINATION_RE = re.compile(
    r"(?:->|→|\b(?:в|на)\b)\s*(?P<destination>[\w][\w\s-]{0,199})\s*$",
    re.IGNORECASE,
)


def _mutation_details(
    value: str,
    *,
    transfer: bool,
) -> tuple[str, Decimal | None, str, str, str]:
    raw_text = unicodedata.normalize("NFKC", value).casefold().strip()
    comment = ""
    comment_match = re.search(r"\(([^()]*)\)\s*$", raw_text)
    if comment_match is not None:
        comment = " ".join(comment_match.group(1).split())[:500]
        raw_text = raw_text[: comment_match.start()].rstrip()
    elif "," in raw_text:
        possible_core, possible_comment = raw_text.rsplit(",", 1)
        if _QUANTITY_RE.search(possible_core) and _UNIT_RE.search(
            normalize_bar_text(possible_core)
        ):
            comment = " ".join(possible_comment.split())[:500]
            raw_text = possible_core.rstrip()

    normalized = normalize_bar_text(raw_text)
    numeric_text = raw_text.replace(",", ".")
    quantity_match = _QUANTITY_RE.search(numeric_text)
    unit_match = _UNIT_RE.search(normalized)
    destination_match = _DESTINATION_RE.search(raw_text.replace("ё", "е")) if transfer else None
    entity = _WRITEOFF_RE.sub(" ", normalized)
    entity = _TRANSFER_RE.sub(" ", entity)
    entity = re.sub(r"\b\d+\s+\d+\b", " ", entity)
    entity = _QUANTITY_RE.sub(" ", entity)
    entity = _UNIT_RE.sub(" ", entity)
    if destination_match is not None:
        normalized_destination = normalize_bar_text(destination_match.group("destination"))
        entity = re.sub(
            rf"\b(?:в|на)\s+{re.escape(normalized_destination)}\s*$",
            " ",
            entity,
        )
        for word in normalized_destination.split():
            entity = re.sub(rf"\b{re.escape(word)}\b", " ", entity)
    quantity: Decimal | None = None
    if quantity_match is not None:
        try:
            quantity = Decimal(quantity_match.group("quantity").replace(",", "."))
        except InvalidOperation:
            quantity = None
    elif unit_match is not None and unit_match.group("unit") in {
        "килограмм",
        "литр",
        "штука",
    }:
        quantity = Decimal("1")
    destination = (
        normalize_bar_text(destination_match.group("destination"))
        if destination_match
        else ""
    )
    if destination.endswith("ю"):
        destination = destination[:-1] + "я"
    elif destination.endswith("у"):
        destination = destination[:-1] + "а"
    return (
        " ".join(entity.split()),
        quantity,
        unit_match.group("unit") if unit_match else "",
        destination,
        comment,
    )


def _canonical_catalog_entity(value: str) -> str:
    def canonical_token(token: str) -> str:
        if token.endswith("ого") and len(token) > 5:
            return token[:-3] + "ый"
        if token.endswith("его") and len(token) > 5:
            return token[:-3] + "ий"
        if token.endswith("ая") and len(token) <= 5:
            return token[:-1] + "й"
        if token.endswith("аю") and len(token) <= 5:
            return token[:-2] + "й"
        return token

    return " ".join(canonical_token(token) for token in value.split())


def parse_bar_request(value: str, *, lexicon: BarLexicon = BAR_LEXICON) -> ParsedBarRequest:
    """Classify a request locally; provider output never supplies these facts."""

    text = normalize_bar_text(value)
    if not text:
        return ParsedBarRequest(action="unknown", domain=AssistantDomain.GENERAL)

    if _WRITEOFF_RE.search(text):
        entity, quantity, unit, destination, comment = _mutation_details(
            value, transfer=False
        )
        return ParsedBarRequest(
            action="writeoff", domain=AssistantDomain.WAREHOUSE, entity=entity,
            quantity=quantity, unit=unit, destination=destination,
            comment=comment,
            confidence=10_000,
            evidence=("explicit_writeoff",), mutation=True,
        )
    if _TRANSFER_RE.search(text):
        entity, quantity, unit, destination, comment = _mutation_details(
            value, transfer=True
        )
        return ParsedBarRequest(
            action="transfer", domain=AssistantDomain.WAREHOUSE, entity=entity,
            quantity=quantity, unit=unit, destination=destination,
            comment=comment,
            confidence=10_000,
            evidence=("explicit_transfer",), mutation=True,
        )

    preparation = lexicon.has_preparation(text)
    tech_card = lexicon.has_tech_card(text)
    ingredients_request = lexicon.has_ingredients_request(text)
    search = lexicon.has_search(text)
    catalog_hint = bool(_CATALOG_HINT_RE.search(text))
    preparation_lookup = preparation and bool(
        search
        or re.search(r"\b(?:для|на)\b", text)
        or re.match(r"^нужн\w*\b", text)
    )
    if ingredients_request or preparation_lookup or tech_card or (search and catalog_hint):
        if ingredients_request:
            relation = AssistantRelation.INGREDIENTS_OF
            requested_kind = AssistantRequestedKind.INGREDIENT
        elif preparation_lookup and re.search(r"\b(?:для|на)\b", text):
            relation = AssistantRelation.PREPARATION_FOR
            requested_kind = AssistantRequestedKind.PREPARATION
        else:
            relation = AssistantRelation.DIRECT_LOOKUP
            requested_kind = (
                AssistantRequestedKind.PREPARATION
                if preparation_lookup
                else AssistantRequestedKind.TECH_CARD
            )
        entity = _canonical_catalog_entity(lexicon.strip_catalog_language(text))
        evidence = tuple(
            marker
            for marker, present in (
                ("search_verb", search),
                ("preparation_term", preparation),
                ("tech_card_term", tech_card),
                ("ingredients_relation", ingredients_request),
                ("catalog_term", catalog_hint),
                (
                    "preparation_relation",
                    relation is AssistantRelation.PREPARATION_FOR,
                ),
            )
            if present
        )
        return ParsedBarRequest(
            action="search",
            domain=AssistantDomain.CATALOG,
            entity=entity,
            relation=relation,
            requested_kind=requested_kind,
            relation_target=entity,
            confidence=10_000 if search or tech_card else 8_500,
            evidence=evidence,
        )

    if _ATTENDANCE_RE.search(text):
        return ParsedBarRequest("read", AssistantDomain.ATTENDANCE, confidence=8_000, evidence=("attendance_term",))
    if _EMPLOYEE_RE.search(text):
        return ParsedBarRequest("read", AssistantDomain.EMPLOYEES, confidence=8_000, evidence=("employee_term",))
    if _REPORT_RE.search(text):
        return ParsedBarRequest("read", AssistantDomain.REPORTS, confidence=8_000, evidence=("report_term",))
    if _WAREHOUSE_RE.search(text):
        return ParsedBarRequest("read", AssistantDomain.WAREHOUSE, confidence=7_000, evidence=("warehouse_term",))
    return ParsedBarRequest(action="unknown", domain=AssistantDomain.GENERAL)


_DOMAIN_PREFIXES: dict[AssistantDomain, tuple[str, ...]] = {
    AssistantDomain.CATALOG: ("catalog.",),
    AssistantDomain.WAREHOUSE: ("warehouse.",),
    AssistantDomain.ATTENDANCE: ("attendance.", "attendance_reports."),
    AssistantDomain.EMPLOYEES: ("users.",),
    AssistantDomain.REPORTS: ("reports.", "recipients.", "schedules.", "reminders."),
    AssistantDomain.ADMINISTRATION: ("catalog.manage", "users.", "recipients.", "schedules.", "reminders."),
    AssistantDomain.GENERAL: ("capabilities.list", "navigation.main"),
    AssistantDomain.UNSPECIFIED: ("capabilities.list", "navigation.main"),
}


def capability_ids_for_domain(
    capability_ids: Sequence[str],
    domain: AssistantDomain,
) -> tuple[str, ...]:
    """Return the smallest runtime-authorized provider surface for a domain."""

    prefixes = _DOMAIN_PREFIXES[domain]
    return tuple(
        identifier
        for identifier in capability_ids
        if any(
            identifier == prefix or identifier.startswith(prefix)
            for prefix in prefixes
        )
    )
