"""Offline Gemini-first chain, admission and exact physical-attempt evidence."""

import asyncio
import json
from decimal import Decimal

import aiohttp
import pytest
from pydantic import ValidationError

from app.bot import build_assistant_clients
from app.config import Settings
from app.models.assistant_provider_planner import ProviderPlanError
from app.services.assistant.coordinator import AssistantRequestCoordinator
from app.services.assistant.planner_policy import (
    CURRENT_REQUEST, PlannerAttemptPolicy, PlannerBusy, mark_tool_boundary, planner_request,
)
from app.services.assistant.planner_providers import (
    GeminiPlannerProvider, GroqPlannerProvider, MultiProviderPlanner,
)
from app.services.assistant.providers import HttpResponse, ProviderError, ProviderErrorKind
from app.services.assistant.evaluation_equivalence import quantity_equivalent, informational_read_only
from tests.test_assistant_multi_provider_planner import CAPS, snapshot, route


def settings(**overrides):
    return Settings(_env_file=None, **dict(dict(bot_token='test', ai_assistant_enabled=True,
        ai_access_mode='role_based', ai_text_enabled=True, gemini_api_key='synthetic'), **overrides))


class Wire:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def post_json(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0) if self.responses else route()
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            return await response()
        if isinstance(response, HttpResponse):
            return response
        raw = json.dumps(response)
        body = ({'candidates': [{'content': {'parts': [{'text': raw}]}}]} if 'googleapis' in url
                else {'choices': [{'message': {'content': raw}}]})
        return HttpResponse(200, json.dumps(body).encode())


def chain(wire, *, mode='ai_first', fallback=True, clock=None, interval=0):
    events = []
    policy = PlannerAttemptPolicy(intervals={'gemini': interval, 'groq': interval},
        clock=clock or __import__('time').monotonic, sink=events.append)
    primary = GeminiPlannerProvider(wire, api_key='synthetic', model='synthetic')
    reserve = GroqPlannerProvider(wire, api_key='synthetic', model='synthetic') if fallback else None
    return MultiProviderPlanner(primary, reserve, mode=mode, attempt_policy=policy), policy, events


def test_defaults_and_disabled_fallback_matrix():
    config = settings(groq_api_key=None, groq_planner_fallback_model=None)
    orchestrator, speech = build_assistant_clients(config, transport=Wire())
    assert config.ai_planner_primary_provider == 'gemini'
    assert config.ai_planner_fallback_provider == 'groq'
    assert not config.ai_planner_fallback_enabled
    assert config.ai_planner_mode == 'local_first'
    assert isinstance(orchestrator.planner_client.primary, GeminiPlannerProvider)
    assert orchestrator.planner_client.primary.model == config.gemini_planner_model
    assert orchestrator.planner_client.fallback is None and speech is None


def test_enabled_fallback_and_independent_voice_configuration():
    config = settings(ai_planner_fallback_enabled=True, groq_api_key='synthetic', ai_voice_enabled=True)
    orchestrator, speech = build_assistant_clients(config, transport=Wire())
    assert isinstance(orchestrator.planner_client.fallback, GroqPlannerProvider)
    assert orchestrator.planner_client.fallback.model == 'openai/gpt-oss-120b'
    assert speech.model == config.groq_stt_model
    no_fallback, speech = build_assistant_clients(settings(ai_voice_enabled=True, groq_api_key='synthetic',
        groq_planner_fallback_model=None), transport=Wire())
    assert no_fallback.planner_client.fallback is None and speech is not None


@pytest.mark.parametrize('overrides', [
    {'ai_planner_primary_provider': 'unknown'}, {'ai_planner_fallback_provider': 'unknown'},
    {'ai_planner_fallback_provider': 'gemini'}, {'gemini_planner_model': ' '},
    {'ai_planner_fallback_enabled': True},
    {'ai_planner_fallback_enabled': True, 'groq_api_key': ' '},
    {'ai_planner_fallback_enabled': True, 'groq_api_key': 'synthetic', 'groq_planner_fallback_model': None},
    {'ai_planner_fallback_enabled': True, 'groq_api_key': 'synthetic', 'groq_planner_fallback_model': ' '},
    {'gemini_planner_min_interval_seconds': 0}, {'ai_request_global_concurrency': 9},
    {'ai_request_queue_policy': 'unbounded'}, {'ai_planner_physical_concurrency': 2},
    {'gemini_planner_total_seconds': 38},
])
def test_config_rejects_invalid_policy(overrides):
    with pytest.raises(ValidationError):
        settings(**overrides)


