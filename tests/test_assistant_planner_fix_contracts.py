"""Independent offline regressions for the four Gemini-first review findings."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from app.bot import build_assistant_clients
from app.config import Settings
from app.models.assistant_provider_planner import ProviderPlanError
from app.services.assistant.coordinator import AssistantRequestCoordinator
from app.services.assistant.evaluation_corpus import LiveCase, LiveExpectation, LiveInput
from app.services.assistant.evaluation_environment import IsolatedEvaluationEnvironment
from app.services.assistant.evaluation_equivalence import CanonicalEvaluationQuantity, quantity_equivalent
from app.services.assistant.live_evaluation import LiveRunOptions, run_live
from app.services.assistant.planner_providers import (
    CircuitState, GeminiPlannerProvider, GroqPlannerProvider, MultiProviderPlanner,
    _provider_error_diagnostic,
)
from app.services.assistant.providers import HttpResponse, ProviderError, ProviderErrorKind
from tests.test_assistant_live_evaluation import settings as fake_settings
from tests.test_assistant_multi_provider_planner import CAPS, route, snapshot
from tests.test_assistant_resilient_policy import Wire, chain, settings


def response(status, code):
    return HttpResponse(status, json.dumps({'error': {'code': code}}).encode())


@pytest.mark.parametrize('status,code,expected', [
    (400, 'rate_limit_exceeded', ProviderPlanError.UNKNOWN_BAD_REQUEST),
    (400, 'server_error', ProviderPlanError.UNKNOWN_BAD_REQUEST),
    (400, 'safety_refusal', ProviderPlanError.SAFETY_REFUSAL),
    (400, 'json_validate_failed', ProviderPlanError.GENERATED_OUTPUT_SCHEMA_MISMATCH),
    (401, 'rate_limit_exceeded', ProviderPlanError.AUTH),
    (403, 'server_error', ProviderPlanError.AUTH),
    (429, 'safety_refusal', ProviderPlanError.RATE_LIMIT),
    (429, 'invalid_api_key', ProviderPlanError.RATE_LIMIT),
    (500, 'invalid_request', ProviderPlanError.SERVER_ERROR),
    (500, 'invalid_api_key', ProviderPlanError.SERVER_ERROR),
    (404, 'rate_limit_exceeded', ProviderPlanError.CONFIG),
    (200, 'rate_limit_exceeded', ProviderPlanError.MALFORMED_RESPONSE),
])
def test_http_status_is_authoritative(status, code, expected):
    diagnostic = _provider_error_diagnostic(status=status, body=response(status, code).body,
        request_hash='a'*64, schema_hash='b'*64)
    assert diagnostic.category is expected
    assert diagnostic.retryable is (status == 429 or status >= 500)


@pytest.mark.parametrize('status,expected', [(400, ProviderPlanError.UNKNOWN_BAD_REQUEST),
    (401, ProviderPlanError.AUTH), (403, ProviderPlanError.AUTH),
    (429, ProviderPlanError.RATE_LIMIT), (500, ProviderPlanError.SERVER_ERROR)])
def test_malformed_error_body_cannot_change_http_class(status, expected):
    result = _provider_error_diagnostic(status=status, body=b'not-json',
        request_hash='a'*64, schema_hash='b'*64)
    assert result.category is expected


@pytest.mark.parametrize('primary', ['groq', 'gemini'])
def test_http400_rate_code_never_invokes_fallback(primary):
    async def run():
        wire = Wire(response(400, 'rate_limit_exceeded'))
        if primary == 'gemini':
            planner, _, events = chain(wire)
        else:
            planner = MultiProviderPlanner(GroqPlannerProvider(wire, api_key='synthetic'),
                GeminiPlannerProvider(wire, api_key='synthetic', model='synthetic'), mode='ai_first')
        with pytest.raises(ProviderError) as error:
            await planner.plan('найди облепиху', snapshot=snapshot(), capabilities=CAPS)
        assert error.value.kind is ProviderErrorKind.MALFORMED_RESPONSE
        assert len(wire.calls) == 1
    asyncio.run(run())


@pytest.mark.parametrize('bad', [response(400, 'safety_refusal'), response(400, 'model_refusal'),
    route(capability_id='shell.execute'), route(domain='warehouse'),
    route(capability_id='catalog.get', action_code='open', target_index=4)])
def test_terminal_primary_does_not_fallback_or_open_availability_circuit(bad):
    async def run():
        wire = Wire(bad)
        planner, _, events = chain(wire)
        with pytest.raises(ProviderError) as error:
            await planner.plan('спиши апельсин 200 грамм', snapshot=snapshot(), capabilities=CAPS)
        assert error.value.kind in {ProviderErrorKind.SAFETY_REFUSAL, ProviderErrorKind.AUTHORITY}
        assert len(wire.calls) == len(events) == 1
        assert planner.circuit.state('gemini') is CircuitState.CLOSED
    asyncio.run(run())


@pytest.mark.parametrize('completion', [
    response(400, 'safety_refusal'), response(400, 'model_refusal'),
    route(capability_id='shell.execute'), route(domain='catalog', capability_id='warehouse.prepare_writeoff', action_code='create'),
    route(capability_id='catalog.get', action_code='open', target_index=4),
    HttpResponse(200, b'{}'), RuntimeError('synthetic internal failure'),
])
def test_original_terminal_repro_never_prepares_local_mutation(monkeypatch, completion):
    from app.handlers import assistant
    async def run():
        with IsolatedEvaluationEnvironment() as env:
            wire = Wire(TimeoutError(), completion)
            orchestrator, _ = build_assistant_clients(settings(ai_planner_mode='ai_first',
                ai_planner_fallback_enabled=True, groq_api_key='synthetic'), transport=wire)
            events = []
            orchestrator.planner_client.attempt_policy.sink = events.append
            prepared = []
            async def intake(message, state, service, raw):
                # If the regression returns, create a genuine persistent draft.
                prepared.append(service.prepare_batch(actor_user_id=env.owner,
                    source_chat_id=env.chat, source_message_id=message.message_id, raw_text=raw))
            monkeypatch.setattr(assistant, 'prepare_intake_message', intake)
            dispatch = Mock(wraps=env.controller.dispatch_plan)
            monkeypatch.setattr(env.controller, 'dispatch_plan', dispatch)
            message = SimpleNamespace(from_user=SimpleNamespace(id=env.owner),
                chat=SimpleNamespace(id=env.chat, type='private'), message_id=71,
                text='спиши апельсин 200 грамм', answer=AsyncMock())
            before = env.business.snapshot()
            await assistant.handle_assistant_text(message, SimpleNamespace(), env.storage.path,
                env.access, orchestrator, AssistantRequestCoordinator(), env.policy, env.memory,
                warehouse_service=env.operations, assistant_capability_controller=env.controller)
            assert len(wire.calls) == 2
            assert not prepared and dispatch.call_count == 0
            assert env.business.snapshot() == before
            assert env.state()['operations'] == env.state()['proposals'] == 0
            assert message.answer.await_count == 1
            assert orchestrator.planner_client.circuit.state('groq') is CircuitState.CLOSED
    asyncio.run(run())


def test_gemini_native_refusal_is_terminal_and_sanitized():
    async def run():
        wire = Wire(HttpResponse(200, json.dumps({'promptFeedback': {'blockReason': 'SAFETY'}}).encode()))
        planner, _, events = chain(wire)
        with pytest.raises(ProviderError) as error:
            await planner.plan('спиши апельсин 200 грамм', snapshot=snapshot(), capabilities=CAPS)
        assert error.value.kind is ProviderErrorKind.SAFETY_REFUSAL
        assert len(wire.calls) == 1 and events[0]['category'] == 'safety_refusal'
    asyncio.run(run())


@pytest.mark.parametrize('provider', ['groq', 'gemini'])
@pytest.mark.parametrize('kind', [ProviderErrorKind.SAFETY_REFUSAL, ProviderErrorKind.AUTHORITY])
def test_typed_terminal_transport_error_survives_adapter(provider, kind):
    async def run():
        wire = Wire(ProviderError(provider, kind))
        cls = GroqPlannerProvider if provider == 'groq' else GeminiPlannerProvider
        planner = MultiProviderPlanner(cls(wire, api_key='synthetic', model='synthetic'),
            mode='ai_first')
        with pytest.raises(ProviderError) as error:
            await planner.plan('спиши апельсин 200 грамм', snapshot=snapshot(), capabilities=CAPS)
        assert error.value.kind is kind and len(wire.calls) == 1
        assert planner.circuit.state(provider) is CircuitState.CLOSED
    asyncio.run(run())


def test_only_a_read_completed_before_refusal_can_be_retained():
    from app.services.assistant.conversation_brain import ConversationToolLoop
    from tests.test_assistant_multi_provider_planner import decision
    async def run():
        calls, tools = [], []
        async def plan(*args, **kwargs):
            calls.append(True)
            if len(calls) > 1:
                raise ProviderError('gemini', ProviderErrorKind.SAFETY_REFUSAL)
            return decision()
        async def dispatch(*args):
            tools.append(True)
            return SimpleNamespace(message='Safe completed read')
        result, question = await ConversationToolLoop().run_iterative(text='найди облепиху',
            snapshot=snapshot(), capabilities=CAPS, plan=plan, dispatch=dispatch)
        assert len(result) == 1 and len(tools) == 1 and len(calls) == 2
        assert question is None
    asyncio.run(run())


def test_generated_mutation_escalation_has_terminal_attempt_trace():
    async def run():
        wire = Wire(route(capability_id='catalog.manage', action_code='edit', target_index=0))
        planner, _, events = chain(wire)
        with pytest.raises(ProviderError) as error:
            await planner.plan('покажи техкарту облепихи', snapshot=snapshot(), capabilities=CAPS)
        assert error.value.kind is ProviderErrorKind.AUTHORITY
        assert len(wire.calls) == 1 and events[0]['category'] == 'authority_violation'
        assert events[0]['validation'] == 'policy_rejected'
    asyncio.run(run())


@pytest.mark.parametrize('kind', [ProviderErrorKind.SAFETY_REFUSAL, ProviderErrorKind.AUTHORITY])
def test_orchestrator_interpret_never_erases_terminal_refusal(kind):
    from app.services.assistant.orchestrator import AssistantOrchestrator
    class Planner:
        async def plan(self, *args, **kwargs):
            raise ProviderError('planner', kind)
    async def run():
        orchestrator = AssistantOrchestrator(None, planner_mode='ai_first', planner_client=Planner())
        result = await orchestrator.interpret('спиши апельсин 200 грамм',
            planner_snapshot=snapshot(), planner_capabilities=CAPS)
        assert result.intent.value == 'clarify'
    asyncio.run(run())


@pytest.mark.parametrize('mode', ['local_first', 'shadow', 'ai_first'])
def test_groq_only_disabled_fallback_requires_no_gemini_contract(mode):
    config = settings(ai_planner_mode=mode, ai_planner_primary_provider='groq',
        ai_planner_fallback_provider='gemini', ai_planner_fallback_enabled=False,
        groq_api_key='synthetic', gemini_api_key=None, gemini_planner_model=None,
        gemini_fast_model=None, gemini_strong_model=None)
    orchestrator, speech = build_assistant_clients(config, transport=Wire())
    assert isinstance(orchestrator.planner_client.primary, GroqPlannerProvider)
    assert orchestrator.planner_client.fallback is None
    assert orchestrator.intent_client is None and orchestrator.search_client is None and speech is None


@pytest.mark.parametrize('fallback', ['gemini', 'unknown', None, ' '])
def test_deployment_old_or_unused_fallback_name_is_ignored(fallback):
    config = settings(ai_planner_fallback_provider=fallback)
    orchestrator, _ = build_assistant_clients(config, transport=Wire())
    assert isinstance(orchestrator.planner_client.primary, GeminiPlannerProvider)
    assert orchestrator.planner_client.fallback is None
    with pytest.raises(ValidationError):
        settings(ai_planner_fallback_provider=fallback, ai_planner_fallback_enabled=True)


@pytest.mark.parametrize('primary,fallback', [('groq', 'gemini'), ('gemini', 'groq')])
def test_enabled_fallback_requires_and_constructs_both_contracts(primary, fallback):
    config = settings(ai_planner_mode='ai_first', ai_planner_primary_provider=primary,
        ai_planner_fallback_provider=fallback, ai_planner_fallback_enabled=True, groq_api_key='synthetic')
    orchestrator, _ = build_assistant_clients(config, transport=Wire())
    assert orchestrator.planner_client.primary.provider_name == primary
    assert orchestrator.planner_client.fallback.provider_name == fallback
    assert orchestrator.intent_client is None
    with pytest.raises(ValidationError):
        settings(ai_planner_mode='ai_first', ai_planner_primary_provider=primary,
            ai_planner_fallback_provider=fallback, ai_planner_fallback_enabled=True,
            groq_api_key=None if fallback == 'groq' else 'synthetic',
            gemini_api_key=None if fallback == 'gemini' else 'synthetic')


@pytest.mark.parametrize('field', ['groq_api_key', 'groq_planner_model'])
def test_active_groq_contract_rejects_whitespace(field):
    values = dict(ai_planner_primary_provider='groq', groq_api_key='synthetic', gemini_api_key=None)
    values[field] = ' '
    with pytest.raises(ValidationError): settings(**values)


@pytest.mark.parametrize('feature', ['ai_general_chat_enabled', 'ai_web_search_enabled'])
def test_gemini_legacy_general_web_dependency_is_explicit(feature):
    values = dict(ai_planner_primary_provider='groq', groq_api_key='synthetic',
        gemini_api_key=None, **{feature: True})
    with pytest.raises(ValidationError, match='GEMINI_API_KEY'):
        settings(**values)
    values['gemini_api_key'] = 'synthetic'
    orchestrator, _ = build_assistant_clients(settings(**values), transport=Wire())
    assert (orchestrator.intent_client is not None) == (feature == 'ai_general_chat_enabled')
    assert (orchestrator.search_client is not None) == (feature == 'ai_web_search_enabled')
    assert orchestrator.planner_client.fallback is None


def test_groq_voice_only_keeps_whisper_and_planner_separate():
    config = settings(ai_text_enabled=False, ai_voice_enabled=True, ai_planner_primary_provider='groq',
        groq_api_key='synthetic', gemini_api_key=None, groq_planner_fallback_model=None)
    orchestrator, speech = build_assistant_clients(config, transport=Wire())
    assert speech.model == config.groq_stt_model
    assert orchestrator.planner_client.primary.model == config.groq_planner_model
    assert orchestrator.planner_client.fallback is None and orchestrator.intent_client is None


@pytest.mark.parametrize('unit', ['g', 'gram', 'grams', 'г', 'гр', 'грамм', 'грамма', 'граммов', 'kg', 'кг'])
def test_real_runner_quantity_equivalence_from_resolved_db(unit):
    async def run():
        wire = Wire(route(domain='warehouse', capability_id='warehouse.prepare_writeoff', action_code='create'))
        expected = LiveExpectation(capabilities=('warehouse.prepare_writeoff',), domain='warehouse',
            preview=True, argument_ok=True, quantity_text='0.2' if unit in {'kg', 'кг'} else '200', unit_text=unit)
        case = LiveCase(LiveInput('regression_D', 'regression', 'спиши апельсин 200 грамм'), expected)
        report, code = await run_live(LiveRunOptions('live_isolated', True, 34, 'groq', True),
            settings_loader=fake_settings, transport_factory=lambda: wire, corpus=(case,))
        actual = report['rows'][0]
        assert code == 0 and report['ready']
        assert actual['quantity_text'] == '0.2' and actual['unit_text'] == 'kg'
        assert actual['argument_ok'] and actual['scores']['argument_extraction']
        assert report['domain_metrics']['warehouse']['argument_extraction'] == 1
        assert actual['operations'] == 0 and report['false_mutation_count'] == 0
        assert case.request.text not in json.dumps(report, ensure_ascii=False)
        assert 'quantity_text' not in json.dumps(wire.calls, ensure_ascii=False)
    asyncio.run(run())


@pytest.mark.parametrize('unit', ['ml', 'мл', 'l', 'liter', 'litre', 'л', 'литр'])
def test_real_runner_volume_conversion(unit):
    async def run():
        wire = Wire(route(domain='warehouse', capability_id='warehouse.prepare_transfer', action_code='create'))
        case = LiveCase(LiveInput('regression_volume', 'regression', 'перемести кокосовое молоко 0.5 л на кухню'),
            LiveExpectation(capabilities=('warehouse.prepare_transfer',), preview=True, argument_ok=True,
                quantity_text='500' if unit in {'ml', 'мл'} else '0.5', unit_text=unit))
        report, code = await run_live(LiveRunOptions('live_isolated', True, 34, 'groq', True),
            settings_loader=fake_settings, transport_factory=lambda: wire, corpus=(case,))
        assert code == 0 and report['ready']
        assert report['rows'][0]['unit_text'] == 'l' and report['rows'][0]['operations'] == 0
    asyncio.run(run())


@pytest.mark.parametrize('missing', [False, True])
def test_real_runner_quantity_mismatch_or_absence_fails_semantic_readiness(missing):
    async def run():
        wire = Wire(route() if not missing else route(domain='general', capability_id='none', action_code='explain'))
        if not missing:
            wire = Wire(route(domain='warehouse', capability_id='warehouse.prepare_writeoff', action_code='create'))
        case = LiveCase(LiveInput('quantity_negative', 'regression',
            'что такое списание' if missing else 'спиши апельсин 200 грамм'),
            LiveExpectation(preview=not missing, quantity_text='200', unit_text='g' if missing else 'ml',
                capabilities=None if missing else ('warehouse.prepare_writeoff',)))
        report, code = await run_live(LiveRunOptions('live_isolated', True, 34, 'groq', True),
            settings_loader=fake_settings, transport_factory=lambda: wire, corpus=(case,))
        assert code == 1 and not report['ready']
        assert report['rows'][0]['scores']['argument_extraction'] is False
        assert report['false_mutation_count'] == 0 and report['operations_before_confirmation'] == 0
    asyncio.run(run())


@pytest.mark.parametrize('value', ['NaN', 'Infinity', '-Infinity', '-1', '0', 'invalid'])
def test_invalid_canonical_quantity_is_rejected(value):
    with pytest.raises(ValidationError):
        CanonicalEvaluationQuantity(quantity_text=value, unit_text='kg')
    assert not quantity_equivalent(value, 'kg', '200', 'g')


@pytest.mark.parametrize('quantity,unit,expected,expected_unit', [
    ('200', 'g', '200', 'ml'), ('1', 'kg', '1', 'l'), ('1', 'kg', '1', 'g'),
    ('200', 'g', '200.000000000000000000000000000001', 'g'),
    (None, 'g', '200', 'g'), ('200', None, '200', 'g'),
])
def test_missing_or_dimensionally_incompatible_quantity_cannot_pass(quantity, unit, expected, expected_unit):
    assert not quantity_equivalent(quantity, unit, expected, expected_unit)
