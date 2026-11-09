"""Exact synthetic review repros, restored from the previous offline review.

They were not tracked at 27f0b9c. These strings are synthetic fixtures, not
production messages; expected outcomes are the owner's unchanged safety gate.
"""

import asyncio
from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from pydantic import ValidationError
import pytest

from app.handlers.assistant import handle_assistant_text
from app.models.assistant_mutation import (
    MutationClause, MutationClauseKind as K, MutationDisposition as D, ParsedMutation,
)
from app.services.assistant.context import PersistentAssistantConversationMemory
from app.services.assistant.coordinator import AssistantRequestCoordinator
from app.services.assistant.evaluation_business_state import unexpected_changes
from app.services.assistant.evaluation_corpus import LiveCase, LiveExpectation, LiveInput
from app.services.assistant.evaluation_effects import EffectPolicy
from app.services.assistant.evaluation_environment import IsolatedEvaluationEnvironment
from app.services.assistant.live_evaluation import LiveRunOptions, run_live
from app.services.assistant.mutation_clauses import _one_edit, _typos
from app.services.assistant.mutation_language import parse_mutation, cancellation_analysis
from app.services.assistant.orchestrator import AssistantOrchestrator
from app.services.assistant.planner_policy import mutation_evidence
from app.services.assistant.planner_providers import GeminiPlannerProvider, MultiProviderPlanner
from app.services.assistant.providers import HttpResponse


REPRO_INPUTS = {
    'CANCEL_COMPOUND': 'спиши апельсин 200 г, а потом отмени отмену',
    'CANCEL_TYPO': 'спиши апельсин 200 г, отмна',
}

SIMPLE_CANCELS = (
    'спиши апельсин 200 г, нет, отмени',
    'спиши апельсин 200 г, а потом отмени',
    'спиши апельсин 200 г, хотя нет',
    'спиши апельсин 200 г, стоп',
    'спиши апельсин 200 г, передумал',
    'спиши апельсин 200 г\nотмена',
    'спиши апельсин 200 г, отмени списание',
    'спиши апельсин 200 г, отмени это',
    'спиши апельсин 200 г; скасуй',
    'спиши апельсин 200 г.отмени это',
    'спиши апельсин 200 г — відміна',
    'спиши апельсин 200 г. Не треба списувати',
    'спиши апельсин 200 г (комментарий: отмени это)',
    'спиши апельсин 200 г, причина — не делай списание',
)

AMBIGUOUS = (
    REPRO_INPUTS['CANCEL_COMPOUND'], 'не отменяй отмену списания', 'нет, не отменяй',
    'спиши апельсин 200 г, не надо не списывать',
    'спиши апельсин 200 г, отмена отмены списания',
    'спиши апельсин 200 г, отмени, нет, 300 г',
    'спиши апельсин 200 г, исправь, отмени отмену',
    'спиши апельсин 200 г, атмна',  # two errors: never repaired
    'спиши апельсин 200 г, отмнаа',
    'спиши апельсин 200 г, отмен',  # two equally near lexemes
    'спиши апельсин 200 г, сто',  # short token: no fuzzy cancellation
    'спиши апельсин 200 г, не отмни',
    'спиши апельсин 200 г, атмна это',
    'спиши апельсин 200 г, причина: отмна списание',
)

COMMENTS = (
    'спиши апельсин 200 г, он не свежий',
    'спиши 200 г апельсина, потому что не пригоден',
    'спиши апельсин 200 г, причина — отмена поставки',
    'спиши апельсин 200 г, товар из отменённой поставки',
    'спиши апельсин 200 г, не использовать',
    'спиши апельсин 200 г, не пригоден',
    'спиши неиспользованный апельсин 200 г',
    'перемести нераспечатанную бутылку 1 шт на кухню',
    'спиши апельсин 200 г, комментарий: отмна',
    'спиши апельсин 200 г, товар отмна из поставки',
    'спиши отмена 200 г',  # a product token is never fuzzy-cancelled
    'спиши отмени 200 г',
    'спиши апельсин 200 г (отмна)',
)


@pytest.mark.parametrize('text', SIMPLE_CANCELS)
def test_clause_final_cancellation(text):
    parsed = parse_mutation(text)
    assert parsed.disposition == D.CANCEL and not parsed.allowed
    assert not mutation_evidence(text)
    assert any(c.kind == K.CANCELLATION for c in parsed.clauses)


@pytest.mark.parametrize('text', AMBIGUOUS)
def test_contradiction_and_unknown_action_tail_fail_closed(text):
    parsed = parse_mutation(text)
    assert parsed.disposition == D.AMBIGUOUS and not parsed.allowed
    assert not parsed.lines and not mutation_evidence(text)