@pytest.mark.parametrize('response', [TimeoutError(), aiohttp.ClientConnectionError(),
    HttpResponse(429, b'{}'), HttpResponse(500, b'{}'), HttpResponse(503, b'{}')])
def test_availability_uses_exactly_one_groq_attempt(response):
    async def run():
        wire = Wire(response, route())
        planner, policy, events = chain(wire)
        decision = await planner.plan('найди облепиху', snapshot=snapshot(), capabilities=CAPS)
        assert decision.read_only and len(wire.calls) == 2
        assert len(events) == 2 and [e['alias'] for e in events] == ['primary', 'fallback']
        assert events[1]['physical_attempt_count'] == 2 and events[1]['fallback_reason']
        assert not policy.active
    asyncio.run(run())


@pytest.mark.parametrize('response', [HttpResponse(401, b'{}'), HttpResponse(403, b'{}'),
    HttpResponse(400, b'{}'), HttpResponse(200, b'{'), HttpResponse(404, b'{}'),
    route(domain='warehouse'), route(capability_id='shell.execute')])
def test_contract_auth_config_and_authority_never_fallback(response):
    async def run():
        wire = Wire(response)
        planner, _, events = chain(wire)
        with pytest.raises(ProviderError):
            await planner.plan('найди облепиху', snapshot=snapshot(), capabilities=CAPS)
        assert len(wire.calls) == len(events) == 1
    asyncio.run(run())


def test_primary_success_and_semantic_disagreement_do_not_fan_out():
    async def run():
        wire = Wire(route(capability_id='none', domain='general', action_code='explain'))
        planner, _, _ = chain(wire)
        result = await planner.plan('найди облепиху', snapshot=snapshot(), capabilities=CAPS)
        assert result.capability_id is None and len(wire.calls) == 1
    asyncio.run(run())


def test_groq_contract_failure_has_no_data_side_effects():
    from app.services.assistant.evaluation_environment import IsolatedEvaluationEnvironment
    async def run():
        with IsolatedEvaluationEnvironment() as env:
            before = env.business.snapshot()
            wire = Wire(TimeoutError(), HttpResponse(200, b'{'))
            planner, _, events = chain(wire)
            with pytest.raises(ProviderError) as exc:
                await planner.plan('спиши апельсин 200 грамм', snapshot=snapshot(), capabilities=CAPS)
            assert exc.value.kind is ProviderErrorKind.MALFORMED_RESPONSE
            assert before == env.business.snapshot()
            assert env.state()['operations'] == env.state()['proposals'] == 0
            assert len(wire.calls) == 2 and events[-1]['validation'] == 'wire_invalid'
    asyncio.run(run())


@pytest.mark.parametrize('boundary', ['tool', 'pending', 'tool_result'])
def test_fallback_forbidden_after_lifecycle_boundary(boundary):
    from app.models.assistant_planner import PlannerToolData
    async def run():
        snap = snapshot(**({'pending_action': 'catalog.manage'} if boundary == 'pending' else
            {'tool_results': (PlannerToolData(capability_id='catalog.get', summary='safe'),)} if boundary == 'tool_result' else {}))
        wire = Wire(TimeoutError())
        planner, _, _ = chain(wire)
        with planner_request():
            if boundary == 'tool': mark_tool_boundary()
            with pytest.raises(ProviderError):
                await planner.plan('найди облепиху', snapshot=snap, capabilities=CAPS)
        assert len(wire.calls) == 1
    asyncio.run(run())


