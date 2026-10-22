"""Safe diagnostic harness for explicitly authorized planner smoke tests.

This module is not part of the production request path.  It performs no tool
execution and refuses live provider work unless the caller explicitly enables
it for a single diagnostic run.
"""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic
from typing import Sequence

from app.models.assistant_planner import (
    PlannerCapability,
    PlannerContextSnapshot,
    PlannerDecision,
)
from app.services.assistant.providers import PlannerModelClient


@dataclass(frozen=True, slots=True)
class PlannerSmokeCase:
    name: str
    text: str


@dataclass(frozen=True, slots=True)
class PlannerSmokeResult:
    case_name: str
    attempted_call: int
    completed_call: int
    latency_ms: int
    dialogue_act: str
    domain: str
    capability_id: str | None
    target_matches_expected: bool | None
    read_only: bool
    mutation: bool
    needs_clarification: bool
    validation_passed: bool


class PlannerSmokeHarness:
    """Accumulate each validated result immediately and never execute tools."""

    def __init__(
        self,
        client: PlannerModelClient,
        *,
        live_authorized: bool = False,
        max_calls: int = 4,
    ) -> None:
        if max_calls < 1:
            raise ValueError("max_calls must be positive")
        self.client = client
        self.live_authorized = live_authorized
        self.max_calls = max_calls
        self.attempted_calls = 0
        self.completed_calls = 0
        self.results: list[PlannerSmokeResult] = []

    async def run_case(
        self,
        case: PlannerSmokeCase,
        *,
        snapshot: PlannerContextSnapshot,
        capabilities: Sequence[PlannerCapability],
        expected_target_id: str | None = None,
    ) -> PlannerDecision:
        if not self.live_authorized:
            raise PermissionError("live planner smoke requires explicit one-time authorization")
        if self.attempted_calls != self.completed_calls:
            raise RuntimeError("planner call count is indeterminate")
        if self.attempted_calls >= self.max_calls:
            raise RuntimeError("planner smoke call budget exhausted")

        self.attempted_calls += 1
        attempted = self.attempted_calls
        started = monotonic()
        decision = await self.client.plan(
            case.text,
            snapshot=snapshot,
            capabilities=capabilities,
        )
        # PlannerDecision returned by the application adapter has already
        # passed strict Pydantic and allowed stable-ID validation.
        self.completed_calls += 1
        result = PlannerSmokeResult(
            case_name=case.name,
            attempted_call=attempted,
            completed_call=self.completed_calls,
            latency_ms=max(0, round((monotonic() - started) * 1000)),
            dialogue_act=decision.dialogue_act.value,
            domain=decision.domain.value,
            capability_id=decision.capability_id,
            target_matches_expected=(
                None
                if expected_target_id is None and decision.target_entity_id is None
                else decision.target_entity_id == expected_target_id
            ),
            read_only=decision.read_only,
            mutation=decision.mutation,
            needs_clarification=decision.needs_clarification,
            validation_passed=True,
        )
        # Persist in memory before the caller mutates context or starts the next
        # case, so a later failure cannot erase an earlier completed result.
        self.results.append(result)
        return decision

    @staticmethod
    def update_selected_entity(
        snapshot: PlannerContextSnapshot,
        *,
        stable_id: str,
        display_name: str,
    ) -> PlannerContextSnapshot:
        """Update synthetic context using the native Pydantic copy API."""

        return snapshot.model_copy(
            update={
                "last_successful_entity_id": stable_id,
                "last_successful_entity_name": display_name,
                "candidate_entity_ids": (),
            }
        )
