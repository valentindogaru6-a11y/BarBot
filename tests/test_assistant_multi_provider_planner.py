import asyncio
import json

import aiohttp
import pytest
from pydantic import ValidationError

from app.models.assistant_planner import PlannerCapability, PlannerContextSnapshot
from app.models.assistant_provider_planner import (
    GeminiPlannerEnvelope,
    GroqPlannerEnvelope,
    PlannerValidationStatus,
    ProviderPlanError,
    ProviderPlanResult,
    SafeProviderErrorCode,
    SafeProviderErrorType,
)
from app.services.assistant.planner_providers import (
    CircuitState,
    GroqPlannerProvider,
    MultiProviderPlanner,
    PlannerCircuitBreaker,
    convert_gemini_envelope,
    convert_groq_envelope,
    planner_wire_schema,
)
from app.services.assistant.providers import HttpResponse, ProviderError


CAPS = (
    PlannerCapability(
        capability_id="catalog.search",
        domain="catalog",
        read_only=True,
        mutation=False,
        required_arguments=("query",),
        allowed_arguments=("query",),
    ),
    PlannerCapability(
        capability_id="catalog.manage",
        domain="catalog",
        read_only=False,
        mutation=True,
        allowed_arguments=("reason",),
    ),
)


def snapshot(**updates):
    data = {
        "actor_role": "super_admin",
        "available_capability_ids": tuple(cap.capability_id for cap in CAPS),
        "last_successful_entity_id": "stable-oblepikha",
        "candidate_entity_ids": ("stable-candidate",),
    }
    data.update(updates)
    return PlannerContextSnapshot.model_validate(data)


def envelope(**updates):
    data = {
        "dialogue_act": "new_command",
        "domain": "catalog",
        "capability_id": "catalog.search",
        "target_entity_id": "stable-oblepikha",
        "confidence_scaled": 9000,
        "needs_clarification": False,
        "clarification_question": None,
        "inferred_action": "search",
        "argument_items": [{"name": "query", "value": "облепиха"}],
        "referenced_candidate_ids": [],
        "provider_notes_code": None,
    }
    data.update(updates)
    return data


def decision():
    return convert_groq_envelope(
        GroqPlannerEnvelope.model_validate(envelope()),
        snapshot=snapshot(),
        capabilities=CAPS,
    )