def test_read_only_question_cannot_be_promoted_to_mutation():
    async def run():
        wire = Wire(TimeoutError(), route(capability_id='catalog.manage', action_code='edit', target_index=0))
        planner, _, _ = chain(wire)
        with pytest.raises(ValueError, match='local action evidence'):
            await planner.plan('что такое изменение состава?', snapshot=snapshot(), capabilities=CAPS)
        assert len(wire.calls) == 2
    asyncio.run(run())


def test_request_budget_spans_second_step_and_shared_deadline():
    async def run():
        wire = Wire(route(), route())
        planner, _, events = chain(wire)
        with planner_request() as state:
            await planner.plan('найди облепиху', snapshot=snapshot(), capabilities=CAPS)
            mark_tool_boundary()
            with pytest.raises(PlannerBusy):
                await planner.plan('найди облепиху', snapshot=snapshot(), capabilities=CAPS)
            assert state.attempts == 1 and state.deadline is not None
        assert len(wire.calls) == len(events) == 1
    asyncio.run(run())


def test_physical_pacing_shared_between_users_and_busy_never_fallback():
    async def run():
        now = [100.]
        wire = Wire(route(), route())
        planner, policy, events = chain(wire, clock=lambda: now[0], interval=10)
        coordinator = AssistantRequestCoordinator()
        for user, allowed in ((10, True), (20, False), (30, True)):
            if user == 30: now[0] += 10
            async with coordinator.claim(user) as acquired:
                assert acquired
                if allowed:
                    await planner.plan('найди облепиху', snapshot=snapshot(), capabilities=CAPS)
                else:
                    with pytest.raises(PlannerBusy):
                        await planner.plan('найди облепиху', snapshot=snapshot(), capabilities=CAPS)
        assert len(wire.calls) == len(events) == 2 and not policy.active
    asyncio.run(run())


@pytest.mark.parametrize('retry_after,expected', [(None, 90), (-50, 90), (1000000, 900),
    (float('nan'), 90), (float('inf'), 90), (120, 120)])
def test_429_cooldown_and_bounded_retry_after(retry_after, expected):
    async def run():
        now = [100.]
        wire = Wire(HttpResponse(429, b'{}', retry_after), route())
        planner, policy, events = chain(wire, fallback=False, clock=lambda: now[0])
        with pytest.raises(ProviderError):
            await planner.plan('найди облепиху', snapshot=snapshot(), capabilities=CAPS)
        assert policy.cooldown_until['gemini'] == 100 + expected
        now[0] += expected - 1
        with pytest.raises(PlannerBusy):
            await planner.plan('найди облепиху', snapshot=snapshot(), capabilities=CAPS)
        now[0] += 1
        await planner.plan('найди облепиху', snapshot=snapshot(), capabilities=CAPS)
        assert len(wire.calls) == len(events) == 2
    asyncio.run(run())


def test_admission_zero_queue_cooldown_local_bypass_and_duplicate_failure():
    async def run():
        now = [100.]
        c = AssistantRequestCoordinator(max_concurrency=1, user_cooldown_seconds=2, clock=lambda: now[0])
        async with c.claim(10, update_key=(10, 1)) as ok:
            assert ok
            async with c.claim(20) as rejected: assert not rejected
            async with c.claim(20, allow_local=True) as local:
                assert local and not CURRENT_REQUEST.get().provider_allowed
        async with c.claim(10) as rejected: assert not rejected
        async with c.claim(10, allow_local=True) as local: assert local
        now[0] += 2
        with pytest.raises(RuntimeError):
            async with c.claim(10, update_key=(10, 2)) as ok:
                assert ok
                raise RuntimeError('synthetic')
        now[0] += 2
        async with c.claim(10, update_key=(10, 2)) as duplicate: assert not duplicate
        assert not c._active_user_ids and c._provider_active == 0
    asyncio.run(run())


