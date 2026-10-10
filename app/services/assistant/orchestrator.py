"""Local-first intent routing and a narrow allowlisted tool registry."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Sequence

from app.models.assistant import (
    AssistantIntent,
    AssistantIntentItem,
    AssistantIntentName,
)
from app.models.catalog import Catalog
from app.services.assistant.providers import IntentModelClient
from app.services.search_service import SearchResult, SearchService
from app.utils.text import normalize_search_text

MAX_ASSISTANT_TEXT_LENGTH = 2_000
_UNSUPPORTED_RE = re.compile(
    r"\b(?:интернет|перезапуст\w*|измени\s+прав|поменяй|редактир|"
    r"добавь\s+(?:описание|продукт)|создай\s+продукт)\b",
    re.IGNORECASE,
)
_TECH_PATTERNS = (
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


class AssistantUnavailableError(RuntimeError):
    """Raised when a free-form request needs a disabled provider."""


@dataclass(frozen=True, slots=True)
class AssistantToolAction:
    kind: Literal[
        "find_tech_card",
        "prepare_writeoff",
        "prepare_transfer",
        "clarify",
        "unsupported",
        "not_found",
    ]
    message: str | None = None
    raw_intake: str | None = None
    search_results: tuple[SearchResult, ...] = ()


class AssistantOrchestrator:
    """Recognize deterministic commands before consulting Gemini."""

    def __init__(self, intent_client: IntentModelClient | None) -> None:
        self.intent_client = intent_client

    @staticmethod
    def local_intent(text: str) -> AssistantIntent | None:
        cleaned = " ".join(text.split())
        if not cleaned or len(cleaned) > MAX_ASSISTANT_TEXT_LENGTH:
            return AssistantIntent(
                intent=AssistantIntentName.CLARIFY,
                clarification="Сформулируйте запрос короче и конкретнее.",
            )
        if _UNSUPPORTED_RE.search(cleaned):
            return AssistantIntent(intent=AssistantIntentName.UNSUPPORTED)
        for pattern in _TECH_PATTERNS:
            match = pattern.fullmatch(cleaned)
            if match is not None:
                query = match.group("query").strip(" «»\"'.")
                if query:
                    return AssistantIntent(
                        intent=AssistantIntentName.FIND_TECH_CARD,
                        confidence=10_000,
                        query=query,
                    )
        return None

    async def interpret(
        self,
        text: str,
        *,
        candidates: Sequence[str] = (),
    ) -> AssistantIntent:
        local = self.local_intent(text)
        if local is not None:
            return local
        if self.intent_client is None:
            raise AssistantUnavailableError("intent provider is disabled")
        return await self.intent_client.classify(text, candidates=candidates)


class AssistantToolRegistry:
    """Execute only catalog lookup or canonical intake preparation."""

    def __init__(self, catalog: Catalog) -> None:
        self.search = SearchService(catalog)

    def relevant_candidate_names(
        self,
        text: str,
        *,
        limit: int = 8,
    ) -> tuple[str, ...]:
        return tuple(result.item.name for result in self.search.search(text)[:limit])

    def execute(self, intent: AssistantIntent) -> AssistantToolAction:
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
        if intent.intent is AssistantIntentName.FIND_TECH_CARD:
            results = tuple(self.search.search(intent.query or "")[:20])
            if not results:
                return AssistantToolAction(
                    "not_found",
                    message=f"Техкарта «{intent.query or ''}» не найдена.",
                )
            return AssistantToolAction("find_tech_card", search_results=results)
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
            if not product or not quantity:
                return AssistantToolAction(
                    "clarify",
                    message="Укажите продукт и количество.",
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
