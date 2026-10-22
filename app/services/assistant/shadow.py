"""Bounded, side-effect-free supervision for planner shadow evaluation."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Hashable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from time import monotonic
from typing import Protocol
from uuid import uuid4

from app.models.assistant import AssistantDomain
from app.models.assistant_planner import (
    AssistantDialogueAct,
    PlannerCapability,
    PlannerContextSnapshot,
    PlannerDecision,
)
from app.services.assistant.providers import ProviderError, ProviderErrorKind

logger = logging.getLogger(__name__)

SHADOW_EVENT_VERSION = 1
MAX_SHADOW_LATENCY_MS = 60_000


class ShadowOutcome(StrEnum):
    SUCCESS = "success"
    ERROR = "error"
    DROPPED = "dropped"
    CANCELLED = "cancelled"


class ShadowErrorCategory(StrEnum):
    NONE = "none"
    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    SERVER = "server"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    MALFORMED_RESPONSE = "malformed_response"
    RESPONSE_TOO_LARGE = "response_too_large"
    INTERNAL = "internal"
    CANCELLED = "cancelled"
    BUSY = "busy"


@dataclass(frozen=True, slots=True)
class ShadowLocalDecision:
    """Privacy-safe local result used only for shadow comparison."""

    route_category: str
    dialogue_act: AssistantDialogueAct | None
    domain: AssistantDomain
    capability_id: str | None
    target_entity_id: str | None
    mutation: bool
    needs_clarification: bool
    validation_passed: bool


@dataclass(frozen=True, slots=True)
class ShadowEvaluationEvent:
    event_version: int
    correlation_id: str
    planner_mode: str
    local_route_category: str
    local_dialogue_act: str | None
    local_domain: str
    local_capability_id: str | None
    local_target_stable_id: str | None
    planner_outcome: str
    planner_dialogue_act: str | None
    planner_domain: str | None
    planner_capability_id: str | None
    planner_target_stable_id: str | None
    target_match: bool | None
    dialogue_act_match: bool | None
    domain_match: bool | None
    capability_match: bool | None
    overall_disagreement: bool | None
    local_mutation: bool
    local_read_only: bool
    mutation: bool | None
    read_only: bool | None
    needs_clarification: bool | None
    provider_latency_ms: int | None
    provider_error_category: str
    local_validation_passed: bool
    payload_size_bucket: str
    allowed_stable_id_validation: bool | None
    timestamp: str


class ShadowTelemetrySink(Protocol):
    def emit(self, event: ShadowEvaluationEvent) -> None: ...


class LoggingShadowTelemetrySink:
    """Emit one bounded JSON object without user content or Telegram IDs."""

    def emit(self, event: ShadowEvaluationEvent) -> None:
        logger.info(
            "assistant_shadow_evaluation %s",
            json.dumps(asdict(event), ensure_ascii=True, separators=(",", ":")),
        )


def _size_bucket(size: int) -> str:
    if size < 2_000:
        return "lt_2kb"
    if size < 8_000:
        return "lt_8kb"
    if size < 32_000:
        return "lt_32kb"
    return "gte_32kb"


def _error_category(error: BaseException) -> ShadowErrorCategory:
    if isinstance(error, asyncio.TimeoutError):
        return ShadowErrorCategory.TIMEOUT
    if isinstance(error, ProviderError):
        try:
            return ShadowErrorCategory(error.kind.value)
        except ValueError:
            return ShadowErrorCategory.INTERNAL
    return ShadowErrorCategory.INTERNAL


def _safe_emit(sink: ShadowTelemetrySink, event: ShadowEvaluationEvent) -> None:
    try:
        sink.emit(event)
    except Exception:
        # Telemetry is explicitly non-authoritative and must never affect either
        # the primary response or supervisor cleanup.
        logger.warning("assistant_shadow_telemetry_failed")


class ShadowPlannerSupervisor:
    """Own bounded shadow tasks independently of primary request claims."""

    def __init__(
        self,
        *,
        max_concurrency: int = 2,
        timeout_seconds: float = 5.0,
        sink: ShadowTelemetrySink | None = None,
    ) -> None:
        if max_concurrency < 1 or timeout_seconds <= 0:
            raise ValueError("shadow supervisor limits must be positive")
        self.max_concurrency = max_concurrency
        self.timeout_seconds = timeout_seconds
        self.sink = sink or LoggingShadowTelemetrySink()
        self._tasks: set[asyncio.Task[None]] = set()
        self._active_by_user: dict[Hashable, asyncio.Task[None]] = {}
        self._closing = False

    @property
    def active_count(self) -> int:
        return len(self._tasks)

    def submit(
        self,
        *,
        user_key: Hashable,
        planner_call: Callable[[], Awaitable[PlannerDecision]],
        local: ShadowLocalDecision,
        snapshot: PlannerContextSnapshot,
        capabilities: Sequence[PlannerCapability],
        request_size: int,
    ) -> bool:
        """Start a supervised attempt or emit a bounded busy event."""

        correlation_id = uuid4().hex
        if (
            self._closing
            or user_key in self._active_by_user
            or len(self._tasks) >= self.max_concurrency
        ):
            self._emit_result(
                correlation_id=correlation_id,
                local=local,
                snapshot=snapshot,
                decision=None,
                outcome=ShadowOutcome.DROPPED,
                error=ShadowErrorCategory.BUSY,
                latency_ms=None,
                request_size=request_size,
            )
            return False

        async def run() -> None:
            started = monotonic()
            try:
                decision = await asyncio.wait_for(
                    planner_call(), timeout=self.timeout_seconds
                )
            except asyncio.CancelledError:
                self._emit_result(
                    correlation_id=correlation_id,
                    local=local,
                    snapshot=snapshot,
                    decision=None,
                    outcome=ShadowOutcome.CANCELLED,
                    error=ShadowErrorCategory.CANCELLED,
                    latency_ms=self._latency_ms(started),
                    request_size=request_size,
                )
                raise
            except Exception as exc:
                self._emit_result(
                    correlation_id=correlation_id,
                    local=local,
                    snapshot=snapshot,
                    decision=None,
                    outcome=ShadowOutcome.ERROR,
                    error=_error_category(exc),
                    latency_ms=self._latency_ms(started),
                    request_size=request_size,
                )
            else:
                self._emit_result(
                    correlation_id=correlation_id,
                    local=local,
                    snapshot=snapshot,
                    decision=decision,
                    outcome=ShadowOutcome.SUCCESS,
                    error=ShadowErrorCategory.NONE,
                    latency_ms=self._latency_ms(started),
                    request_size=request_size,
                )

        task = asyncio.get_running_loop().create_task(
            run(), name=f"assistant-shadow-{correlation_id[:8]}"
        )
        self._tasks.add(task)
        self._active_by_user[user_key] = task

        def completed(done: asyncio.Task[None]) -> None:
            self._tasks.discard(done)
            if self._active_by_user.get(user_key) is done:
                self._active_by_user.pop(user_key, None)
            # Always retrieve the terminal exception. Cancellation is expected.
            if not done.cancelled():
                try:
                    done.exception()
                except Exception:
                    logger.warning("assistant_shadow_task_cleanup_failed")

        task.add_done_callback(completed)
        return True

    async def drain(self) -> None:
        """Wait for current tasks; primarily useful for deterministic tests."""

        tasks = tuple(self._tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def shutdown(self) -> None:
        """Reject new work, cancel every active task and await cleanup."""

        self._closing = True
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _latency_ms(started: float) -> int:
        return min(MAX_SHADOW_LATENCY_MS, max(0, round((monotonic() - started) * 1000)))

    def _emit_result(
        self,
        *,
        correlation_id: str,
        local: ShadowLocalDecision,
        snapshot: PlannerContextSnapshot,
        decision: PlannerDecision | None,
        outcome: ShadowOutcome,
        error: ShadowErrorCategory,
        latency_ms: int | None,
        request_size: int,
    ) -> None:
        known_ids = {
            value
            for value in (
                snapshot.last_successful_entity_id,
                *snapshot.candidate_entity_ids,
            )
            if value
        }
        allowed_target = (
            None
            if decision is None or decision.target_entity_id is None
            else decision.target_entity_id in known_ids
        )
        comparisons = (
            None
            if decision is None
            else (
                decision.target_entity_id == local.target_entity_id
                if decision.target_entity_id is not None
                or local.target_entity_id is not None
                else None,
                decision.dialogue_act == local.dialogue_act
                if local.dialogue_act is not None
                else None,
                decision.domain == local.domain,
                decision.capability_id == local.capability_id
                if decision.capability_id is not None
                or local.capability_id is not None
                else None,
            )
        )
        target_match, act_match, domain_match, capability_match = (
            (None, None, None, None) if comparisons is None else comparisons
        )
        comparable = tuple(
            value
            for value in (target_match, act_match, domain_match, capability_match)
            if value is not None
        )
        overall = None if not comparable else not all(comparable)
        event = ShadowEvaluationEvent(
            event_version=SHADOW_EVENT_VERSION,
            correlation_id=correlation_id,
            planner_mode="shadow",
            local_route_category=local.route_category,
            local_dialogue_act=(local.dialogue_act.value if local.dialogue_act else None),
            local_domain=local.domain.value,
            local_capability_id=local.capability_id,
            local_target_stable_id=local.target_entity_id,
            planner_outcome=outcome.value,
            planner_dialogue_act=(decision.dialogue_act.value if decision else None),
            planner_domain=(decision.domain.value if decision else None),
            planner_capability_id=(decision.capability_id if decision else None),
            planner_target_stable_id=(decision.target_entity_id if decision else None),
            target_match=target_match,
            dialogue_act_match=act_match,
            domain_match=domain_match,
            capability_match=capability_match,
            overall_disagreement=overall,
            local_mutation=local.mutation,
            local_read_only=not local.mutation,
            mutation=(decision.mutation if decision else None),
            read_only=(decision.read_only if decision else None),
            needs_clarification=(decision.needs_clarification if decision else None),
            provider_latency_ms=latency_ms,
            provider_error_category=error.value,
            local_validation_passed=local.validation_passed,
            payload_size_bucket=_size_bucket(request_size),
            allowed_stable_id_validation=allowed_target,
            timestamp=datetime.now(UTC).isoformat(),
        )
        _safe_emit(self.sink, event)