def test_shutdown_and_cancellation_release_all_permits():
    async def run():
        c = AssistantRequestCoordinator()
        started = asyncio.Event()
        wire_started = asyncio.Event()
        async def blocked():
            wire_started.set()
            await asyncio.Event().wait()
        wire = Wire(blocked, route())
        planner, policy, events = chain(wire)
        async def request():
            async with c.claim(10):
                started.set()
                await planner.plan('найди облепиху', snapshot=snapshot(), capabilities=CAPS)
        task = asyncio.create_task(request())
        await started.wait(); await wire_started.wait()
        await c.shutdown()
        assert task.cancelled() and not c._active_user_ids and c._provider_active == 0
        assert not policy.active and events[0]['category'] == 'cancelled'
        async with c.claim(20) as allowed: assert not allowed
    asyncio.run(run())


def test_shadow_no_fallback_stage_two_or_effects():
    async def run():
        wire = Wire(TimeoutError(), route())
        planner, _, events = chain(wire, mode='shadow')
        with pytest.raises(ProviderError):
            await planner.plan('измени состав', snapshot=snapshot(), capabilities=CAPS)
        assert len(wire.calls) == len(events) == 1
    asyncio.run(run())


def test_timeout_policy_and_trace_privacy():
    async def run():
        events = []
        wire = Wire(route())
        orchestrator, _ = build_assistant_clients(settings(ai_planner_mode='ai_first'), transport=wire)
        planner = orchestrator.planner_client
        planner.attempt_policy.sink = events.append
        await planner.plan('найди облепиху', snapshot=snapshot(), capabilities=CAPS)
        kwargs = wire.calls[0][1]
        assert (kwargs['connect_timeout_seconds'], kwargs['read_timeout_seconds'], kwargs['timeout_seconds']) == (3, 4, 5)
        text = json.dumps(events)
        for forbidden in ('облепих', 'synthetic', 'Authorization', 'user_id', 'chat_id', 'reasoning'):
            assert forbidden not in text
        assert events[0]['validation'] == 'passed' and len(events[0]['correlation']) == 32
    asyncio.run(run())


@pytest.mark.parametrize('alias', ['g', 'gram', 'grams', 'г', 'гр', 'грамм', 'грамма', 'граммов'])
def test_bakeoff_D_gram_aliases(alias):
    from app.services.assistant.planner_arguments import local_stage_two_arguments
    args = local_stage_two_arguments('warehouse.prepare_writeoff', 'спиши апельсин 200 грамм')
    assert args['product'] == 'апельсин'
    assert quantity_equivalent(args['quantity'], args['unit'], '200', alias)
    assert quantity_equivalent('0.2', 'кг', '200', alias)
    assert not quantity_equivalent('200', 'мл', '200', alias)
    assert not quantity_equivalent('NaN', alias, '200', alias)
    assert not quantity_equivalent(.2, 'кг', '200', alias)


@pytest.mark.parametrize('domain', ['general', 'warehouse'])
def test_bakeoff_E_informational_domain_equivalence(domain):
    actual = dict(domain=domain, capability=None, mutation=False, read_only=True,
        preview=False, safety=True, operations=0)
    assert informational_read_only(actual)
    for changes in ({'mutation': True}, {'preview': True}, {'operations': 1},
        {'capability': 'warehouse.prepare_writeoff'}, {'draft_delta': 1}, {'proposal_delta': 1},
        {'unexpected_state_change_count': 1}):
        assert not informational_read_only({**actual, **changes})


def test_overlapping_physical_attempt_rejected_without_losing_trace():
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        async def block():
            started.set()
            await release.wait()
            raw = json.dumps(route())
            return HttpResponse(200, json.dumps({'candidates': [{'content': {'parts': [{'text': raw}]}}]}).encode())
        wire = Wire(block)
        planner, policy, events = chain(wire)
        first = asyncio.create_task(planner.plan('найди облепиху', snapshot=snapshot(), capabilities=CAPS))
        await started.wait()
        with pytest.raises(PlannerBusy):
            await planner.plan('найди облепиху', snapshot=snapshot(), capabilities=CAPS)
        assert len(wire.calls) == 1 and policy.active
        release.set()
        await first
        assert len(events) == 1 and events[0]['validation'] == 'passed' and not policy.active
    asyncio.run(run())


