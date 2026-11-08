"""Bounded context assembly, reference resolution and guarded tool looping."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Awaitable, Callable, Sequence

import asyncio

from app.models.assistant import AssistantDomain
from app.models.assistant_planner import (
    PlannerCapability,
    PlannerContextSnapshot,
    PlannerTargetOption,
    PlannerTurn,
    PlannerMemoryData,
    PlannerDecision,
    PlannerToolData,
)
from app.services.assistant.context import AssistantConversationContext

MAX_CONTEXT_BYTES = 24_000
MAX_TURNS = 12
MAX_CANDIDATES = 5
MAX_MEMORIES = 8
MAX_TOOL_RESULT_CHARS = 500


class ReferenceKind(StrEnum):
    NONE = "none"
    SELECTED_ENTITY = "selected_entity"
    CANDIDATE = "candidate"
    DESTINATION = "destination"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class ResolvedReference:
    kind: ReferenceKind
    stable_id: str | None = None
    clarification: str | None = None


_REFERENCE_RE = re.compile(
    r"\b(?:он|она|оно|его|ее|её|него|нее|неё|нем|нём|ней|ему|ей|"
    r"этот|эта|это|эту|данная\s+позиция|эта\s+карточка|"
    r"эта\s+заготовка|этот\s+рецепт)\b",
    re.IGNORECASE,
)
_ORDINALS = {"перв": 0, "втор": 1}


def _normalize(text: str) -> str:
    return " ".join(
        unicodedata.normalize("NFKC", text).casefold().replace("ё", "е").split()
    )


class ContextReferenceResolver:
    """Resolve references only against server-owned stable identifiers."""

    def resolve(self, text: str, context: AssistantConversationContext | None) -> ResolvedReference:
        normalized = _normalize(text)
        if context is None:
            return ResolvedReference(ReferenceKind.NONE)
        if "туда же" in normalized:
            if context.last_tool_result_ref:
                return ResolvedReference(ReferenceKind.DESTINATION, context.last_tool_result_ref)
            return ResolvedReference(ReferenceKind.AMBIGUOUS, clarification="Куда именно?")
        if "последн" in normalized and context.current_candidates:
            return ResolvedReference(ReferenceKind.CANDIDATE, context.current_candidates[-1])
        for stem, index in _ORDINALS.items():
            if stem in normalized:
                if len(context.current_candidates) > index:
                    return ResolvedReference(ReferenceKind.CANDIDATE, context.current_candidates[index])
                return ResolvedReference(ReferenceKind.AMBIGUOUS, clarification="Какой вариант выбрать?")
        if _REFERENCE_RE.search(normalized):
            if context.last_successful_entity_id:
                return ResolvedReference(
                    ReferenceKind.SELECTED_ENTITY,
                    context.last_successful_entity_id,
                )
            if len(context.current_candidates) == 1:
                return ResolvedReference(ReferenceKind.CANDIDATE, context.current_candidates[0])
            return ResolvedReference(
                ReferenceKind.AMBIGUOUS,
                clarification="Какой именно объект вы имеете в виду?",
            )
        return ResolvedReference(ReferenceKind.NONE)


def build_context_snapshot(
    *,
    context: AssistantConversationContext | None,
    actor_role: str,
    access_mode: str,
    capabilities: Sequence[PlannerCapability],
    targets: Sequence[PlannerTargetOption] = (),
    feature_flags: Sequence[str] = (),
    owner_memories: Sequence[PlannerMemoryData | str] = (),
) -> PlannerContextSnapshot:
    """Build a deterministic provider snapshot without actor/chat identifiers."""

    turns = context.turns[-MAX_TURNS:] if context else ()
    snapshot = PlannerContextSnapshot(
        actor_role=actor_role,
        access_mode=access_mode,
        available_capability_ids=tuple(item.capability_id for item in capabilities),
        feature_flags=tuple(feature_flags[:12]),
        current_domain=_domain(context.last_entity_type if context else ""),
        dialogue_state=context.dialogue_state if context else "idle",
        rolling_summary=(context.summary_text or None) if context else None,
        last_successful_entity_id=(context.last_successful_entity_id or None) if context else None,
        last_successful_entity_name=(context.last_successful_entity_label or None) if context else None,
        candidate_entity_ids=context.current_candidates[:MAX_CANDIDATES] if context else (),
        target_options=tuple(targets[:MAX_CANDIDATES]),
        pending_clarification=(context.pending_clarification or None) if context else None,
        pending_action=(context.pending_task or context.authorized_action or None) if context else None,
        last_tool_operation=(context.last_successful_capability or context.last_tool_name or None) if context else None,
        last_tool_summary=(context.last_tool_result_ref or None) if context else None,
        recent_turns=tuple(PlannerTurn(role=turn.role, text=turn.text[:1000]) for turn in turns),
        owner_memories=tuple(
            item if isinstance(item, PlannerMemoryData)
            else PlannerMemoryData(content=" ".join(item.split())[:500])
            for item in owner_memories[:MAX_MEMORIES]
        ),
        revision=context.revision if context else 0,
        expires_at=context.expires_at if context else None,
    )
    if len(snapshot.model_dump_json().encode("utf-8")) > MAX_CONTEXT_BYTES:
        raise ValueError("conversation snapshot exceeds the bounded contract")
    return snapshot


def _domain(value: str) -> AssistantDomain:
    try:
        return AssistantDomain(value)
    except ValueError:
        return AssistantDomain.UNSPECIFIED


@dataclass(frozen=True, slots=True)
class ToolLoopStep:
    capability_id: str
    summary: str
    read_only: bool
    proposal_ref: str | None = None


class ConversationToolLoop:
    """Run at most two server-authorized steps; mutations stop at preview."""

    def __init__(self, *, max_steps: int = 2) -> None:
        if max_steps != 2:
            raise ValueError("conversation tool loop is fixed at two steps")
        self.max_steps = max_steps

    async def run(
        self,
        capability_ids: Sequence[str],
        *,
        allowed: set[str],
        is_read_only: Callable[[str], bool],
        execute_read: Callable[[str], Awaitable[str]],
        prepare_mutation: Callable[[str], Awaitable[str]],
    ) -> tuple[ToolLoopStep, ...]:
        if len(capability_ids) > self.max_steps:
            raise ValueError("tool loop exceeds two steps")
        results: list[ToolLoopStep] = []
        for capability_id in capability_ids:
            if capability_id not in allowed:
                raise PermissionError("capability is not available at runtime")
            if is_read_only(capability_id):
                summary = (await execute_read(capability_id))[:MAX_TOOL_RESULT_CHARS]
                results.append(ToolLoopStep(capability_id, summary, True))
            else:
                proposal = await prepare_mutation(capability_id)
                results.append(ToolLoopStep(capability_id, "preview prepared", False, proposal[:100]))
                break
        return tuple(results)

    async def run_iterative(
        self, *, text: str, snapshot: PlannerContextSnapshot,
        capabilities: Sequence[PlannerCapability],
        plan: Callable[..., Awaitable[PlannerDecision]],
        dispatch: Callable[[PlannerDecision, PlannerContextSnapshot], Awaitable[Any]],
    ) -> tuple[tuple[Any, ...], str | None]:
        from app.services.assistant.planner_policy import planner_request
        with planner_request():
            return await self._run_iterative(text=text, snapshot=snapshot,
                capabilities=capabilities, plan=plan, dispatch=dispatch)

    async def _run_iterative(
        self, *, text: str, snapshot: PlannerContextSnapshot,
        capabilities: Sequence[PlannerCapability],
        plan: Callable[..., Awaitable[PlannerDecision]],
        dispatch: Callable[[PlannerDecision, PlannerContextSnapshot], Awaitable[Any]],
    ) -> tuple[tuple[Any, ...], str | None]:
        """Two planner/tool steps, with inert typed results and no executor in input.

        A read result cannot escalate to a mutation. Any preview stops the loop;
        a later provider failure keeps the completed read result intact.
        """
        results: list[Any] = []
        seen: set[tuple[str, str | None]] = set()
        current = snapshot
        allowed = tuple(capabilities)
        for step in range(self.max_steps):
            try:
                # model_copy is intentionally followed by revalidation, including
                # byte limits, before every provider call.
                current = PlannerContextSnapshot.model_validate(current.model_dump())
                decision = await plan(text, snapshot=current, capabilities=allowed)
                if decision.capability_id is None:
                    return tuple(results), decision.clarification_question
                contract = next((c for c in allowed if c.capability_id == decision.capability_id), None)
                if contract is None or decision.capability_id not in current.available_capability_ids:
                    raise PermissionError('Unavailable planned tool')
                if decision.read_only != contract.read_only or decision.mutation != contract.mutation:
                    raise PermissionError('Planned tool risk mismatch')
                allowed_targets = {option.stable_id for option in current.target_options}
                if not current.target_options:
                    allowed_targets.update(current.candidate_entity_ids)
                    if current.last_successful_entity_id:
                        allowed_targets.add(current.last_successful_entity_id)
                if decision.target_entity_id is not None and decision.target_entity_id not in allowed_targets:
                    raise PermissionError('Unknown planned target')
                if decision.needs_clarification:
                    return tuple(results), decision.clarification_question
                key = (decision.capability_id, decision.target_entity_id)
                if key in seen:
                    break
                seen.add(key)
                from app.services.assistant.planner_policy import mark_tool_boundary
                mark_tool_boundary()
                result = await dispatch(decision, current)
            except asyncio.CancelledError:
                raise
            except Exception:
                if results:
                    break
                raise
            results.append(result)
            if not contract.read_only:
                break
            allowed = tuple(cap for cap in capabilities if cap.read_only and cap.domain == contract.domain)
            if not allowed:
                break
            current = current.model_copy(update={
                'available_capability_ids': tuple(cap.capability_id for cap in allowed),
                'tool_results': (*current.tool_results, PlannerToolData(
                    capability_id=contract.capability_id, summary=result.message[:MAX_TOOL_RESULT_CHARS],
                )),
                'last_tool_operation': contract.capability_id,
                'last_tool_summary': result.message[:MAX_TOOL_RESULT_CHARS],
            })
        return tuple(results), None