def walk_objects(node):
    if isinstance(node, dict):
        if node.get("type") == "object":
            yield node
        for value in node.values():
            yield from walk_objects(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk_objects(value)


def test_groq_wire_schema_is_flat_closed_required_and_runtime_filtered():
    schema = planner_wire_schema(CAPS)
    serialized = json.dumps(schema)
    assert "$defs" not in serialized and "$ref" not in serialized
    assert "default" not in serialized
    assert all(obj.get("additionalProperties") is False for obj in walk_objects(schema))
    assert set(schema["required"]) == set(schema["properties"])
    item = schema["properties"]["argument_items"]["items"]
    assert set(item["required"]) == {"name", "value"}
    assert schema["properties"]["capability_id"]["enum"] == [
        "catalog.search", "catalog.manage", None
    ]
    for field in ("capability_id", "target_entity_id", "clarification_question", "provider_notes_code"):
        assert "null" in schema["properties"][field]["type"]


def test_valid_provider_envelopes_converge_on_canonical_decision():
    groq = convert_groq_envelope(
        GroqPlannerEnvelope.model_validate(envelope()), snapshot=snapshot(), capabilities=CAPS
    )
    gemini = convert_gemini_envelope(
        GeminiPlannerEnvelope.model_validate(envelope()), snapshot=snapshot(), capabilities=CAPS
    )
    assert groq == gemini
    assert groq.read_only is True and groq.mutation is False
    assert groq.arguments == {"query": "облепиха"}
    assert groq.context_updates.model_dump(exclude_none=True) == {"candidate_entity_ids": ()}


def test_stable_target_reference_is_derived_locally_for_mutation():
    wire = GroqPlannerEnvelope.model_validate(
        envelope(
            dialogue_act="correction",
            capability_id="catalog.manage",
            inferred_action="manage",
            argument_items=[{"name": "reason", "value": "изменить состав"}],
            needs_clarification=True,
            clarification_question="Что именно изменить?",
        )
    )
    converted = convert_groq_envelope(wire, snapshot=snapshot(), capabilities=CAPS)
    assert converted.arguments == {
        "reason": "изменить состав",
        "target_ref": "stable-oblepikha",
    }
    assert converted.mutation is True and converted.read_only is False


@pytest.mark.parametrize(
    "updates,match",
    [
        ({"capability_id": "unknown"}, "unavailable capability"),
        ({"domain": "warehouse"}, "domain disagrees"),
        ({"target_entity_id": "invented"}, "invented"),
        ({"referenced_candidate_ids": ["invented"]}, "unavailable candidate"),
        ({"argument_items": [{"name": "query", "value": "a"}, {"name": "query", "value": "b"}]}, "repeated"),
        ({"argument_items": [{"name": "reason", "value": "x"}]}, "unknown argument"),
        ({"inferred_action": "manage"}, "disagrees"),
    ],
)
def test_converter_rejects_untrusted_capability_target_arguments_and_action(updates, match):
    wire = GroqPlannerEnvelope.model_validate(envelope(**updates))
    with pytest.raises(ValueError, match=match):
        convert_groq_envelope(wire, snapshot=snapshot(), capabilities=CAPS)


def test_wire_rejects_oversized_extra_authority_and_null_inconsistency():
    with pytest.raises(ValidationError):
        GroqPlannerEnvelope.model_validate(
            envelope(argument_items=[{"name": "query", "value": "x" * 1001}])
        )
    for authority in ("mutation", "read_only", "confirmed", "role", "permissions", "revision", "actor", "chat_id"):
        with pytest.raises(ValidationError):
            GroqPlannerEnvelope.model_validate({**envelope(), authority: True})
    with pytest.raises(ValueError, match="clarification"):
        convert_groq_envelope(
            GroqPlannerEnvelope.model_validate(
                envelope(needs_clarification=True, clarification_question=None)
            ),
            snapshot=snapshot(),
            capabilities=CAPS,
        )


class Transport:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def post_json(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def test_groq_transport_uses_strict_wire_schema_bounded_timeouts_and_no_tools():
    body = json.dumps({"choices": [{"message": {"content": json.dumps(envelope())}}]}).encode()
    transport = Transport(HttpResponse(200, body))
    provider = GroqPlannerProvider(transport, api_key="secret")
    result = asyncio.run(provider.plan(snapshot(), "synthetic", CAPS))
    assert result.validation_status is PlannerValidationStatus.PASSED
    _, call = transport.calls[0]
    assert call["connect_timeout_seconds"] == 3
    assert call["read_timeout_seconds"] == 5
    assert call["timeout_seconds"] == 8
    payload = call["payload"]
    assert payload["response_format"]["json_schema"]["strict"] is True
    assert payload["response_format"]["json_schema"]["name"] == "barbot_groq_planner_v1"
    assert "tools" not in payload
    assert "secret" not in json.dumps(payload)


def test_groq_schema_is_invariant_across_context_and_stable_ids_stay_out_of_schema():
    body = json.dumps({"choices": [{"message": {"content": json.dumps(envelope())}}]}).encode()
    transport = Transport(HttpResponse(200, body))
    provider = GroqPlannerProvider(transport, api_key="secret")

    async def scenario():
        await provider.plan(
            snapshot(last_successful_entity_id=None), "first", CAPS
        )
        await provider.plan(
            snapshot(
                last_successful_entity_id="stable-oblepikha",
                candidate_entity_ids=(),
                last_tool_operation="catalog.get",
                last_tool_summary="selected tracked card",
                revision=2,
            ),
            "second",
            CAPS,
        )

    asyncio.run(scenario())
    payloads = [call[1]["payload"] for call in transport.calls]
    schemas = [payload["response_format"]["json_schema"]["schema"] for payload in payloads]
    assert schemas[0] == schemas[1]
    assert "stable-oblepikha" not in json.dumps(schemas)
    assert schemas[0]["properties"]["target_entity_id"] == {
        "type": ["string", "null"],
        "maxLength": 300,
    }


def test_groq_diagnostics_prove_same_schema_hash_for_different_contexts():
    body = _error_body(code="json_validate_failed", param="response_format")
    transport = Transport(HttpResponse(400, body))
    provider = GroqPlannerProvider(transport, api_key="secret")

    async def scenario():
        first = await provider.plan(
            snapshot(last_successful_entity_id=None), "first", CAPS
        )
        second = await provider.plan(
            snapshot(
                last_successful_entity_id="stable-oblepikha",
                candidate_entity_ids=(),
                last_tool_operation="catalog.get",
                last_tool_summary="selected tracked card",
                revision=2,
            ),
            "second",
            CAPS,
        )
        return first, second

    first, second = asyncio.run(scenario())
    assert first.diagnostic is not None and second.diagnostic is not None
    assert first.diagnostic.schema_hash == second.diagnostic.schema_hash
    assert first.diagnostic.request_hash != second.diagnostic.request_hash


def _error_body(*, error_type="invalid_request_error", code=None, param=None, message="hidden"):
    return json.dumps(
        {"error": {"type": error_type, "code": code, "param": param, "message": message}}
    ).encode()


@pytest.mark.parametrize(
    "status,body,expected,retryable",
    [
        (400, _error_body(code="json_schema_invalid", param="json_schema"), ProviderPlanError.INVALID_SCHEMA, False),
        (400, _error_body(code="json_validate_failed", param="response_format"), ProviderPlanError.GENERATED_OUTPUT_SCHEMA_MISMATCH, False),
        (400, _error_body(code="unsupported_parameter", param="reasoning_effort"), ProviderPlanError.UNSUPPORTED_PARAMETER, False),
        (400, _error_body(error_type="model_refusal", code="model_refusal"), ProviderPlanError.MODEL_REFUSAL, False),
        (400, _error_body(error_type="safety_refusal", code="safety_refusal"), ProviderPlanError.SAFETY_REFUSAL, False),
        (400, _error_body(error_type="novel", code="novel", param="secret"), ProviderPlanError.UNKNOWN_BAD_REQUEST, False),
        (401, _error_body(code="invalid_api_key"), ProviderPlanError.AUTH, False),
        (402, _error_body(code="insufficient_quota"), ProviderPlanError.QUOTA, False),
        (429, _error_body(code="rate_limit_exceeded"), ProviderPlanError.RATE_LIMIT, True),
        (503, _error_body(error_type="server_error", code="server_error"), ProviderPlanError.SERVER_ERROR, True),
    ],
)
def test_groq_error_envelope_classifies_only_safe_allowlisted_metadata(
    status, body, expected, retryable
):
    result = asyncio.run(
        GroqPlannerProvider(Transport(HttpResponse(status, body)), api_key="x").plan(
            snapshot(), "request-secret", CAPS
        )
    )
    assert result.error is expected
    assert result.diagnostic is not None
    assert result.diagnostic.category is expected
    assert result.diagnostic.retryable is retryable
    serialized = result.diagnostic.model_dump_json()
    assert "hidden" not in serialized
    assert "request-secret" not in serialized
    assert "secret" not in serialized
    if expected is ProviderPlanError.UNKNOWN_BAD_REQUEST:
        assert result.diagnostic.provider_error_type is SafeProviderErrorType.UNKNOWN
        assert result.diagnostic.provider_error_code is SafeProviderErrorCode.UNKNOWN
        assert result.diagnostic.parameter is None


def test_groq_malformed_extra_fields_are_normalized():
    malformed = json.dumps({"choices": [{"message": {"content": json.dumps({**envelope(), "confirmed": True})}}]}).encode()
    result = asyncio.run(GroqPlannerProvider(Transport(HttpResponse(200, malformed)), api_key="x").plan(snapshot(), "x", CAPS))
    assert result.error is ProviderPlanError.MALFORMED_RESPONSE


class RaisingTransport:
    def __init__(self, exception):
        self.exception = exception

    async def post_json(self, *_args, **_kwargs):
        raise self.exception


@pytest.mark.parametrize(
    "exception,expected",
    [
        (asyncio.TimeoutError(), ProviderPlanError.TIMEOUT),
        (aiohttp.ClientConnectionError(), ProviderPlanError.NETWORK),
    ],
)
def test_groq_transport_failures_have_safe_diagnostics(exception, expected):
    result = asyncio.run(
        GroqPlannerProvider(RaisingTransport(exception), api_key="x").plan(
            snapshot(), "synthetic", CAPS
        )
    )
    assert result.error is expected
    assert result.diagnostic is not None and result.diagnostic.retryable is True
    assert result.diagnostic.http_status is None


class FakeProvider:
    def __init__(self, name, results, *, delay=0):
        self.provider_name = name
        self.model = f"{name}-model"
        self.results = list(results)
        self.delay = delay
        self.calls = 0

    async def plan(self, _snapshot, _message, _caps):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.results.pop(0)


def outcome(provider, *, ok=True, error=ProviderPlanError.TIMEOUT):
    return ProviderPlanResult(
        provider=provider,
        model=f"{provider}-model",
        decision=decision() if ok else None,
        error=None if ok else error,
        latency_ms=1,
        schema_mode="test",
        validation_status=(PlannerValidationStatus.PASSED if ok else PlannerValidationStatus.PROVIDER_ERROR),
    )


def test_primary_success_does_not_call_fallback():
    primary = FakeProvider("groq", [outcome("groq")])
    fallback = FakeProvider("gemini", [outcome("gemini")])
    result = asyncio.run(MultiProviderPlanner(primary, fallback, mode="ai_first").plan("x", snapshot=snapshot(), capabilities=CAPS))
    assert result.capability_id == "catalog.search"
    assert (primary.calls, fallback.calls) == (1, 0)


@pytest.mark.parametrize("error", [ProviderPlanError.TIMEOUT, ProviderPlanError.GENERATED_OUTPUT_SCHEMA_MISMATCH, ProviderPlanError.RATE_LIMIT, ProviderPlanError.SERVER_ERROR])
def test_transient_primary_failure_uses_one_bounded_fallback(error):
    primary = FakeProvider("groq", [outcome("groq", ok=False, error=error)])
    fallback = FakeProvider("gemini", [outcome("gemini")])
    asyncio.run(MultiProviderPlanner(primary, fallback, mode="ai_first").plan("x", snapshot=snapshot(), capabilities=CAPS))
    assert (primary.calls, fallback.calls) == (1, 1)


@pytest.mark.parametrize(
    "non_retryable",
    [ProviderPlanError.AUTH, ProviderPlanError.QUOTA, ProviderPlanError.CONFIG],
)
def test_auth_quota_config_error_does_not_fallback_and_both_fail_safely(non_retryable):
    primary = FakeProvider("groq", [outcome("groq", ok=False, error=non_retryable)])
    fallback = FakeProvider("gemini", [outcome("gemini")])
    with pytest.raises(ProviderError):
        asyncio.run(MultiProviderPlanner(primary, fallback, mode="ai_first").plan("x", snapshot=snapshot(), capabilities=CAPS))
    assert fallback.calls == 0
    both = MultiProviderPlanner(
        FakeProvider("groq", [outcome("groq", ok=False)]),
        FakeProvider("gemini", [outcome("gemini", ok=False)]),
        mode="ai_first",
    )
    with pytest.raises(ProviderError):
        asyncio.run(both.plan("x", snapshot=snapshot(), capabilities=CAPS))


def test_shadow_never_calls_fallback_by_default():
    primary = FakeProvider("groq", [outcome("groq", ok=False)])
    fallback = FakeProvider("gemini", [outcome("gemini")])
    with pytest.raises(ProviderError):
        asyncio.run(MultiProviderPlanner(primary, fallback, mode="shadow").plan("x", snapshot=snapshot(), capabilities=CAPS))
    assert fallback.calls == 0


@pytest.mark.parametrize(
    "error",
    [
        ProviderPlanError.INVALID_SCHEMA,
        ProviderPlanError.UNSUPPORTED_PARAMETER,
        ProviderPlanError.UNKNOWN_BAD_REQUEST,
        ProviderPlanError.SAFETY_REFUSAL,
    ],
)
def test_deterministic_bad_requests_neither_fallback_nor_open_circuit(error):
    circuit = PlannerCircuitBreaker(failure_threshold=1, cooldown_seconds=30)
    primary = FakeProvider(
        "groq",
        [outcome("groq", ok=False, error=error), outcome("groq", ok=False, error=error)],
    )
    fallback = FakeProvider("gemini", [outcome("gemini"), outcome("gemini")])
    planner = MultiProviderPlanner(
        primary,
        fallback,
        mode="ai_first",
        circuit_breaker=circuit,
    )
    for _ in range(2):
        with pytest.raises(ProviderError):
            asyncio.run(planner.plan("x", snapshot=snapshot(), capabilities=CAPS))
    assert primary.calls == 2
    assert fallback.calls == 0
    assert circuit.state("groq") is CircuitState.CLOSED


def test_generated_output_mismatch_can_fallback_without_opening_primary_circuit():
    circuit = PlannerCircuitBreaker(failure_threshold=1, cooldown_seconds=30)
    primary = FakeProvider(
        "groq",
        [outcome("groq", ok=False, error=ProviderPlanError.GENERATED_OUTPUT_SCHEMA_MISMATCH)],
    )
    fallback = FakeProvider("gemini", [outcome("gemini")])
    result = asyncio.run(
        MultiProviderPlanner(
            primary,
            fallback,
            mode="ai_first",
            circuit_breaker=circuit,
        ).plan("x", snapshot=snapshot(), capabilities=CAPS)
    )
    assert result.capability_id == "catalog.search"
    assert circuit.state("groq") is CircuitState.CLOSED


def test_total_deadline_and_cancellation_are_bounded_and_propagated():
    slow = FakeProvider("groq", [outcome("groq")], delay=0.05)
    with pytest.raises(ProviderError):
        asyncio.run(MultiProviderPlanner(slow, mode="ai_first", total_deadline_seconds=0.01).plan("x", snapshot=snapshot(), capabilities=CAPS))

    async def cancelled():
        provider = FakeProvider("groq", [outcome("groq")], delay=10)
        task = asyncio.create_task(MultiProviderPlanner(provider, mode="ai_first").plan("x", snapshot=snapshot(), capabilities=CAPS))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancelled())


def test_circuit_opens_skips_provider_and_half_open_success_recovers():
    now = [0.0]
    circuit = PlannerCircuitBreaker(failure_threshold=2, cooldown_seconds=5, clock=lambda: now[0])
    circuit.failure("groq", transient=True)
    circuit.failure("groq", transient=True)
    assert circuit.state("groq") is CircuitState.OPEN
    assert circuit.acquire("groq") is False
    now[0] = 6
    assert circuit.state("groq") is CircuitState.HALF_OPEN
    assert circuit.acquire("groq") is True
    assert circuit.acquire("groq") is False
    circuit.success("groq")
    assert circuit.state("groq") is CircuitState.CLOSED


def test_open_primary_circuit_skips_call_and_uses_fallback():
    circuit = PlannerCircuitBreaker(failure_threshold=1, cooldown_seconds=30)
    circuit.failure("groq", transient=True)
    primary = FakeProvider("groq", [outcome("groq")])
    fallback = FakeProvider("gemini", [outcome("gemini")])
    result = asyncio.run(
        MultiProviderPlanner(
            primary,
            fallback,
            mode="ai_first",
            circuit_breaker=circuit,
        ).plan("x", snapshot=snapshot(), capabilities=CAPS)
    )
    assert result.capability_id == "catalog.search"
    assert (primary.calls, fallback.calls) == (0, 1)


def test_mutation_plan_fallback_never_executes_or_confirms_anything():
    mutation_wire = GroqPlannerEnvelope.model_validate(
        envelope(
            dialogue_act="follow_up",
            capability_id="catalog.manage",
            inferred_action="manage",
            argument_items=[],
            needs_clarification=True,
            clarification_question="Что изменить?",
        )
    )
    mutation_decision = convert_groq_envelope(
        mutation_wire, snapshot=snapshot(), capabilities=CAPS
    )
    failed = FakeProvider("groq", [outcome("groq", ok=False)])
    fallback_result = ProviderPlanResult(
        provider="gemini",
        model="gemini-model",
        decision=mutation_decision,
        latency_ms=1,
        schema_mode="test",
        validation_status=PlannerValidationStatus.PASSED,
    )
    fallback = FakeProvider("gemini", [fallback_result])
    result = asyncio.run(
        MultiProviderPlanner(failed, fallback, mode="ai_first").plan(
            "synthetic", snapshot=snapshot(), capabilities=CAPS
        )
    )
    assert result.mutation is True and result.needs_clarification is True
    assert not hasattr(result, "confirmed")
    assert (failed.calls, fallback.calls) == (1, 1)


def test_concurrent_users_share_no_request_or_decision_state():
    provider = FakeProvider("groq", [outcome("groq"), outcome("groq")])
    planner = MultiProviderPlanner(provider, mode="ai_first")

    async def scenario():
        return await asyncio.gather(
            planner.plan("first", snapshot=snapshot(revision=1), capabilities=CAPS),
            planner.plan("second", snapshot=snapshot(revision=2), capabilities=CAPS),
        )

    results = asyncio.run(scenario())
    assert len(results) == 2 and provider.calls == 2