def test_open_circuit_uses_one_fallback_not_a_primary_http_attempt():
    async def run():
        from app.services.assistant.planner_providers import PlannerCircuitBreaker
        wire = Wire(route())
        planner, _, events = chain(wire)
        planner.circuit = PlannerCircuitBreaker(failure_threshold=1)
        planner.circuit.failure('gemini', transient=True)
        result = await planner.plan('найди облепиху', snapshot=snapshot(), capabilities=CAPS)
        assert result.read_only and len(wire.calls) == 1
        assert events[0]['alias'] == 'fallback' and events[0]['ordinal'] == 1
        assert events[0]['fallback_reason'] == 'circuit_open'
    asyncio.run(run())


def test_actual_two_step_loop_retains_read_with_no_extra_http_or_fallback():
    from types import SimpleNamespace
    from app.services.assistant.conversation_brain import ConversationToolLoop
    async def run():
        wire = Wire(route())
        planner, _, events = chain(wire)
        async def dispatch(*args):
            return SimpleNamespace(message='Safe local read')
        completed, question = await ConversationToolLoop().run_iterative(
            text='найди облепиху', snapshot=snapshot(), capabilities=CAPS,
            plan=planner.plan, dispatch=dispatch)
        assert len(completed) == 1 and question is None
        assert len(wire.calls) == len(events) == 1
    asyncio.run(run())


def test_local_only_claim_cannot_escalate_to_either_provider():
    async def run():
        c = AssistantRequestCoordinator(max_concurrency=1)
        wire = Wire()
        planner, _, _ = chain(wire)
        async with c.claim(10):
            async with c.claim(20, allow_local=True) as local:
                assert local
                with pytest.raises(PlannerBusy):
                    await planner.plan('найди облепиху', snapshot=snapshot(), capabilities=CAPS)
        assert not wire.calls
    asyncio.run(run())


def test_chain_deadline_does_not_start_a_late_fallback():
    async def run():
        async def delay():
            await asyncio.sleep(1)
        wire = Wire(delay)
        planner, policy, events = chain(wire)
        planner.total_deadline_seconds = .005
        with pytest.raises(ProviderError):
            await planner.plan('найди облепиху', snapshot=snapshot(), capabilities=CAPS)
        assert len(wire.calls) == 1 and not policy.active
        assert len(events) == 1 and events[0]['category'] == 'deadline'
    asyncio.run(run())


def test_ai_first_chain_failure_never_calls_legacy_gemini():
    from app.services.assistant.orchestrator import AssistantOrchestrator
    class Planner:
        async def plan(self, *args, **kwargs):
            raise ProviderError('gemini', ProviderErrorKind.UNAVAILABLE)
    class Forbidden:
        async def classify(self, *args, **kwargs):
            raise AssertionError('hidden extra provider attempt')
    async def run():
        orchestrator = AssistantOrchestrator(Forbidden(), planner_mode='ai_first', planner_client=Planner())
        result = await orchestrator.interpret('неопределённый вопрос', planner_snapshot=snapshot(), planner_capabilities=CAPS)
        assert result.intent.value == 'clarify'
    asyncio.run(run())


def test_equivalence_is_wired_into_scorer_only():
    from app.services.assistant.evaluation_corpus import LiveExpectation
    from app.services.assistant.live_evaluation import score_observation
    actual = dict(safety=True, operations=0, domain='warehouse', capability=None,
        dialogue_act='general_chat', target=None, clarification=False, preview=False,
        memory_active=0, restart=False, argument_ok=None, read_only=True, mutation=False,
        quantity_text='200', unit_text='грамм')
    expected = LiveExpectation(informational=True, quantity_text='200', unit_text='g')
    result = score_observation(actual, expected)
    assert result['route'] and result['argument_extraction']
    assert not score_observation({**actual, 'mutation': True}, expected)['route']
    assert not score_observation({**actual, 'unit_text': 'мл'}, expected)['argument_extraction']
    assert informational_read_only({**actual, 'operations': 7, 'operation_delta': 0})
    assert score_observation(actual, LiveExpectation(informational=True, domain='general'))['route']


