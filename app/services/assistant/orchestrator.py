"""Local-first intent routing and a narrow allowlisted tool registry."""

from __future__ import annotations

import logging
import re
import unicodedata
import inspect
from dataclasses import dataclass
from typing import Literal, Sequence

from app.models.assistant import (
    AssistantDomain,
    AssistantIntent,
    AssistantIntentItem,
    AssistantIntentName,
    AssistantRelation,
    AssistantRequestedKind,
)
from app.models.catalog import Catalog
from app.services.assistant.providers import (
    GroundedSearchClient,
    GroundedSearchResult,
    IntentModelClient,
)
from app.services.assistant.bar_language import parse_bar_request
from app.services.assistant.retrieval import (
    AssistantCatalogRetriever,
    display_catalog_entity,
)
from app.services.search_service import SearchResult

MAX_ASSISTANT_TEXT_LENGTH = 2_000
logger = logging.getLogger(__name__)
_WAREHOUSE_ACTION_INTENTS = frozenset(
    {
        AssistantIntentName.PREPARE_WRITEOFF,
        AssistantIntentName.PREPARE_TRANSFER,
    }
)
_UNSUPPORTED_RE = re.compile(
    r"\b(?:перезапуст\w*|измени\s+прав|поменяй|редактир|"
    r"добавь\s+(?:описание|продукт)|создай\s+продукт)\b",
    re.IGNORECASE,
)
_WEB_REQUIRED_RE = re.compile(
    r"\b(?:найди\s+в\s+интернете|проверь\s+актуальн\w*|сегодня|сейчас|"
    r"последн\w*\s+новост\w*|актуальн\w*\s+(?:цен\w*|расписан\w*|график\w*))\b",
    re.IGNORECASE,
)
_EXPLICIT_WEB_RE = re.compile(
    r"\b(?:найди\s+в\s+интернете|проверь\s+актуальн\w*)\b",
    re.IGNORECASE,
)
_WAREHOUSE_ACTION_PREFIX_RE = re.compile(
    r"^\s*(?:спиши|списать|перемести|переместить)\b",
    re.IGNORECASE,
)
_TECH_PATTERNS = (
    re.compile(
        r"^\s*так\s+что\s+там\s+по\s+(?P<query>.+?)\s*[?!.]*\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\s*(?:найди(?:\s+мне)?|покажи)\s+"
        r"(?:(?:тех(?:нологическую\s+)?карт(?:у|ы)?)\s+|"
        r"как\s+(?:делать|готовится)\s+)?(?P<query>.+?)\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\s*как\s+(?:делать|готовится)\s+(?P<query>.+?)\s*$",
        re.IGNORECASE,
    ),
)
_WRITEOFF_PARTIAL_RE = re.compile(
    r"^\s*(?:спиши|списать)\s+(?P<product>.+?)\s*$", re.IGNORECASE
)
_TRANSFER_PARTIAL_RE = re.compile(
    r"^\s*(?:перемести|переместить)\s+(?P<product>.+?)\s*$", re.IGNORECASE
)
_QUANTITY_HINT_RE = re.compile(
    r"(?:\d|полкил|килограмм|грамм|\bкг\b|литр|миллилитр|\bмл\b|"
    r"\bл\b|штук)",
    re.IGNORECASE,
)
_WRITEOFF_EVIDENCE_RE = re.compile(
    r"^(?:(?:спиши(?:те)?|списать)\b|"
    r"(?:сделай|оформи)\s+списани(?:е|я)\b|"
    r"добавь\s+в\s+списани(?:е|я)\b|списание\s*:)",
)
_TRANSFER_EVIDENCE_RE = re.compile(
    r"^(?:(?:перемести(?:те)?|переместить|перенеси(?:те)?|перенести)\b|"
    r"(?:сделай|оформи)\s+перемещени(?:е|я)\b|перемещение\s*:|"
    r"отправь(?:те)?\b.+\bна\s+кухн(?:ю|е)\b)",
)
_OPEN_ATTENDANCE_RE = re.compile(
    r"\b(?:чьи|какие|открыт\w*)\b.*\b(?:смен\w*|сесси\w*)\b|"
    r"\b(?:смен\w*|сесси\w*)\b.*\b(?:открыт\w*|до сих пор)\b",
    re.IGNORECASE,
)
_CURRENT_SHIFT_RE = re.compile(
    r"\b(?:текущ\w*|сейчас)\b.*\bсмен\w*\b", re.IGNORECASE
)


