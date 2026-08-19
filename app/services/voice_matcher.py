"""Fuzzy matching between normalized speech and active catalog items."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import Enum
from typing import Iterable, Sequence

from app.models.catalog import Catalog
from app.models.catalog_item import CatalogItem
from app.services.catalog_service import CatalogService
from app.services.search_service import SearchResult, SearchService
from app.services.voice_alias_service import VoiceAliasService
from app.services.voice_query_service import normalize_voice_text

HIGH_CONFIDENCE_THRESHOLD = 0.88
MEDIUM_CONFIDENCE_THRESHOLD = 0.58
HIGH_CONFIDENCE_MARGIN = 0.08
MAX_MEDIUM_RESULTS = 5

COMMAND_VOCABULARY = (
    "найди",
    "найди мне",
    "покажи",
    "покажи мне",
    "открой",
    "открой мне",
    "мне нужно",
    "хочу посмотреть",
    "где",
    "рецепт",
    "карточку",
    "технологическую карту",
    "тех карту",
    "пожалуйста",
)

class MatchConfidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True, slots=True)
class VoiceMatch:
    result: SearchResult
    score: float
    matched_text: str


@dataclass(frozen=True, slots=True)
class VoiceMatchOutcome:
    query: str
    confidence: MatchConfidence
    matches: tuple[VoiceMatch, ...] = ()


def _item_phrases(
    item: CatalogItem,
    alias_service: VoiceAliasService,
) -> tuple[str, ...]:
    values: list[str] = [
        *alias_service.build_aliases(item),
        *item.tags,
        item.short_description or "",
        item.description or "",
        *(ingredient.name for ingredient in item.ingredients),
        *(str(value) for value in item.fields.values()),
        *item.steps,
    ]
    normalized = {
        normalize_voice_text(value)
        for value in values
        if value and normalize_voice_text(value)
    }
    return tuple(sorted(normalized))


def _token_score(query: str, candidate: str) -> float:
    if query == candidate:
        return 1.0
    if min(len(query), len(candidate)) < 3:
        return SequenceMatcher(None, query, candidate).ratio() * 0.7

    query_tokens = query.split()
    candidate_tokens = candidate.split()
    if sorted(query_tokens) == sorted(candidate_tokens):
        return 0.99
    if query in candidate or candidate in query:
        shorter = min(len(query), len(candidate))
        longer = max(len(query), len(candidate))
        return 0.82 + (0.14 * shorter / longer)

    query_token_set = set(query_tokens)
    candidate_token_set = set(candidate_tokens)
    union = query_token_set | candidate_token_set
    overlap = (
        len(query_token_set & candidate_token_set) / len(union)
        if union
        else 0.0
    )
    sequence = SequenceMatcher(None, query, candidate).ratio()
    token_sequence = SequenceMatcher(
        None,
        " ".join(sorted(query_tokens)),
        " ".join(sorted(candidate_tokens)),
    ).ratio()
    prefix_hits = sum(
        1
        for query_token in query_tokens
        if len(query_token) >= 3
        and any(
            candidate_token.startswith(query_token)
            or query_token.startswith(candidate_token)
            for candidate_token in candidate_tokens
            if len(candidate_token) >= 3
        )
    )
    prefix_score = (
        0.72 + (0.2 * prefix_hits / len(query_tokens))
        if query_tokens and prefix_hits
        else 0.0
    )
    return max(
        sequence,
        token_sequence * 0.97,
        (0.65 * overlap) + (0.35 * sequence),
        prefix_score,
    )


def _iter_active_results(catalog: Catalog) -> Iterable[SearchResult]:
    service = CatalogService(catalog)
    for category in service.get_active_categories():
        for item in service.get_active_items(category.id):
            yield SearchResult(category=category, item=item)
        for subcategory in service.get_active_subcategories(category.id):
            for item in service.get_active_items(category.id, subcategory.id):
                yield SearchResult(
                    category=category,
                    subcategory=subcategory,
                    item=item,
                )


class VoiceCatalogMatcher:
    """Rank active catalog items while preserving their navigation context."""

    def __init__(
        self,
        catalog: Catalog,
        alias_service: VoiceAliasService | None = None,
    ) -> None:
        self.catalog = catalog
        self._search_service = SearchService(catalog)
        self._alias_service = alias_service or VoiceAliasService()

    def rank(
        self,
        query: str,
        *,
        limit: int | None = None,
    ) -> tuple[VoiceMatch, ...]:
        """Return deterministic best-alias scores for diagnostic use."""

        normalized_query = normalize_voice_text(query)
        if not normalized_query:
            return ()

        ordinary_matches = {
            (
                result.category.id,
                result.subcategory.id if result.subcategory else "_",
                result.item.id,
            )
            for result in self._search_service.search(normalized_query)
        }
        ranked: list[VoiceMatch] = []
        for result in _iter_active_results(self.catalog):
            key = (
                result.category.id,
                result.subcategory.id if result.subcategory else "_",
                result.item.id,
            )
            phrases = _item_phrases(result.item, self._alias_service)
            if not phrases:
                continue
            scored = [
                (_token_score(normalized_query, phrase), phrase)
                for phrase in phrases
            ]
            score, matched_text = max(scored, key=lambda pair: pair[0])
            if key in ordinary_matches:
                score = max(score, 0.76)
            ranked.append(VoiceMatch(result, min(score, 1.0), matched_text))

        ranked.sort(
            key=lambda match: (
                -match.score,
                match.result.item.sort_order,
                match.result.item.name.casefold(),
            )
        )
        return tuple(ranked[:limit] if limit is not None else ranked)

    def match(self, query: str) -> VoiceMatchOutcome:
        normalized_query = normalize_voice_text(query)
        if not normalized_query:
            return VoiceMatchOutcome("", MatchConfidence.LOW)

        ranked = self.rank(normalized_query)
        if not ranked or ranked[0].score < MEDIUM_CONFIDENCE_THRESHOLD:
            return VoiceMatchOutcome(normalized_query, MatchConfidence.LOW)

        top = ranked[0]
        runner_up_score = ranked[1].score if len(ranked) > 1 else 0.0
        if (
            top.score >= HIGH_CONFIDENCE_THRESHOLD
            and top.score - runner_up_score >= HIGH_CONFIDENCE_MARGIN
        ):
            return VoiceMatchOutcome(
                normalized_query,
                MatchConfidence.HIGH,
                (top,),
            )

        minimum_score = max(
            MEDIUM_CONFIDENCE_THRESHOLD,
            top.score - 0.18,
        )
        medium_matches = tuple(
            match
            for match in ranked
            if match.score >= minimum_score
        )[:MAX_MEDIUM_RESULTS]
        return VoiceMatchOutcome(
            normalized_query,
            MatchConfidence.MEDIUM,
            medium_matches,
        )


def build_voice_vocabulary(catalog: Catalog) -> list[str]:
    """Build a stable Vosk grammar from active names, aliases, and commands."""

    phrases = {normalize_voice_text(value) for value in COMMAND_VOCABULARY}
    alias_service = VoiceAliasService()
    for result in _iter_active_results(catalog):
        phrases.update(alias_service.build_aliases(result.item))
    phrases.discard("")
    phrases.add("[unk]")
    return sorted(phrases)
