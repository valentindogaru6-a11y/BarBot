import asyncio
from dataclasses import asdict

import pytest

from app.models.assistant import AssistantDomain
from app.models.assistant_planner import (
    AssistantDialogueAct,
    PlannerCapability,
    PlannerContextSnapshot,
    PlannerDecision,
)
from app.services.assistant import (
    AssistantOrchestrator,
    AssistantRequestCoordinator,
    ProviderError,
    ProviderErrorKind,
    ShadowLocalDecision,
    ShadowPlannerSupervisor,
)


CAPABILITY = PlannerCapability(
    capability_id="catalog.search",
    domain="catalog",
    read_only=True,
    mutation=False,
)


def _snapshot() -> PlannerContextSnapshot:
    return PlannerContextSnapshot(
        actor_role="super_admin",
        available_capability_ids=("catalog.search",),
        last_successful_entity_id="stable-card",
    )


def _decision(**updates) -> PlannerDecision:
    data = {
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
    data.update(updates)
    return PlannerDecision.model_validate(data)


def _local(**updates) -> ShadowLocalDecision:
    data = {
        "route_category": "deterministic_local",
        "dialogue_act": AssistantDialogueAct.NEW_COMMAND,
        "domain": AssistantDomain.CATALOG,
        "capability_id": "catalog.search",
        "target_entity_id": "stable-card",
        "mutation": False,
        "needs_clarification": False,
        "validation_passed": True,
    }
    data.update(updates)
    return ShadowLocalDecision(**data)


class Sink:
    def __init__(self, *, fail: bool = False) -> None:
        self.events = []
        self.fail = fail

    def emit(self, event) -> None:
        if self.fail:
            raise RuntimeError("sink unavailable")
        self.events.append(event)


class BlockingPlanner:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def plan(self, *_args, **_kwargs):
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return _decision()


class NeverIntent:
    async def classify(self, *_args, **_kwargs):
        raise AssertionError("legacy provider must not be called")


def test_primary_does_not_wait_and_claim_releases_while_shadow_runs() -> None:
    async def scenario() -> None:
        sink = Sink()
        planner = BlockingPlanner()
        supervisor = ShadowPlannerSupervisor(sink=sink)
        orchestrator = AssistantOrchestrator(
            NeverIntent(),
            planner_mode="shadow",
            planner_client=planner,
            shadow_supervisor=supervisor,
        )
        coordinator = AssistantRequestCoordinator()
        async with coordinator.claim(10) as acquired:
            assert acquired
            result = await orchestrator.interpret(
                "покажи техкарту Облепиха",
                planner_snapshot=_snapshot(),
                planner_capabilities=(CAPABILITY,),
                shadow_key=10,
            )
            assert result.query == "Облепиха"
        await planner.started.wait()
        assert supervisor.active_count == 1
        async with coordinator.claim(10) as acquired_again:
            assert acquired_again
        planner.release.set()
        await supervisor.drain()
        assert sink.events[0].planner_outcome == "success"

    asyncio.run(scenario())

def test_duplicate_user_is_dropped_but_second_user_is_independent() -> None:
    async def scenario() -> None:
        sink = Sink()
        release = asyncio.Event()
        starts = [asyncio.Event(), asyncio.Event()]
        calls = 0

        async def plan():
            nonlocal calls
            index = calls
            calls += 1
            starts[index].set()
            await release.wait()
            return _decision()

        supervisor = ShadowPlannerSupervisor(max_concurrency=2, sink=sink)
        common = dict(
            planner_call=plan,
            local=_local(),
            snapshot=_snapshot(),
            capabilities=(CAPABILITY,),
            request_size=100,
        )
        assert supervisor.submit(user_key=1, **common)
        await starts[0].wait()
        assert not supervisor.submit(user_key=1, **common)
        assert supervisor.submit(user_key=2, **common)
        await starts[1].wait()
        assert calls == 2
        assert any(event.provider_error_category == "busy" for event in sink.events)
        release.set()
        await supervisor.drain()

    asyncio.run(scenario())


def test_never_ending_shadow_times_out_without_side_effects() -> None:
    async def scenario() -> None:
        sink = Sink()
        pending = asyncio.Event()
        context = _snapshot()

        async def plan():
            await pending.wait()
            return _decision()

        supervisor = ShadowPlannerSupervisor(timeout_seconds=0.01, sink=sink)
        assert supervisor.submit(
            user_key=1,
            planner_call=plan,
            local=_local(),
            snapshot=context,
            capabilities=(CAPABILITY,),
            request_size=100,
        )
        await supervisor.drain()
        assert context == _snapshot()
        assert sink.events[0].planner_outcome == "error"
        assert sink.events[0].provider_error_category == "timeout"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (ProviderError("gemini", ProviderErrorKind.MALFORMED_RESPONSE), "malformed_response"),
        (ProviderError("gemini", ProviderErrorKind.RATE_LIMIT), "rate_limit"),
        (ProviderError("gemini", ProviderErrorKind.SERVER), "server"),
    ],
)
def test_provider_errors_are_consumed_and_normalized(error, category) -> None:
    async def scenario() -> None:
        sink = Sink()

        async def fail():
            raise error

        supervisor = ShadowPlannerSupervisor(sink=sink)
        supervisor.submit(
            user_key=1,
            planner_call=fail,
            local=_local(),
            snapshot=_snapshot(),
            capabilities=(CAPABILITY,),
            request_size=100,
        )
        await supervisor.drain()
        assert sink.events[0].provider_error_category == category
        assert supervisor.active_count == 0

    asyncio.run(scenario())