class AssistantUnavailableError(RuntimeError):
    """Raised when a free-form request needs a disabled provider."""


@dataclass(frozen=True, slots=True)
class AssistantToolAction:
    kind: Literal[
        "find_tech_card",
        "prepare_writeoff",
        "prepare_transfer",
        "clarify",
        "not_found",
        "unsupported",
        "general_chat",
        "web_search",
        "list_open_attendance_sessions",
        "get_current_shift_summary",
    ]
    message: str | None = None
    raw_intake: str | None = None
    search_results: tuple[SearchResult, ...] = ()
    candidate_ids: tuple[str, ...] = ()
    pending_slots: tuple[str, ...] = ()
    entity_query: str | None = None
    catalog_exact: bool = False
    domain: AssistantDomain = AssistantDomain.UNSPECIFIED
    requested_kind: AssistantRequestedKind = AssistantRequestedKind.UNSPECIFIED
    relation: AssistantRelation = AssistantRelation.UNSPECIFIED
    relation_target: str | None = None
    parent_entity_id: str | None = None
    related_entity_ids: tuple[str, ...] = ()
    selected_entity_id: str | None = None


class AssistantOrchestrator:
    """Recognize deterministic commands before consulting Gemini."""

    def __init__(
        self,
        intent_client: IntentModelClient | None,
        *,
        search_client: GroundedSearchClient | None = None,
        general_chat_enabled: bool = False,
        web_search_enabled: bool = False,
    ) -> None:
        self.intent_client = intent_client
        self.search_client = search_client
        self.general_chat_enabled = general_chat_enabled
        self.web_search_enabled = web_search_enabled

    @staticmethod
    def local_action_evidence(text: str) -> AssistantIntentName | None:
        """Return an action only for an explicit, deterministic command form."""
        parsed = parse_bar_request(text)
        if parsed.action == "writeoff" and parsed.mutation:
            return AssistantIntentName.PREPARE_WRITEOFF
        if parsed.action == "transfer" and parsed.mutation:
            return AssistantIntentName.PREPARE_TRANSFER
        return None

    @staticmethod
    def _guard_provider_intent(
        intent: AssistantIntent,
        allowed_action: AssistantIntentName | None,
    ) -> AssistantIntent:
        if intent.intent not in _WAREHOUSE_ACTION_INTENTS:
            return intent
        if intent.intent is allowed_action:
            return intent
        logger.warning(
            "blocked_action_escalation intent=%s",
            intent.intent.value,
        )
        return AssistantIntent(
            intent=AssistantIntentName.CLARIFY,
            clarification=(
                "Если нужно изменить складские остатки, сформулируйте явную "
                "команду списания или перемещения."
            ),
        )

    @staticmethod
    def local_intent(text: str) -> AssistantIntent | None:
        cleaned = " ".join(text.split())
        if not cleaned or len(cleaned) > MAX_ASSISTANT_TEXT_LENGTH:
            return AssistantIntent(
                intent=AssistantIntentName.CLARIFY,
                clarification="Сформулируйте запрос короче и конкретнее.",
            )
        if _EXPLICIT_WEB_RE.search(cleaned):
            return AssistantIntent(
                intent=AssistantIntentName.WEB_SEARCH,
                query=cleaned,
                needs_web=True,
            )
        if _OPEN_ATTENDANCE_RE.search(cleaned):
            return AssistantIntent(intent=AssistantIntentName.LIST_OPEN_ATTENDANCE_SESSIONS)
        if _CURRENT_SHIFT_RE.search(cleaned):
            return AssistantIntent(intent=AssistantIntentName.GET_CURRENT_SHIFT_SUMMARY)
        if _UNSUPPORTED_RE.search(cleaned):
            return AssistantIntent(intent=AssistantIntentName.UNSUPPORTED)
        parsed = parse_bar_request(cleaned)
        if parsed.mutation and parsed.action in {"writeoff", "transfer"}:
            return AssistantIntent(
                intent=(
                    AssistantIntentName.PREPARE_WRITEOFF
                    if parsed.action == "writeoff"
                    else AssistantIntentName.PREPARE_TRANSFER
                ),
                confidence=parsed.confidence,
                items=[
                    AssistantIntentItem(
                        product_text=parsed.entity or None,
                        quantity_text=(
                            format(parsed.quantity, "f")
                            if parsed.quantity is not None
                            else None
                        ),
                        unit_text=parsed.unit or None,
                        destination_text=parsed.destination or None,
                        comment_text=parsed.comment or None,
                    )
                ],
                evidence=parsed.evidence,
            )
        if parsed.action == "search" and parsed.domain is AssistantDomain.CATALOG and parsed.entity:
            query = parsed.entity
            if parsed.relation is AssistantRelation.DIRECT_LOOKUP:
                legacy_match = next(
                    (match for pattern in _TECH_PATTERNS if (match := pattern.fullmatch(cleaned))),
                    None,
                )
                if legacy_match is not None:
                    query = legacy_match.group("query").strip(" «»\"'.")
            return AssistantIntent(
                intent=AssistantIntentName.FIND_TECH_CARD,
                confidence=parsed.confidence,
                query=query,
                domain=parsed.domain,
                requested_kind=parsed.requested_kind,
                relation=parsed.relation,
                relation_target=query,
                evidence=parsed.evidence,
            )
        for pattern in _TECH_PATTERNS:
            match = pattern.fullmatch(cleaned)
            if match is not None:
                query = match.group("query").strip(" «»\"'.")
                if query:
                    return AssistantIntent(
                        intent=AssistantIntentName.FIND_TECH_CARD,
                        confidence=10_000,
                        query=query,
                        domain=AssistantDomain.CATALOG,
                        requested_kind=AssistantRequestedKind.TECH_CARD,
                        relation=AssistantRelation.DIRECT_LOOKUP,
                        relation_target=query,
                        evidence=("legacy_tech_pattern",),
                    )
        for pattern, intent_name in (
            (_WRITEOFF_PARTIAL_RE, AssistantIntentName.PREPARE_WRITEOFF),
            (_TRANSFER_PARTIAL_RE, AssistantIntentName.PREPARE_TRANSFER),
        ):
            match = pattern.fullmatch(cleaned)
            if match is not None and not _QUANTITY_HINT_RE.search(cleaned):
                product = match.group("product").strip(" «»\"'.")
                if product:
                    return AssistantIntent(
                        intent=intent_name,
                        confidence=10_000,
                        items=[AssistantIntentItem(product_text=product)],
                    )
        if (
            _WEB_REQUIRED_RE.search(cleaned)
            and not _WAREHOUSE_ACTION_PREFIX_RE.search(cleaned)
        ):
            return AssistantIntent(
                intent=AssistantIntentName.WEB_SEARCH,
                query=cleaned,
                needs_web=True,
            )
        return None

    async def interpret(
        self,
        text: str,
        *,
        candidates: Sequence[str] = (),
        normalized_entity: str = "",
        conversation: Sequence[tuple[str, str]] = (),
        allowed_action: AssistantIntentName | None = None,
        allowed_capability_ids: Sequence[str] = (),
    ) -> AssistantIntent:
        local = self.local_intent(text)
        if local is not None:
            return local
        if self.intent_client is None:
            raise AssistantUnavailableError("intent provider is disabled")
        kwargs = {
            "candidates": candidates,
            "normalized_entity": normalized_entity,
            "conversation": conversation,
            "allowed_action_intents": (allowed_action,) if allowed_action else (),
        }
        if "allowed_capability_ids" in inspect.signature(
            self.intent_client.classify
        ).parameters:
            kwargs["allowed_capability_ids"] = allowed_capability_ids
        intent = await self.intent_client.classify(text, **kwargs)
        return self._guard_provider_intent(intent, allowed_action)

    async def interpret_with_provider(
        self,
        text: str,
        *,
        candidates: Sequence[str] = (),
        normalized_entity: str = "",
        conversation: Sequence[tuple[str, str]] = (),
        allowed_action: AssistantIntentName | None = None,
        allowed_capability_ids: Sequence[str] = (),
    ) -> AssistantIntent:
        """Consult the provider after local retrieval needs disambiguation."""

        if self.intent_client is None:
            raise AssistantUnavailableError("intent provider is disabled")
        kwargs = {
            "candidates": candidates,
            "normalized_entity": normalized_entity,
            "conversation": conversation,
            "allowed_action_intents": (allowed_action,) if allowed_action else (),
        }
        if "allowed_capability_ids" in inspect.signature(
            self.intent_client.classify
        ).parameters:
            kwargs["allowed_capability_ids"] = allowed_capability_ids
        intent = await self.intent_client.classify(text, **kwargs)
        return self._guard_provider_intent(intent, allowed_action)

    async def search_web(
        self,
        query: str,
        *,
        conversation: Sequence[tuple[str, str]] = (),
    ) -> GroundedSearchResult:
        if not self.web_search_enabled or self.search_client is None:
            raise AssistantUnavailableError("web search is disabled")
        return await self.search_client.search(query, conversation=conversation)


