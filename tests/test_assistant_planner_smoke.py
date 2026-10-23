import asyncio

import pytest

from app.models.assistant_planner import (
    PlannerCapability,
    PlannerContextSnapshot,
    PlannerDecision,
)
from app.services.assistant import (
    OFFLINE_PLANNER_CONTRACT_CASES,
    PlannerSmokeCase,
    PlannerSmokeHarness,
)
from app.services.assistant import GeminiPlannerProvider, MultiProviderPlanner, ProviderError
from app.services.assistant.providers import HttpResponse


def _decision() -> PlannerDecision:
    return PlannerDecision.model_validate(
        {
            "dialogue_act": "new_command",
            "domain": "catalog",
            "capability_id": "catalog.search",
            "target_entity_id": "stable-card",
            "arguments": {"query": "bounded"},
            "confidence": 9000,
            "needs_clarification": False,
            "clarification_question": None,
            "read_only": True,
            "mutation": False,
            "context_updates": {},
        }
    )


class Planner:
    def __init__(self) -> None:
        self.calls = 0

    async def plan(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("second failed")
        return _decision()


def _snapshot() -> PlannerContextSnapshot:
    return PlannerContextSnapshot(
        actor_role="super_admin",
        available_capability_ids=("catalog.search",),
        candidate_entity_ids=("stable-card",),
    )


CAPS = (
    PlannerCapability(
        capability_id="catalog.search",
        domain="catalog",
        read_only=True,
        mutation=False,
    ),
)


def test_live_mode_requires_explicit_authorization() -> None:
    harness = PlannerSmokeHarness(Planner())
    with pytest.raises(PermissionError):
        asyncio.run(
            harness.run_case(
                PlannerSmokeCase("one", "synthetic"),
                snapshot=_snapshot(),
                capabilities=CAPS,
            )
        )
    assert harness.attempted_calls == harness.completed_calls == 0


def test_completed_case_survives_later_failure_and_counts_are_exact() -> None:
    async def scenario() -> None:
        harness = PlannerSmokeHarness(Planner(), live_authorized=True)
        snapshot = _snapshot()
        await harness.run_case(
            PlannerSmokeCase("one", "synthetic one"),
            snapshot=snapshot,
            capabilities=CAPS,
            expected_target_id="stable-card",
        )
        assert len(harness.results) == 1
        with pytest.raises(RuntimeError, match="second failed"):
            await harness.run_case(
                PlannerSmokeCase("two", "synthetic two"),
                snapshot=snapshot,
                capabilities=CAPS,
            )
        assert len(harness.results) == 1
        assert harness.attempted_calls == 2
        assert harness.completed_calls == 1
        with pytest.raises(RuntimeError, match="indeterminate"):
            await harness.run_case(
                PlannerSmokeCase("three", "synthetic three"),
                snapshot=snapshot,
                capabilities=CAPS,
            )

    asyncio.run(scenario())


def test_pydantic_context_update_uses_model_copy() -> None:
    original = _snapshot()
    updated = PlannerSmokeHarness.update_selected_entity(
        original, stable_id="stable-card", display_name="Облепиха"
    )
    assert updated.last_successful_entity_id == "stable-card"
    assert updated.last_successful_entity_name == "Облепиха"
    assert updated.candidate_entity_ids == ()
    assert original.candidate_entity_ids == ("stable-card",)


def test_application_adapter_can_be_forced_to_zero_retries() -> None:
    class Transport:
        def __init__(self) -> None:
            self.calls = 0

        async def post_json(self, *_args, **_kwargs):
            self.calls += 1
            return HttpResponse(429, b"")

    transport = Transport()
    provider = GeminiPlannerProvider(
        transport,
        api_key="test-secret",
        model="test-model",
        total_timeout_seconds=1,
    )
    client = MultiProviderPlanner(provider, mode="ai_first")
    harness = PlannerSmokeHarness(client, live_authorized=True)
    with pytest.raises(ProviderError):
        asyncio.run(
            harness.run_case(
                PlannerSmokeCase("one", "synthetic"),
                snapshot=_snapshot(),
                capabilities=CAPS,
            )
        )
    assert transport.calls == 1
    assert harness.attempted_calls == 1
    assert harness.completed_calls == 0


def test_offline_contract_cases_cover_required_planner_language_groups() -> None:
    names = {name for name, _ in OFFLINE_PLANNER_CONTRACT_CASES}
    assert names == {
        "catalog_search",
        "catalog_followup",
        "catalog_correction",
        "catalog_add_ingredient",
        "warehouse_writeoff",
        "warehouse_information",
        "attendance_read",
        "report_generation",
        "cancellation",
        "confirmation",
    }