def test_shutdown_cancels_and_cleans_up_shadow() -> None:
    async def scenario() -> None:
        sink = Sink()
        started = asyncio.Event()

        async def plan():
            started.set()
            await asyncio.Event().wait()

        supervisor = ShadowPlannerSupervisor(sink=sink)
        supervisor.submit(
            user_key=1,
            planner_call=plan,
            local=_local(),
            snapshot=_snapshot(),
            capabilities=(CAPABILITY,),
            request_size=100,
        )
        await started.wait()
        await supervisor.shutdown()
        assert supervisor.active_count == 0
        assert sink.events[0].planner_outcome == "cancelled"

    asyncio.run(scenario())


def test_telemetry_is_complete_private_and_calculates_disagreement() -> None:
    async def scenario() -> None:
        sink = Sink()

        async def plan():
            return _decision(dialogue_act="follow_up", target_entity_id=None)

        supervisor = ShadowPlannerSupervisor(sink=sink)
        supervisor.submit(
            user_key=999999,
            planner_call=plan,
            local=_local(),
            snapshot=_snapshot(),
            capabilities=(CAPABILITY,),
            request_size=2500,
        )
        await supervisor.drain()
        payload = asdict(sink.events[0])
        assert payload["event_version"] == 1
        assert payload["dialogue_act_match"] is False
        assert payload["target_match"] is False
        assert payload["overall_disagreement"] is True
        assert payload["payload_size_bucket"] == "lt_8kb"
        serialized = str(payload)
        assert "999999" not in serialized
        assert "raw query" not in serialized
        assert set(payload) == {
            "event_version", "correlation_id", "planner_mode",
            "local_route_category", "local_dialogue_act", "local_domain",
            "local_capability_id", "local_target_stable_id", "planner_outcome",
            "planner_dialogue_act", "planner_domain", "planner_capability_id",
            "planner_target_stable_id", "target_match", "dialogue_act_match",
            "domain_match", "capability_match", "overall_disagreement",
            "local_mutation", "local_read_only", "mutation", "read_only",
            "needs_clarification",
            "provider_latency_ms", "provider_error_category",
            "local_validation_passed", "payload_size_bucket",
            "allowed_stable_id_validation", "timestamp",
        }

    asyncio.run(scenario())


def test_telemetry_sink_failure_is_isolated() -> None:
    async def scenario() -> None:
        supervisor = ShadowPlannerSupervisor(sink=Sink(fail=True))

        async def plan():
            return _decision()

        assert supervisor.submit(
            user_key=1,
            planner_call=plan,
            local=_local(),
            snapshot=_snapshot(),
            capabilities=(CAPABILITY,),
            request_size=100,
        )
        await supervisor.drain()
        assert supervisor.active_count == 0

    asyncio.run(scenario())
