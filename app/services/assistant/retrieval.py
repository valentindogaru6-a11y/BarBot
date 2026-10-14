"""Local-first ranked retrieval over active catalog cards."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable

from app.models.assistant import AssistantRelation, AssistantRequestedKind
from app.models.catalog import Catalog
from app.services.catalog_service import CatalogService
from app.services.search_service import SearchResult
from app.services.assistant.bar_language import parse_bar_request

MAX_CATALOG_CANDIDATES = 10
_COMMAND_PHRASES = re.compile(
    r"\b(?:найди(?:\s+мне)?|покажи|тех(?:нологическ\w*\s+)?карт\w*|"
    r"рецепт\w*|приготовлен\w*|как\s+приготовить|как\s+делать|как\s+готовится)\b",
    re.IGNORECASE,
)
_QUERY_FILLER_RE = re.compile(
    r"^\s*(?:что\s+у\s+нас\s+есть\s+с|что\s+есть\s+с|у\s+нас\s+с)\s+",
    re.IGNORECASE,
)
_COMMAND_TOKENS = {
    "найди", "мне", "покажи", "техкарта", "технологическая", "карта",
    "рецепт", "как", "приготовить", "приготовление", "делать", "готовится",
    "в", "техкартах",
}
_RUSSIAN_SUFFIXES = tuple(
    sorted(
        {
            "иями", "ями", "ами", "ского", "скому", "ской", "ская", "скую",
            "ские", "ский", "ого", "ему", "ому", "ыми", "ими", "ое",
            "ая", "яя", "ую", "юю", "ий", "ый", "ой", "ие", "ые", "ов", "ев",
            "ам", "ям", "ах", "ях", "ом", "ем", "у", "ю", "а", "я",
            "ы", "и", "е", "о",
        },
        key=len,
        reverse=True,
    )
)


def normalize_assistant_text(value: str) -> str:
    """Normalize Unicode, case, ``ё`` and punctuation for retrieval."""

    normalized = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    normalized = re.sub(r"[^\w]+", " ", normalized, flags=re.UNICODE)
    return " ".join(normalized.replace("_", " ").split())


def extract_catalog_entity(value: str) -> str:
    """Remove command language while retaining category/style modifiers."""

    parsed = parse_bar_request(value)
    if parsed.domain == "catalog" and parsed.entity:
        return parsed.entity

    without_phrases = _QUERY_FILLER_RE.sub("", value)
    without_phrases = _COMMAND_PHRASES.sub(" ", without_phrases)
    tokens = [
        token
        for token in normalize_assistant_text(without_phrases).split()
        if token not in _COMMAND_TOKENS
    ]
    return " ".join(tokens)


def display_catalog_entity(value: str) -> str:
    """Render a short user-facing entity without command words."""

    entity = extract_catalog_entity(value)
    replacements = {
        "безалкогольного": "безалкогольный",
        "безалкогольное": "безалкогольный",
        "алкогольного": "алкогольный",
        "алкогольное": "алкогольный",
    }
    words = [replacements.get(word, word) for word in entity.split()]
    return " ".join(words).capitalize()


def _stem(token: str) -> str:
    # Common short words such as "чая"/"края" carry a mobile й in
    # oblique cases.  Normalize the pattern generically, not per recipe.
    if token.endswith("ая") and len(token) <= 5:
        token = token[:-1] + "й"
    for suffix in _RUSSIAN_SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 4:
            return token[: -len(suffix)]
    return token


def _token_key(value: str) -> tuple[str, ...]:
    return tuple(sorted({_stem(token) for token in value.split() if token}))


def candidate_key(result: SearchResult) -> str:
    """Return a local stable key without user identifiers or raw queries."""

    subcategory_id = result.subcategory.id if result.subcategory else "_"
    return f"{result.category.id}/{subcategory_id}/{result.item.id}"


@dataclass(frozen=True, slots=True)
class RankedCatalogCandidate:
    result: SearchResult
    score: int
    reason: str

    @property
    def key(self) -> str:
        return candidate_key(self.result)

    @property
    def provider_label(self) -> str:
        path = self.result.category.name
        if self.result.subcategory is not None:
            path += f" / {self.result.subcategory.name}"
        return f"{self.result.item.name} — {path}"


@dataclass(frozen=True, slots=True)
class CatalogRetrieval:
    normalized_query: str
    exact: tuple[SearchResult, ...] = ()
    candidates: tuple[RankedCatalogCandidate, ...] = ()
    requested_kind: AssistantRequestedKind = AssistantRequestedKind.UNSPECIFIED
    relation: AssistantRelation = AssistantRelation.UNSPECIFIED
    relation_target: str | None = None
    parent_entity_ids: tuple[str, ...] = ()
    related_entity_ids: tuple[str, ...] = ()


class AssistantCatalogRetriever:
    """Resolve exact/token matches first and keep fuzzy matches explicit."""

    def __init__(self, catalog: Catalog) -> None:
        self.catalog_service = CatalogService(catalog)

    def retrieve(
        self,
        query: str,
        *,
        limit: int = 5,
        requested_kind: AssistantRequestedKind = AssistantRequestedKind.UNSPECIFIED,
        relation: AssistantRelation = AssistantRelation.UNSPECIFIED,
        relation_target: str | None = None,
        parent_entity_id: str | None = None,
        selected_entity_id: str | None = None,
    ) -> CatalogRetrieval:
        parsed_request = parse_bar_request(query)
        if requested_kind is AssistantRequestedKind.UNSPECIFIED:
            requested_kind = parsed_request.requested_kind
        if relation is AssistantRelation.UNSPECIFIED:
            relation = parsed_request.relation
        raw_target = relation_target or parsed_request.relation_target or query
        normalized_query = extract_catalog_entity(raw_target)
        metadata = {
            "requested_kind": requested_kind,
            "relation": relation,
            "relation_target": (
                normalized_query
                if relation is not AssistantRelation.UNSPECIFIED
                else None
            ),
        }
        if selected_entity_id:
            selected = self.by_key(selected_entity_id)
            return CatalogRetrieval(
                normalized_query=normalized_query,
                exact=(selected,) if selected is not None else (),
                related_entity_ids=(selected_entity_id,) if selected is not None else (),
                **metadata,
            )
        if not normalized_query:
            return CatalogRetrieval(normalized_query="", **metadata)
        if relation is AssistantRelation.PREPARATION_FOR:
            parents, related = self._related_preparations(
                normalized_query,
                parent_entity_id=parent_entity_id,
            )
            if related:
                return CatalogRetrieval(
                    normalized_query=normalized_query,
                    exact=related,
                    parent_entity_ids=tuple(candidate_key(item) for item in parents),
                    related_entity_ids=tuple(candidate_key(item) for item in related),
                    **metadata,
                )
            if parents:
                return CatalogRetrieval(
                    normalized_query=normalized_query,
                    parent_entity_ids=tuple(candidate_key(item) for item in parents),
                    **metadata,
                )
        query_tokens = _token_key(normalized_query)
        exact: list[SearchResult] = []
        ranked: list[RankedCatalogCandidate] = []
        for result in self._active_results():
            title = normalize_assistant_text(result.item.name)
            aliases = tuple(
                normalize_assistant_text(alias)
                for alias in result.item.voice_aliases
                if normalize_assistant_text(alias)
            )
            tags = tuple(
                normalize_assistant_text(tag)
                for tag in result.item.tags
                if normalize_assistant_text(tag)
            )
            title_tokens = _token_key(title)
            if (
                normalized_query == title
                or normalized_query in aliases
                or (
                    len(normalized_query.split()) >= 2
                    and any(
                        normalized_query == tag
                        or query_tokens == _token_key(tag)
                        for tag in tags
                    )
                )
            ):
                exact.append(result)
                continue
            if query_tokens and query_tokens == title_tokens:
                exact.append(result)
                continue
            score, reason = self._score(
                normalized_query,
                query_tokens,
                result,
                title,
                title_tokens,
                aliases,
            )
            if requested_kind is AssistantRequestedKind.PREPARATION:
                preparation_fields = (
                    normalize_assistant_text(result.category.name),
                    normalize_assistant_text(
                        result.subcategory.name if result.subcategory else ""
                    ),
                    *(normalize_assistant_text(tag) for tag in result.item.tags),
                )
                if any(
                    marker in field
                    for field in preparation_fields
                    for marker in ("п ф", "пф", "полуфабрикат", "заготовк")
                ):
                    score += 180
                    reason = "preparation_" + reason
            if score >= 580:
                ranked.append(RankedCatalogCandidate(result, score, reason))
        if exact:
            return CatalogRetrieval(
                normalized_query=normalized_query,
                exact=tuple(exact),
                related_entity_ids=tuple(candidate_key(item) for item in exact),
                **metadata,
            )
        ranked.sort(key=lambda value: (-value.score, value.result.item.name.casefold()))
        bounded = tuple(ranked[: max(1, min(limit, MAX_CATALOG_CANDIDATES))])
        # A complete title-token containment is deterministic lexical evidence,
        # unlike fuzzy similarity.  Promote only one unambiguous result.
        if (
            bounded
            and bounded[0].reason.endswith("title_tokens")
            and bounded[0].score >= 850
            and all(item.score < bounded[0].score for item in bounded[1:])
        ):
            result = bounded[0].result
            return CatalogRetrieval(
                normalized_query=normalized_query,
                exact=(result,),
                related_entity_ids=(candidate_key(result),),
                **metadata,
            )
        return CatalogRetrieval(
            normalized_query=normalized_query,
            candidates=bounded,
            related_entity_ids=tuple(candidate.key for candidate in bounded),
            **metadata,
        )

    def _related_preparations(
        self,
        query: str,
        *,
        parent_entity_id: str | None = None,
    ) -> tuple[tuple[SearchResult, ...], tuple[SearchResult, ...]]:
        """Resolve a requested preparation through tags or a card ingredient."""

        results = tuple(self._active_results())
        query_tokens = _token_key(query)
        direct: list[SearchResult] = []
        parents: list[SearchResult] = []
        for result in results:
            title = normalize_assistant_text(result.item.name)
            aliases = tuple(normalize_assistant_text(value) for value in result.item.voice_aliases)
            tags = tuple(normalize_assistant_text(value) for value in result.item.tags)
            container = " ".join(
                filter(None, (
                    normalize_assistant_text(result.category.name),
                    normalize_assistant_text(result.subcategory.name if result.subcategory else ""),
                ))
            )
            is_preparation = "полуфабрикат" in container or any(
                marker in tag
                for tag in tags
                for marker in ("п ф", "пф", "полуфабрикат", "заготовк")
            )
            if is_preparation and (
                query in tags
                or (
                    len(query_tokens) >= 2
                    and any(query_tokens == _token_key(tag) for tag in tags)
                )
                or query == title
                or query in aliases
                or (query_tokens and query_tokens == _token_key(title))
            ):
                direct.append(result)
            if parent_entity_id and candidate_key(result) == parent_entity_id:
                parents.append(result)
            elif not parent_entity_id and (query == title or query in aliases or (
                query_tokens and query_tokens == _token_key(title)
            ) or (
                len(query_tokens) >= 2
                and set(query_tokens).issubset(_token_key(title))
            )):
                parents.append(result)
        if direct:
            ordered = tuple(sorted(direct, key=lambda value: value.item.name.casefold()))
            return (), ordered

        related_names: set[str] = set()
        for parent in parents:
            for ingredient in parent.item.ingredients:
                name = normalize_assistant_text(ingredient.name)
                if re.search(r"\b(?:п\s*ф|пф|полуфабрикат)\b", name):
                    cleaned = re.sub(r"\b(?:п\s*ф|пф|полуфабрикат)\b", " ", name)
                    related_names.add(" ".join(cleaned.split()))
        related = [
            result
            for result in results
            if normalize_assistant_text(result.item.name) in related_names
        ]
        return (
            tuple(sorted(parents, key=lambda value: value.item.name.casefold())),
            tuple(sorted(related, key=lambda value: value.item.name.casefold())),
        )

    def by_key(self, key: str) -> SearchResult | None:
        return next(
            (result for result in self._active_results() if candidate_key(result) == key),
            None,
        )

    @staticmethod
    def key_for(result: SearchResult) -> str:
        return candidate_key(result)

    def _active_results(self) -> Iterable[SearchResult]:
        for category in self.catalog_service.get_active_categories():
            for item in self.catalog_service.get_active_items(category.id):
                yield SearchResult(category=category, item=item)
            for subcategory in self.catalog_service.get_active_subcategories(category.id):
                for item in self.catalog_service.get_active_items(
                    category.id, subcategory.id
                ):
                    yield SearchResult(
                        category=category,
                        subcategory=subcategory,
                        item=item,
                    )

    @staticmethod
    def _score(
        query: str,
        query_tokens: tuple[str, ...],
        result: SearchResult,
        title: str,
        title_tokens: tuple[str, ...],
        aliases: tuple[str, ...],
    ) -> tuple[int, str]:
        query_set = set(query_tokens)
        title_set = set(title_tokens)
        if query_set and query_set.issubset(title_set):
            return 850 + min(49, len(query_set) * 5), "title_tokens"
        searchable = [
            title,
            *aliases,
            normalize_assistant_text(result.category.name),
            normalize_assistant_text(
                result.subcategory.name if result.subcategory else ""
            ),
            normalize_assistant_text(result.item.short_description or ""),
            normalize_assistant_text(result.item.description or ""),
            *(normalize_assistant_text(tag) for tag in result.item.tags),
            *(
                normalize_assistant_text(ingredient.name)
                for ingredient in result.item.ingredients
            ),
        ]
        if any(query and query in value for value in searchable):
            return 700, "searchable_text"
        searchable_token_sets = [set(_token_key(value)) for value in searchable]
        if len(query_set) >= 2 and any(
            all(
                any(left.startswith(right) or right.startswith(left) for right in tokens)
                for left in query_set
            )
            for tokens in searchable_token_sets
            if tokens
        ):
            title_prefix_match = all(
                any(left.startswith(right) or right.startswith(left) for right in title_set)
                for left in query_set
            ) if title_set else False
            return (830 if title_prefix_match else 680), (
                "title_tokens" if title_prefix_match else "metadata_tokens"
            )
        if query_set and title_set and all(
            any(left.startswith(right) or right.startswith(left) for right in title_set)
            for left in query_set
        ):
            return 830, "title_tokens"
        token_overlap = 0.0
        if query_set and title_set:
            token_overlap = len(query_set & title_set) / len(query_set | title_set)
        similarities = [SequenceMatcher(None, query, title).ratio()]
        similarities.extend(SequenceMatcher(None, query, alias).ratio() for alias in aliases)
        similarity = max(similarities)
        score = int(max(similarity, token_overlap) * 1000)
        return score, "fuzzy"