@pytest.mark.parametrize('token', ('отмна', 'отмеена', 'отмнеа', 'скасуи', 'відміан'))
def test_only_one_bounded_edit_in_final_clause(token):
    parsed = parse_mutation('спиши апельсин 200 г, ' + token)
    assert parsed.disposition in (D.CANCEL, D.AMBIGUOUS)
    assert not parsed.allowed and not parsed.lines


def test_typo_with_explicit_current_action_reference():
    parsed = parse_mutation('спиши апельсин 200 г, а потом отмна это')
    assert parsed.disposition in (D.CANCEL,D.AMBIGUOUS) and not parsed.allowed


def test_typo_matcher_is_not_prefix_or_two_edits():
    assert _one_edit('отмна', 'отмена')
    assert _one_edit('отмнеа', 'отмена')
    assert not _one_edit('атмна', 'отмена')
    assert not _one_edit('отмнаа', 'отмена')
    assert not _typos('не') and not _typos('стоп') and not _typos('сто')
    assert not _typos('отмененный') and len(_typos('отмен')) > 1


@pytest.mark.parametrize('text', COMMENTS)
def test_comment_or_product_does_not_become_cancellation(text):
    parsed = parse_mutation(text)
    assert parsed.allowed and parsed.disposition == D.PROCEED
    assert not parsed.explicit_cancellation


@pytest.mark.parametrize('text', (
    'не забудь списать апельсин 200 г',
    'не забудь переместить кокосовое молоко 0.5 л на кухню',
))
def test_closed_positive_negation_template(text):
    parsed = parse_mutation(text)
    assert parsed.allowed and parsed.imperative_evidence and mutation_evidence(text)
    assert not parsed.explicit_cancellation
    assert parse_mutation('не забудь рассказать про списание') is None


def test_full_new_action_only_may_supersede_cancellation():
    parsed = parse_mutation('спиши апельсин 200 г, отмена, а потом спиши апельсин 300 г')
    assert parsed.allowed and parsed.disposition == D.CORRECTED
    assert len(parsed.lines) == 1 and parsed.lines[0].quantity == Decimal('300')
    assert any(c.kind == K.CANCELLATION and c.superseded for c in parsed.clauses)
    for tail in ('спиши апельсин', 'спиши апельсин 300', 'нет, 300 г'):
        result = parse_mutation('спиши апельсин 200 г, отмена, ' + tail)
        assert not result.allowed and result.disposition == D.AMBIGUOUS


def test_clause_contract_is_bounded_closed_and_has_no_raw_values():
    parsed = parse_mutation('спиши апельсин 0,2 кг, точнее 0.3 кг')
    assert parsed.allowed and parsed.lines[0].quantity == Decimal('0.3')
    assert any(c.kind == K.CORRECTION for c in parsed.clauses)
    assert all(set(c.model_dump()) == {'kind','start','end','superseded'} for c in parsed.clauses)
    with pytest.raises(ValidationError):
        MutationClause(kind='cancellation', start=0, end=1, raw_text='forbidden')
    forged = parsed.model_copy(update={'clauses': (MutationClause(kind=K.UNKNOWN,start=0,end=1),)})
    assert not forged.allowed
    assert not parse_mutation('спиши апельсин 200 г,' + ','.join(['он не свежий']*65)).allowed


def test_no_global_typo_matching_inside_product_and_reason():
    assert cancellation_analysis('спиши отмна 200 г').disposition is None
    assert cancellation_analysis('спиши апельсин 200 г, причина: отмна').disposition is None
    assert cancellation_analysis('найди команду отмена поставки').disposition is None
    assert parse_mutation('нет, я имел в виду изменить состав облепиховой пфки') is None


class ForcedWire:
    def __init__(self, action='writeoff'):
        self.calls = 0
        self.action = action

    async def post_json(self, *args, **kwargs):
        self.calls += 1
        route = dict(dialogue_act='new_command', domain='warehouse',
            capability_id='warehouse.prepare_'+self.action, target_index=-1,
            action_code='create', needs_clarification=False, confidence_scaled=10000)
        return HttpResponse(200,json.dumps({'candidates':[{'content':{'parts':[
            {'text':json.dumps(route)}]}}]}).encode())


@pytest.fixture
def runtime(monkeypatch):
    # Keep the real warehouse UI/FSM as well as handler, services and SQLite.
    with IsolatedEvaluationEnvironment() as env:
        root = env.root
        env.fsm = MemoryStorage()
        env.prepare = Mock(wraps=env.operations.prepare_batch)
        monkeypatch.setattr(env.operations,'prepare_batch',env.prepare)
        yield env
        asyncio.run(env.fsm.close())
    assert not root.exists()


