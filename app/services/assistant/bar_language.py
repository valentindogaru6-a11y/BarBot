"""Deterministic normalization and domain parsing for everyday bar language."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Sequence

from app.models.assistant import (
    AssistantDomain,
    AssistantRelation,
    AssistantRequestedKind,
)
from app.models.assistant_mutation import ParsedMutation
from app.services.assistant.mutation_language import parse_mutation


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
    mutation_request: ParsedMutation | None = None

    @property
    def disposition(self):
        return self.mutation_request.disposition if self.mutation_request else None

    @property
    def validation_error(self):
        return self.mutation_request.error if self.mutation_request else None

    @property
    def imperative_evidence(self):
        return bool(self.mutation_request and self.mutation_request.imperative_evidence)

    @property
    def explicit_cancellation(self):
        return bool(self.mutation_request and self.mutation_request.explicit_cancellation)

    @property
    def correction_segment(self):
        return self.mutation_request.correction_segment if self.mutation_request else None


_CATALOG_HINT_RE = re.compile(
    r"\b(?:ингредиент\w*|состав\w*|напит\w*|коктейл\w*|чай\w*|сироп\w*)\b"
)
_ATTENDANCE_RE = re.compile(r"\b(?:табел\w*|персональн\w*\s+смен\w*|attendance)\b")
_EMPLOYEE_RE = re.compile(r"\b(?:бармен\w*|сотрудник\w*|пользовател\w*|роль\w*)\b")
_REPORT_RE = re.compile(r"\b(?:отчет\w*|xlsx|excel|получател\w*|расписан\w*|напоминан\w*)\b")
_WAREHOUSE_RE = re.compile(r"\b(?:склад\w*|списани\w*|перемещени\w*|остат\w*)\b")


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

    if mutation := parse_mutation(value):
        line = mutation.lines[0] if mutation.lines else None
        return ParsedBarRequest(
            action=mutation.action or 'unknown', domain=AssistantDomain.WAREHOUSE,
            entity=line.entity if line else '', quantity=line.quantity if line else None,
            unit=line.unit if line else '', destination=line.destination if line else '',
            comment=line.comment if line else '', mutation_request=mutation,
            confidence=10_000,
            evidence=(f'explicit_{mutation.action}',) if mutation.imperative_evidence else (),
            mutation=mutation.allowed,
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
