"""Local-first intent routing and a narrow allowlisted tool registry."""

from __future__ import annotations

import asyncio
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
from app.models.assistant_planner import (
    AssistantDialogueAct,
    PlannerCapability,
    PlannerContextSnapshot,
    PlannerDecision,
)
from app.models.catalog import Catalog
from app.services.assistant.providers import (
    ProviderError,
    ProviderErrorKind,
    GroundedSearchClient,
    GroundedSearchResult,
    IntentModelClient,
    PlannerModelClient,
)
from app.services.assistant.bar_language import parse_bar_request
from app.models.assistant_mutation import ParsedMutation
from app.services.assistant.mutation_language import parse_mutation, mutation_reply
from app.services.assistant.retrieval import (
    AssistantCatalogRetriever,
    display_catalog_entity,
)
from app.services.assistant.shadow import (
    ShadowLocalDecision,
    ShadowPlannerSupervisor,
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
_CONTEXT_MUTATION_FOLLOWUP_RE = re.compile(
    r"\b(?:измени|изменить|исправь|редактир|не\s+это|"
    r"имел[аи]?\s+в\s+виду|точнее)\b",
    re.IGNORECASE,
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
        planner_mode: Literal["local_first", "shadow", "ai_first"] = "local_first",
        planner_client: PlannerModelClient | None = None,
        shadow_supervisor: ShadowPlannerSupervisor | None = None,
    ) -> None:
        self.intent_client = intent_client
        self.search_client = search_client
        self.general_chat_enabled = general_chat_enabled
        self.web_search_enabled = web_search_enabled
        self.planner_mode = planner_mode
        self.planner_client = planner_client
        self.shadow_supervisor = shadow_supervisor or (
            ShadowPlannerSupervisor()
            if planner_mode == "shadow" and self.planner_client is not None
            else None
        )

    async def plan(
        self,
        text: str,
        *,
        snapshot: PlannerContextSnapshot,
        capabilities: Sequence[PlannerCapability],
    ) -> PlannerDecision:
        if self.planner_client is None:
            raise AssistantUnavailableError("planner provider is disabled")
        decision = await self.planner_client.plan(
            text, snapshot=snapshot, capabilities=capabilities
        )
        allowed = {item.capability_id for item in capabilities}
        if decision.capability_id is not None and decision.capability_id not in allowed:
            raise ValueError("planner capability escaped the runtime allowlist")
        return decision

    async def observe_shadow(
        self,
        text: str,
        *,
        snapshot: PlannerContextSnapshot,
        capabilities: Sequence[PlannerCapability],
    ) -> PlannerDecision:
        """Evaluate a plan without returning or executing it."""

        from app.services.assistant.planner_policy import CURRENT_REQUEST, planner_request
        parent = CURRENT_REQUEST.get()
        with planner_request(fresh=True, provider_allowed=parent.provider_allowed if parent else True):
            return await self.plan(text, snapshot=snapshot, capabilities=capabilities)

    @staticmethod
    def shadow_local_decision(
        intent: AssistantIntent,
        *,
        route_category: str = "deterministic_local",
    ) -> ShadowLocalDecision:
        capability_by_intent = {
            AssistantIntentName.FIND_TECH_CARD: "catalog.search",
            AssistantIntentName.PREPARE_WRITEOFF: "warehouse.prepare_writeoff",
            AssistantIntentName.PREPARE_TRANSFER: "warehouse.prepare_transfer",
            AssistantIntentName.LIST_OPEN_ATTENDANCE_SESSIONS: "attendance.list_open",
            AssistantIntentName.GET_CURRENT_SHIFT_SUMMARY: "warehouse.shift_summary",
        }
        mutation = intent.intent in _WAREHOUSE_ACTION_INTENTS
        return ShadowLocalDecision(
            route_category=route_category,
            dialogue_act=AssistantDialogueAct.NEW_COMMAND,
            domain=intent.domain,
            capability_id=(
                intent.capability_id
                if intent.intent is AssistantIntentName.CAPABILITY
                else capability_by_intent.get(intent.intent)
            ),
            target_entity_id=intent.selected_entity_id,
            mutation=mutation,
            needs_clarification=intent.intent is AssistantIntentName.CLARIFY,
            validation_passed=True,
        )

    def start_shadow(
        self,
        text: str,
        *,
        user_key: object,
        snapshot: PlannerContextSnapshot,
        capabilities: Sequence[PlannerCapability],
        local: ShadowLocalDecision,
    ) -> bool:
        """Submit a comparison attempt without extending the primary request."""

        if self.planner_mode != "shadow" or self.shadow_supervisor is None:
            return False
        request_size = len(text.encode("utf-8")) + len(
            snapshot.model_dump_json().encode("utf-8")
        ) + sum(len(item.model_dump_json().encode("utf-8")) for item in capabilities)
        return self.shadow_supervisor.submit(
            user_key=user_key,
            planner_call=lambda: self.observe_shadow(
                text,
                snapshot=snapshot,
                capabilities=capabilities,
            ),
            local=local,
            snapshot=snapshot,
            capabilities=capabilities,
            request_size=request_size,
        )

    async def shutdown(self) -> None:
        if self.shadow_supervisor is not None:
            await self.shadow_supervisor.shutdown()

    @staticmethod
    def planner_decision_to_intent(decision: PlannerDecision) -> AssistantIntent:
        if decision.needs_clarification:
            return AssistantIntent(
                intent=AssistantIntentName.CLARIFY,
                clarification=decision.clarification_question,
            )
        if decision.capability_id in {"catalog.search", "catalog.get"}:
            query = str(decision.arguments.get("query") or "").strip()
            if not query and decision.target_entity_id:
                query = str(decision.arguments.get("display_name") or "").strip()
            if not query:
                return AssistantIntent(
                    intent=AssistantIntentName.CLARIFY,
                    clarification="Уточните, какую позицию каталога найти.",
                )
            return AssistantIntent(
                intent=AssistantIntentName.FIND_TECH_CARD,
                query=query,
                domain=AssistantDomain.CATALOG,
                selected_entity_id=decision.target_entity_id,
            )
        if decision.capability_id in {
            "warehouse.prepare_writeoff", "warehouse.prepare_transfer"
        }:
            args = decision.arguments
            return AssistantIntent(
                intent=(
                    AssistantIntentName.PREPARE_WRITEOFF
                    if decision.capability_id == "warehouse.prepare_writeoff"
                    else AssistantIntentName.PREPARE_TRANSFER
                ),
                items=[AssistantIntentItem(
                    product_text=args.get("product"),
                    quantity_text=args.get("quantity"),
                    unit_text=args.get("unit"),
                    destination_text=args.get("destination"),
                    comment_text=args.get("comment"),
                )],
            )
        if decision.capability_id:
            return AssistantIntent(
                intent=AssistantIntentName.CAPABILITY,
                capability_id=decision.capability_id,
            )
        if decision.dialogue_act in {
            AssistantDialogueAct.CONFIRMATION,
            AssistantDialogueAct.CANCELLATION,
        }:
            return AssistantIntent(
                intent=AssistantIntentName.CLARIFY,
                clarification="Используйте кнопку у ожидающего подтверждения.",
            )
        return AssistantIntent(
            intent=AssistantIntentName.CLARIFY,
            clarification="Уточните, пожалуйста, что нужно сделать.",
        )

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
    def mutation_intent(parsed: ParsedMutation) -> AssistantIntent:
        if not parsed.allowed:
            return AssistantIntent(intent=AssistantIntentName.CLARIFY,
                clarification=mutation_reply(parsed))
        return AssistantIntent(
            intent=(AssistantIntentName.PREPARE_WRITEOFF if parsed.action == 'writeoff'
                else AssistantIntentName.PREPARE_TRANSFER), confidence=10_000,
            items=[AssistantIntentItem(product_text=line.entity or None,
                quantity_text=format(line.quantity,'f') if line.quantity is not None else None,
                unit_text=line.unit or None, destination_text=line.destination or None,
                comment_text=line.comment or None) for line in parsed.lines],
            evidence=(f'explicit_{parsed.action}',),
        )

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
        if mutation := parse_mutation(text):
            return AssistantOrchestrator.mutation_intent(mutation)
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
        planner_snapshot: PlannerContextSnapshot | None = None,
        planner_capabilities: Sequence[PlannerCapability] = (),
        shadow_key: object | None = None,
        shadow_text: str | None = None,
    ) -> AssistantIntent:
        mutation = parse_mutation(text)
        if mutation is not None and not mutation.allowed:
            return self.mutation_intent(mutation)
        if self.planner_mode == "ai_first" and planner_snapshot is not None:
            try:
                decision = await self.plan(
                    text,
                    snapshot=planner_snapshot,
                    capabilities=planner_capabilities,
                )
                return self._guard_provider_intent(
                    self.planner_decision_to_intent(decision),
                    allowed_action,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.info("Assistant planner fallback kind=%s", type(exc).__name__)
                terminal = isinstance(exc, ProviderError) and exc.kind in {
                    ProviderErrorKind.SAFETY_REFUSAL, ProviderErrorKind.AUTHORITY,
                }
                available = isinstance(exc, ProviderError) and exc.kind in {
                    ProviderErrorKind.TIMEOUT, ProviderErrorKind.UNAVAILABLE,
                    ProviderErrorKind.RATE_LIMIT, ProviderErrorKind.BUSY,
                }
                local = self.local_intent(text)
                if terminal or (local and local.intent in {
                    AssistantIntentName.PREPARE_WRITEOFF, AssistantIntentName.PREPARE_TRANSFER,
                } and not available):
                    return AssistantIntent(intent=AssistantIntentName.CLARIFY,
                        clarification='Запрос не прошёл безопасную проверку. Уточните действие или используйте меню.')
                if (
                    planner_snapshot.last_successful_entity_id
                    and _CONTEXT_MUTATION_FOLLOWUP_RE.search(text)
                ):
                    return AssistantIntent(
                        intent=AssistantIntentName.CLARIFY,
                        clarification=(
                            "Уточните, что именно нужно изменить в выбранной позиции."
                        ),
                    )
                # Never re-enter the legacy Gemini intent adapter after the
                # bounded planner chain. That would be a hidden extra attempt.
                return self.local_intent(text) or AssistantIntent(
                    intent=AssistantIntentName.CLARIFY,
                    clarification="Уточните действие или используйте меню BarBot.",
                )
        local = self.local_intent(text)
        if local is not None:
            if self.planner_mode == "shadow" and planner_snapshot is not None:
                self.start_shadow(
                    shadow_text or text,
                    user_key=shadow_key if shadow_key is not None else object(),
                    snapshot=planner_snapshot,
                    capabilities=planner_capabilities,
                    local=self.shadow_local_decision(local),
                )
            return local
        if self.planner_mode == 'shadow' and planner_snapshot is not None:
            local = AssistantIntent(intent=AssistantIntentName.CLARIFY,
                clarification='Уточните запрос или используйте меню BarBot.')
            self.start_shadow(shadow_text or text,
                user_key=shadow_key if shadow_key is not None else object(),
                snapshot=planner_snapshot, capabilities=planner_capabilities,
                local=self.shadow_local_decision(local, route_category='local_clarification'))
            return local
        if self.intent_client is None:
            # Groq-only planning must not silently depend on a disabled Gemini
            # fallback/legacy client. Local routes above still have priority.
            if self.planner_client is not None and planner_snapshot is not None:
                decision = await self.plan(text, snapshot=planner_snapshot,
                    capabilities=planner_capabilities)
                return self._guard_provider_intent(self.planner_decision_to_intent(decision), allowed_action)
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
        guarded = self._guard_provider_intent(intent, allowed_action)
        if self.planner_mode == "shadow" and planner_snapshot is not None:
            self.start_shadow(
                shadow_text or text,
                user_key=shadow_key if shadow_key is not None else object(),
                snapshot=planner_snapshot,
                capabilities=planner_capabilities,
                local=self.shadow_local_decision(
                    guarded, route_category="legacy_provider_primary"
                ),
            )
        return guarded

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

    def execute(self, intent: AssistantIntent, *, mutation_request: ParsedMutation | None = None) -> AssistantToolAction:
        if mutation_request is not None and intent.intent in _WAREHOUSE_ACTION_INTENTS:
            expected = ('writeoff' if intent.intent is AssistantIntentName.PREPARE_WRITEOFF else 'transfer')
            if not mutation_request.allowed or mutation_request.missing or mutation_request.action != expected:
                return AssistantToolAction('clarify',message=mutation_reply(mutation_request))
            # A server AST/fact record is rendered, never parsed as another
            # command. Product resolution and confirmation still own execution.
            return AssistantToolAction('prepare_'+expected,raw_intake=mutation_request.command())
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
        header = 'списание:' if intent.intent is AssistantIntentName.PREPARE_WRITEOFF else 'перемещение:'
        parsed = parse_mutation(header + '\n' + '\n'.join(lines))
        if parsed is None or not parsed.allowed:
            return AssistantToolAction('clarify', message=mutation_reply(parsed) if parsed else 'Уточните позицию.')
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