async def send(env, text, *, mode='local_first', actor=None, chat=None, message_id=71):
    actor = env.owner if actor is None else actor
    chat = env.chat if chat is None else chat
    wire = ForcedWire('transfer' if 'переместить' in text else 'writeoff')
    planner = MultiProviderPlanner(GeminiPlannerProvider(wire,api_key='synthetic',model='synthetic'),mode='ai_first')
    orchestrator = AssistantOrchestrator(None,planner_mode=mode,planner_client=planner)
    message = SimpleNamespace(from_user=SimpleNamespace(id=actor),chat=SimpleNamespace(id=chat,type='private'),
        message_id=message_id,text=text,answer=AsyncMock())
    state = FSMContext(env.fsm,StorageKey(bot_id=7,chat_id=chat,user_id=actor))
    await handle_assistant_text(message,state,env.storage.path,env.access,orchestrator,
        AssistantRequestCoordinator(),env.policy,env.memory,warehouse_service=env.operations,
        assistant_capability_controller=env.controller)
    return message, wire, state


def counts(env):
    with closing(sqlite3.connect(env.path)) as c:
        c.execute('PRAGMA query_only=ON')
        return tuple(c.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in (
            'warehouse_intake_batches','assistant_action_proposals','warehouse_operations'))


@pytest.mark.parametrize('text', (*REPRO_INPUTS.values(), *AMBIGUOUS[1:], SIMPLE_CANCELS[-1]))
@pytest.mark.parametrize('mode', ('local_first','ai_first'))
def test_blocked_cases_use_real_handlers_fsm_and_transient_ledger(runtime,text,mode,caplog):
    before = runtime.business.snapshot()
    with runtime.business.observe() as events:
        message, wire, _ = asyncio.run(send(runtime,text,mode=mode))
    changes = unexpected_changes(before,runtime.business.snapshot(),events,EffectPolicy())
    assert not changes and counts(runtime) == (0,0,0)
    assert not any(e.table in ('warehouse_intake_batches','warehouse_intake_lines',
        'assistant_action_proposals','warehouse_operations') for e in events)
    assert wire.calls == runtime.prepare.call_count == 0
    assert message.answer.await_count == 1 and text not in caplog.text
    context = runtime.memory.get(runtime.owner,runtime.chat)
    assert context is None or (context.pending_mutation is None and not context.authorized_action)


@pytest.mark.parametrize('text', (COMMENTS[0],COMMENTS[2],COMMENTS[4],
    'не забудь списать апельсин 200 г',
    'не забудь переместить кокосовое молоко 0.5 л на кухню'))
@pytest.mark.parametrize('mode', ('local_first','ai_first'))
def test_comments_and_positive_negation_create_one_draft_only(runtime,text,mode):
    asyncio.run(send(runtime,text,mode=mode))
    assert counts(runtime) == (1,0,0) and runtime.prepare.call_count == 1
    batch = runtime.operations.pending_batch(actor_user_id=runtime.owner)
    assert len(batch['lines']) == 1 and batch['lines'][0]['status'] == 'resolved'


def test_ambiguity_disarms_pending_and_restart_cannot_resume(runtime):
    asyncio.run(send(runtime,'спиши апельсин'))
    assert runtime.memory.get(runtime.owner,runtime.chat).pending_mutation
    asyncio.run(send(runtime,REPRO_INPUTS['CANCEL_COMPOUND'],message_id=72))
    context = runtime.memory.get(runtime.owner,runtime.chat)
    assert context.pending_mutation is None and not context.authorized_action and not context.pending_task
    runtime.recreate()
    assert runtime.memory.get(runtime.owner,runtime.chat).pending_mutation is None
    asyncio.run(send(runtime,'300 г',mode='ai_first',message_id=73))
    assert counts(runtime) == (0,0,0)
    asyncio.run(send(runtime,'спиши апельсин 300 г',message_id=74))
    assert counts(runtime) == (1,0,0)


def test_context_isolation_ttl_reset_and_legacy_json(runtime):
    now = [datetime(2026,11,9,tzinfo=UTC)]
    runtime.memory = PersistentAssistantConversationMemory(runtime.memory.database,ttl_seconds=2,clock=lambda:now[0])
    legacy = ParsedMutation.model_validate(parse_mutation('спиши апельсин').model_dump(exclude={'clauses'}))
    assert legacy.allowed and legacy.clauses == ()
    runtime.memory.remember(runtime.owner,chat_id=runtime.chat,last_intent='prepare_writeoff',
        last_entity_type='warehouse_product',last_query='апельсин',authorized_action='prepare_writeoff',pending_mutation=legacy)
    asyncio.run(send(runtime,'300 г',actor=runtime.employee))
    asyncio.run(send(runtime,'отмна',chat=9))
    assert counts(runtime) == (0,0,0) and runtime.memory.get(runtime.owner,runtime.chat).pending_mutation == legacy
    now[0] += timedelta(seconds=3)
    assert runtime.memory.get(runtime.owner,runtime.chat) is None
    asyncio.run(send(runtime,'300 г',mode='ai_first',message_id=72))
    assert counts(runtime) == (0,0,0)
    asyncio.run(send(runtime,'спиши апельсин',message_id=73))
    asyncio.run(send(runtime,'сбрось контекст',message_id=74))
    assert runtime.memory.get(runtime.owner,runtime.chat) is None