def test_unknown_shadow_request_never_waits_for_foreground_gemini():
    from app.services.assistant.orchestrator import AssistantOrchestrator
    class Forbidden:
        async def classify(self, *a, **k):
            raise AssertionError('foreground Gemini called')
    class Slow:
        async def plan(self, *a, **k):
            await asyncio.Event().wait()
    async def run():
        orchestrator = AssistantOrchestrator(Forbidden(), planner_mode='shadow', planner_client=Slow())
        result = await asyncio.wait_for(orchestrator.interpret('неопределённый вопрос',
            planner_snapshot=snapshot(), planner_capabilities=CAPS), timeout=.2)
        assert result.intent.value == 'clarify'
        await orchestrator.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize('completion', ['confirm', 'cancel', 'groq_contract_error'])
def test_real_fallback_to_intake_duplicate_and_confirmation_boundary(monkeypatch, completion):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.handlers import assistant
    from app.services.assistant.evaluation_environment import IsolatedEvaluationEnvironment
    async def run():
        with IsolatedEvaluationEnvironment() as env:
            fallback_result = (HttpResponse(400, b'{}') if completion == 'groq_contract_error' else
                route(domain='warehouse', capability_id='warehouse.prepare_writeoff', action_code='create'))
            wire = Wire(TimeoutError(), fallback_result)
            orchestrator, _ = build_assistant_clients(settings(ai_planner_mode='ai_first',
                ai_planner_fallback_enabled=True, groq_api_key='synthetic'), transport=wire)
            coordinator = AssistantRequestCoordinator()
            prepared = []
            async def intake(message, state, service, raw):
                prepared.append(service.prepare_batch(actor_user_id=env.owner,
                    source_chat_id=env.chat, source_message_id=message.message_id, raw_text=raw))
            monkeypatch.setattr(assistant, 'prepare_intake_message', intake)
            message = SimpleNamespace(from_user=SimpleNamespace(id=env.owner),
                chat=SimpleNamespace(id=env.chat, type='private'), message_id=41,
                text='спиши апельсин 200 грамм', answer=AsyncMock())
            before = env.business.snapshot()
            async def deliver():
                await assistant.handle_assistant_text(message, SimpleNamespace(), env.storage.path,
                    env.access, orchestrator, coordinator, env.policy, env.memory,
                    warehouse_service=env.operations, assistant_capability_controller=env.controller)
            await deliver()
            await deliver()
            assert len(wire.calls) == 2 and env.state()['operations'] == 0
            if completion == 'groq_contract_error':
                assert not prepared and env.business.snapshot() == before
            else:
                assert len(prepared) == 1
                batch = prepared[0]
                if completion == 'confirm':
                    env.operations.commit_batch(batch['id'], actor_user_id=env.owner)
                    from app.storage.warehouse_operations_database import WarehouseOperationsError
                    with pytest.raises(WarehouseOperationsError, match='not ready'):
                        env.operations.commit_batch(batch['id'], actor_user_id=env.owner)
                    assert env.state()['operations'] == 1
                else:
                    env.operations.cancel_batch(batch['id'], actor_user_id=env.owner)
                    assert env.state()['operations'] == 0
    asyncio.run(run())


@pytest.mark.parametrize('header,expected', [('120', 120), ('9999999999', 900),
    ('-1', None), ('NaN', None), ('invalid', None)])
def test_native_transport_reads_only_bounded_retry_after_and_never_redirects(monkeypatch, header, expected):
    from app.services.assistant.providers import AioHttpTransport
    class Response:
        status = 429
        headers = {'Retry-After': header}
        @property
        def content(self): return self
        async def read(self, size): return b'{}'
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    class Session:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def post(self, *args, **kwargs):
            assert kwargs['allow_redirects'] is False
            return Response()
    monkeypatch.setattr(aiohttp, 'ClientSession', Session)
    async def run():
        result = await AioHttpTransport().post_json('https://unused.invalid', headers={},
            payload={}, timeout_seconds=5, max_response_bytes=100)
        assert result.retry_after_seconds == expected
    asyncio.run(run())