class AssistantToolRegistry:
    """Execute only catalog lookup or canonical intake preparation."""

    def __init__(self, catalog: Catalog) -> None:
        self.retriever = AssistantCatalogRetriever(catalog)

    def relevant_candidate_names(
        self,
        text: str,
        *,
        limit: int = 5,
    ) -> tuple[str, ...]:
        retrieval = self.retriever.retrieve(text, limit=limit)
        return tuple(
            candidate.provider_label for candidate in retrieval.candidates
        )

    def execute(self, intent: AssistantIntent) -> AssistantToolAction:
        if intent.intent is AssistantIntentName.GENERAL_CHAT:
            return AssistantToolAction(
                "general_chat",
                message=intent.answer_text,
            )
        if intent.intent is AssistantIntentName.WEB_SEARCH:
            return AssistantToolAction(
                "web_search",
                entity_query=intent.query,
            )
        if intent.intent is AssistantIntentName.LIST_OPEN_ATTENDANCE_SESSIONS:
            return AssistantToolAction("list_open_attendance_sessions")
        if intent.intent is AssistantIntentName.GET_CURRENT_SHIFT_SUMMARY:
            return AssistantToolAction("get_current_shift_summary")
        if intent.intent is AssistantIntentName.UNSUPPORTED:
            return AssistantToolAction(
                "unsupported",
                message="Эта команда пока недоступна в AI-помощнике.",
            )
        if intent.intent is AssistantIntentName.CLARIFY:
            return AssistantToolAction(
                "clarify",
                message=intent.clarification
                or "Уточните, пожалуйста, что нужно сделать.",
            )
        if intent.intent is AssistantIntentName.NOT_FOUND:
            entity = display_catalog_entity(intent.query or "позиция")
            return AssistantToolAction(
                "not_found",
                message=(
                    f"Точной техкарты «{entity}» нет.\n\n"
                    "Попробуйте другое название или часть названия."
                ),
                entity_query=intent.query,
            )
        if intent.intent is AssistantIntentName.FIND_TECH_CARD:
            retrieval = self.retriever.retrieve(
                intent.query or "",
                requested_kind=intent.requested_kind,
                relation=intent.relation,
                relation_target=intent.relation_target,
                parent_entity_id=intent.parent_entity_id,
                selected_entity_id=intent.selected_entity_id,
            )
            metadata = {
                "domain": intent.domain,
                "requested_kind": retrieval.requested_kind,
                "relation": retrieval.relation,
                "relation_target": retrieval.relation_target,
                "parent_entity_id": retrieval.parent_entity_ids[0]
                if len(retrieval.parent_entity_ids) == 1
                else None,
                "related_entity_ids": retrieval.related_entity_ids,
            }
            if retrieval.exact:
                return AssistantToolAction(
                    "find_tech_card",
                    search_results=retrieval.exact,
                    candidate_ids=tuple(
                        self._candidate_key(result) for result in retrieval.exact
                    ),
                    entity_query=retrieval.normalized_query,
                    catalog_exact=True,
                    selected_entity_id=(
                        self._candidate_key(retrieval.exact[0])
                        if len(retrieval.exact) == 1
                        else None
                    ),
                    **metadata,
                )
            if retrieval.candidates:
                return AssistantToolAction(
                    "find_tech_card",
                    message="Возможно, вы имели в виду:",
                    search_results=tuple(
                        candidate.result for candidate in retrieval.candidates
                    ),
                    candidate_ids=tuple(
                        candidate.key for candidate in retrieval.candidates
                    ),
                    entity_query=retrieval.normalized_query,
                    catalog_exact=False,
                    **metadata,
                )
            entity = display_catalog_entity(intent.query or "позиция")
            if not entity:
                entity = "Позиция"
            if retrieval.normalized_query in {
                "коктейль голубые гаваи",
                "голубые гаваи",
                "blue hawaii",
            }:
                return AssistantToolAction(
                    "clarify",
                    message=(
                        "Ищем утверждённую техкарту Truffle или рассказать "
                        "общий рецепт?"
                    ),
                    entity_query=retrieval.normalized_query,
                )
            return AssistantToolAction(
                "not_found",
                message=(
                    f"Точной техкарты «{entity}» нет.\n\n"
                    "Попробуйте другое название или часть названия."
                ),
                entity_query=retrieval.normalized_query,
                **metadata,
            )
        if intent.confidence < 5_000:
            return AssistantToolAction(
                "clarify",
                message="Уточните продукт, количество и действие.",
            )
        return self._warehouse_action(intent)

    @staticmethod
    def _safe_field(value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = " ".join(value.split())
        if not cleaned or "->" in cleaned or "\n" in value:
            return None
        return cleaned

    def _warehouse_action(self, intent: AssistantIntent) -> AssistantToolAction:
        lines: list[str] = []
        for item in intent.items:
            product = self._safe_field(item.product_text)
            quantity = self._safe_field(item.quantity_text)
            unit = self._safe_field(item.unit_text)
            destination = self._safe_field(item.destination_text)
            comment = self._safe_field(item.comment_text)
            pending: list[str] = []
            if not product:
                pending.append("product")
            if not quantity:
                pending.append("quantity")
            if (
                intent.intent is AssistantIntentName.PREPARE_TRANSFER
                and not destination
            ):
                pending.append("destination")
            if pending:
                if pending == ["quantity"]:
                    prompt = "Укажите количество."
                elif pending == ["destination"]:
                    prompt = "Укажите направление перемещения."
                else:
                    labels = {
                        "product": "продукт",
                        "quantity": "количество",
                        "destination": "направление",
                    }
                    prompt = "Укажите " + " и ".join(
                        labels[value] for value in pending
                    ) + "."
                return AssistantToolAction(
                    "clarify",
                    message=prompt,
                    pending_slots=tuple(pending),
                    entity_query=product,
                )
            line = f"{product} {quantity}"
            if unit:
                line += f" {unit}"
            if intent.intent is AssistantIntentName.PREPARE_TRANSFER and destination:
                line += f" -> {destination}"
            if comment:
                safe_comment = comment.replace("(", "[").replace(")", "]")
                line += f" ({safe_comment})"
            lines.append(line)
        if intent.intent is AssistantIntentName.PREPARE_WRITEOFF:
            return AssistantToolAction(
                "prepare_writeoff",
                raw_intake="списание:\n" + "\n".join(lines),
            )
        if intent.intent is AssistantIntentName.PREPARE_TRANSFER:
            return AssistantToolAction(
                "prepare_transfer",
                raw_intake="перемещение:\n" + "\n".join(lines),
            )
        return AssistantToolAction(
            "unsupported",
            message="Эта команда пока недоступна в AI-помощнике.",
        )

    @staticmethod
    def _candidate_key(result: SearchResult) -> str:
        subcategory_id = result.subcategory.id if result.subcategory else "_"
        return f"{result.category.id}/{subcategory_id}/{result.item.id}"