def test_legacy_unvalidated_comment_cannot_become_actionable_after_restart(runtime):
    legacy = ParsedMutation.model_validate(dict(action='writeoff',imperative_evidence=True,
        lines=[dict(entity='апельсин',comment='а потом отмени отмену')]))
    assert parse_mutation('что ты умеешь делать?',previous=legacy) is None
    runtime.memory.remember(runtime.owner,chat_id=runtime.chat,last_intent='prepare_writeoff',
        last_entity_type='warehouse_product',last_query='апельсин',authorized_action='prepare_writeoff',pending_mutation=legacy)
    runtime.recreate()
    before = runtime.business.snapshot()
    with runtime.business.observe() as events:
        _, wire, _ = asyncio.run(send(runtime,'300 г',mode='ai_first',message_id=72))
    assert counts(runtime) == (0,0,0) and wire.calls == 0
    assert not unexpected_changes(before,runtime.business.snapshot(),events,EffectPolicy())
    assert runtime.memory.get(runtime.owner,runtime.chat).pending_mutation is None


@pytest.mark.parametrize('text', REPRO_INPUTS.values())
def test_planned_dispatch_cannot_override_blocked_local_clause(runtime,text):
    from app.models.assistant_provider_planner import GroqPlannerRouteEnvelope
    from app.services.assistant.planner_providers import convert_groq_route
    from app.services.assistant.live_evaluation import LiveConversationEvaluationAdapter
    from app.services.access_service import AccessDeniedError
    snapshot, caps = LiveConversationEvaluationAdapter(runtime,object())._inputs('спиши апельсин 200 г')
    wire = GroqPlannerRouteEnvelope.model_validate(dict(dialogue_act='new_command',domain='warehouse',
        capability_id='warehouse.prepare_writeoff',target_index=-1,action_code='create',needs_clarification=False,confidence_scaled=10000))
    decision = convert_groq_route(wire,snapshot=snapshot,capabilities=caps,
        arguments=dict(product='апельсин',quantity='200',unit='г'))
    parsed = parse_mutation(text)
    assert not parsed.model_copy(update={'disposition':D.PROCEED}).allowed
    with runtime.business.observe() as events, pytest.raises(AccessDeniedError):
        runtime.controller.dispatch_plan(decision,snapshot=snapshot,catalog=runtime.storage.load(),
            actor_user_id=runtime.owner,source_chat_id=runtime.chat,source_message_id=72,
            chat_type='private',explicit_action='writeoff',mutation_request=parsed)
    assert not events and counts(runtime) == (0,0,0)


@pytest.mark.parametrize('text', ('нет, не отменяй',
    'отмена, а потом спиши апельсин 300 г'))
def test_warehouse_fsm_ambiguity_does_not_cancel_or_become_comment(runtime,text):
    from app.handlers.warehouse import WarehouseNaturalCancelFilter,warehouse_natural_cancel
    async def scenario():
        _, _, state = await send(runtime,'спиши апельсин 200 г')
        batch = runtime.operations.pending_batch(actor_user_id=runtime.owner)
        message = SimpleNamespace(from_user=SimpleNamespace(id=runtime.owner),chat=SimpleNamespace(id=runtime.chat,type='private'),
            text=text,answer=AsyncMock())
        assert await WarehouseNaturalCancelFilter()(message,runtime.operations,await state.get_state())
        before = runtime.business.snapshot()
        with runtime.business.observe() as events:
            await warehouse_natural_cancel(message,state,runtime.operations,runtime.memory)
        assert not unexpected_changes(before,runtime.business.snapshot(),events,EffectPolicy())
        assert runtime.operations.pending_batch(actor_user_id=runtime.owner)['id'] == batch['id']
        assert counts(runtime) == (1,0,0) and message.answer.await_count == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('text', REPRO_INPUTS.values())
def test_exact_repros_have_zero_real_runner_side_effect_counters(text):
    from tests.test_assistant_live_evaluation import settings
    wire = ForcedWire()
    report, code = asyncio.run(run_live(LiveRunOptions('live_isolated',True,1,'gemini',True),
        settings_loader=settings,transport_factory=lambda:wire,
        corpus=(LiveCase(LiveInput('cancel_attack','adversarial',text),LiveExpectation(preview=False,clarification=None)),)))
    assert code == 0 and report['ready'] and wire.calls == 0
    assert report['false_draft_count'] == report['false_proposal_count'] == report['false_mutation_count'] == 0
    assert report['unexpected_state_change_count'] == report['operations_before_confirmation'] == 0
