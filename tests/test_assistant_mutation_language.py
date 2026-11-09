"""Adversarial language → real handler/service → owned SQLite v15 regressions."""

import asyncio
from contextlib import closing
from decimal import Decimal
import json
import logging
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from app.handlers import assistant
from app.models.assistant_mutation import MutationDisposition as D, MutationLine, ParsedMutation
from app.models.assistant_provider_planner import PlannerAuthorityError
from app.services.assistant.bar_language import parse_bar_request
from app.services.assistant.coordinator import AssistantRequestCoordinator
from app.services.assistant.evaluation_corpus import LiveCase, LiveExpectation, LiveInput
from app.services.assistant.evaluation_environment import IsolatedEvaluationEnvironment
from app.services.assistant.live_evaluation import LiveRunOptions, run_live
from app.services.assistant.mutation_language import parse_mutation
from app.services.assistant.orchestrator import AssistantOrchestrator
from app.services.assistant.planner_arguments import local_stage_two_arguments
from app.services.assistant.planner_policy import mutation_evidence
from app.services.assistant.planner_providers import (
    GeminiPlannerProvider, MultiProviderPlanner, convert_groq_route,
)
from app.services.assistant.providers import HttpResponse


CANCELS = ('не списывай','не надо списывать','не надо','ничего не списывай','отмена',
    'отмени','отбой','не делай','передумал','передумала','хотя нет','нет, отмени',
    'стоп','не списуй','не треба списувати','скасуй','відміна','передумав')
BAD_NUMBERS = ('-200','−200','–200','+200','0','NaN','Inf','infinity','1e3',
    '1,2,3','1.2.3','1,2.3','1,','1.','12abc','1к2','200/300',
    '200-300','200–300','0.0000000001','100000000000000000000','- 200','− 200','+ 200')


def route(action='writeoff'):
    return dict(dialogue_act='new_command',domain='warehouse',
        capability_id='warehouse.prepare_'+action,target_index=-1,
        action_code='create',needs_clarification=False,confidence_scaled=10000)


class Wire:
    def __init__(self, action='writeoff'):
        self.calls=0
        self.action=action

    async def post_json(self,*args,**kwargs):
        self.calls+=1
        return HttpResponse(200,json.dumps({'candidates':[{'content':{'parts':[
            {'text':json.dumps(route(self.action))} ]}}]}).encode())


@pytest.mark.parametrize('tail',CANCELS)
@pytest.mark.parametrize('separator',(', ',' '))
def test_action_related_cancellation_precedes_evidence(tail,separator):
    text='спиши апельсин 200 г'+separator+tail
    parsed=parse_mutation(text)
    assert parsed.disposition is D.CANCEL and parsed.explicit_cancellation
    assert not parsed.allowed and not mutation_evidence(text)
    assert not parse_bar_request(text).mutation


@pytest.mark.parametrize('number',BAD_NUMBERS)
def test_entire_quantity_token_is_rejected(number):
    parsed=parse_mutation('спиши апельсин '+number+' кг')
    assert parsed.disposition in (D.INVALID,D.AMBIGUOUS)
    assert not parsed.allowed and parsed.error is not None and not parsed.lines
    assert not mutation_evidence('спиши апельсин '+number+' кг')
    with pytest.raises(PlannerAuthorityError):
        local_stage_two_arguments('warehouse.prepare_writeoff','спиши апельсин '+number+' кг')


@pytest.mark.parametrize('value,quantity,unit',(
    ('200','200',''),('200 г','200','г'),('200г','200','г'),
    ('0.2 кг','0.2','кг'),('0,2 кг','0.2','кг'),('1 200 г','1200','г'),
    ('.5 л','0.5','л'),(',5 л','0.5','л'),
    ('200 г.','200','г'),('200г.','200','г'),('200 грамм.','200','грамм'),
))
def test_whole_valid_tokens_use_decimal(value,quantity,unit):
    parsed=parse_mutation('спиши апельсин '+value)
    assert parsed.allowed and parsed.lines[0].quantity==Decimal(quantity)
    assert type(parsed.lines[0].quantity) is Decimal and parsed.lines[0].unit==unit


@pytest.mark.parametrize('text,quantity,unit',(
    ('спиши апельсин 200 г, нет, 300 г','300','г'),
    ('спиши не 200, а 300 грамм апельсина','300','грамм'),
    ('спиши апельсин 200 г, точнее 250 г','250','г'),
    ('спиши апельсин 200 г, точнее 250 г, нет, 300 г','300','г'),
    ('перемести молоко 1 л на кухню, исправь на 500 мл','500','мл'),
    ('спиши апельсин 200 г, нет, 0.3 кг','0.3','кг'),
))
def test_last_unambiguous_correction_has_one_canonical_quantity(text,quantity,unit):
    parsed=parse_mutation(text)
    assert parsed.disposition is D.CORRECTED and parsed.correction_segment
    assert len(parsed.lines)==1 and parsed.lines[0].quantity==Decimal(quantity)
    assert parsed.lines[0].unit==unit and parsed.allowed


@pytest.mark.parametrize('text',(
    'спиши апельсин 200 или 300 грамм','спиши примерно 200–300 г',
    'спиши апельсин может 200, может 500','спиши апельсин, нет лимон',
    'спиши апельсин 200 г 300 г','спиши апельсин 200 г 1 л',
    'спиши апельсин 200 г, а 300 г','спиши апельсин 200 г, потом 300 г',
    'перемести молоко 1 л на кухню -200',
    'спиши апельсин 200 г, нет, 300 мл','спиши не 200 г, а 300 мл апельсина',
))
def test_ambiguity_or_incompatible_correction_cannot_mutate(text):
    parsed=parse_mutation(text)
    assert not parsed.allowed and parsed.disposition in (D.INVALID,D.AMBIGUOUS)


@pytest.mark.parametrize('text',(
    'спиши апельсин 200 г, не свежий',
    'спиши 200 г апельсина, потому что он не пригоден',
    'спиши апельсин 200 г (не свежий)',
    'перемести нераспечатанную бутылку',
))
def test_descriptive_negation_is_not_cancellation(text):
    parsed=parse_mutation(text)
    assert parsed.allowed and not parsed.explicit_cancellation


def test_typed_contract_forbids_unknown_fields_and_float():
    with pytest.raises(ValidationError):
        ParsedMutation(action='writeoff',permissions='super_admin')
    with pytest.raises(ValidationError):
        MutationLine(entity='апельсин',quantity=0.2)
    with pytest.raises(ValidationError):
        MutationLine(entity='апельсин',quantity=Decimal('NaN'))


def test_correction_fragments_use_only_typed_previous_fields():
    previous=parse_mutation('спиши апельсин')
    parsed=parse_mutation('200 г, точнее 250 г',previous=previous)
    assert parsed.disposition is D.CORRECTED and parsed.lines[0].quantity==Decimal('250')
    previous=parse_mutation('перемести молоко на кухню')
    parsed=parse_mutation('перемести 1 л, исправь на 500 мл')
    assert not parsed.allowed and parsed.lines[0].quantity==Decimal('500')
    assert parsed.disposition is D.CORRECTED
    assert parse_mutation('500 мл',previous=previous).lines[0].entity=='молоко'
    padded=parse_mutation('спиши апельсин 200 г, нет, 300'+' '*2100+'г')
    assert padded.allowed and padded.correction_segment=='300 г'


def test_explicit_product_and_destination_corrections():
    parsed=parse_mutation('спиши апельсин 200 г, замени продукт на лимон')
    assert parsed.allowed and parsed.lines[0].entity=='лимон'
    parsed=parse_mutation('перемести молоко 1 л на кухню, исправь направление на кондитерку')
    assert parsed.allowed and parsed.lines[0].destination=='кондитерка'


@pytest.mark.parametrize('text',('спиши апельсин 200 г, нет','измени состав, нет',
    'открой мою смену, передумал','отправь отчет, не делай','измени состав стоп'))
def test_terminal_cancellation_cannot_be_generic_mutation_evidence(text):
    assert parse_mutation(text).disposition is D.CANCEL
    assert not mutation_evidence(text)


def test_inflection_is_exact_unique_and_never_fuzzy():
    from dataclasses import replace
    from app.services.assistant.mutation_language import resolve_product_case
    from app.services.warehouse.resolution_service import ProductOption
    from app.models.warehouse.nomenclature import BaseUnit
    product=ProductOption(1,'Апельсин','апельсин',BaseUnit.KG,(),frozenset({'product_writeoff'}))
    parsed=parse_mutation('спиши 200 г апельсина')
    assert resolve_product_case(parsed,(product,)).lines[0].entity=='Апельсин'
    collision=replace(product,id=2)
    assert resolve_product_case(parsed,(product,collision)).lines[0].entity=='апельсина'
    misspelled=parse_mutation('спиши апелсин 200 г')
    assert resolve_product_case(misspelled,(product,)).lines[0].entity=='апелсин'


@pytest.mark.parametrize('text',('отправь отчет в рабочую группу',
    'отправь сегодняшние списания получателю','отправь табель XLSX'))
def test_document_send_is_not_intercepted_as_warehouse_transfer(text):
    assert parse_mutation(text) is None
    assert not parse_bar_request(text).mutation


@pytest.fixture
def runtime(monkeypatch):
    with IsolatedEvaluationEnvironment() as env:
        prepared=[]
        async def intake(message,state,service,raw):
            prepared.append(service.prepare_batch(actor_user_id=message.from_user.id,
                source_chat_id=message.chat.id,source_message_id=message.message_id,raw_text=raw))
        monkeypatch.setattr(assistant,'prepare_intake_message',intake)
        env.prepared=prepared
        env.dispatch=Mock(wraps=env.controller.dispatch_plan)
        monkeypatch.setattr(env.controller,'dispatch_plan',env.dispatch)
        yield env
    assert not env.root.exists()


async def send(env,text,*,mode='local_first',wire=None,message_id=71,actor=None,chat=None,chat_type='private'):
    wire=wire or Wire()
    planner=MultiProviderPlanner(GeminiPlannerProvider(wire,api_key='synthetic',model='synthetic'),mode='ai_first')
    orchestrator=AssistantOrchestrator(None,planner_mode=mode,planner_client=planner)
    message=SimpleNamespace(from_user=SimpleNamespace(id=env.owner if actor is None else actor),
        chat=SimpleNamespace(id=env.chat if chat is None else chat,type=chat_type),
        message_id=message_id,text=text,answer=AsyncMock())
    await assistant.handle_assistant_text(message,SimpleNamespace(),env.storage.path,env.access,
        orchestrator,AssistantRequestCoordinator(),env.policy,env.memory,
        warehouse_service=env.operations,assistant_capability_controller=env.controller)
    return message,wire


def counts(env):
    with closing(sqlite3.connect(env.path)) as connection:
        connection.execute('PRAGMA query_only=ON')
        return tuple(connection.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
            for table in ('warehouse_intake_batches','assistant_action_proposals','warehouse_operations'))


@pytest.mark.parametrize('mode',('local_first','ai_first','shadow'))
@pytest.mark.parametrize('text',(
    'спиши апельсин 200 г, не списывай',
    'спиши апельсин -200 г','спиши апельсин 1,2,3 кг',
    'спиши апельсин 200–300 г',
    'спиши апельсин 200 г\nапельсин -200 г',
    'спиши апельсин 200 г\nапельсин 1,2,3 кг',
    'спиши апельсин 200 г\nотмена',
    'спиши апельсин 200 г стоп','спиши апельсин 200 г отмена','спиши апельсин 200 г скасуй',
))
def test_original_adversarial_repros_through_real_handler_and_db(runtime,mode,text,caplog):
    caplog.set_level(logging.INFO)
    before=counts(runtime)
    message,wire=asyncio.run(send(runtime,text,mode=mode))
    assert counts(runtime)==before==(0,0,0)
    assert wire.calls==0 and not runtime.prepared and runtime.dispatch.call_count==0
    assert message.answer.await_count==1
    context=runtime.memory.get(runtime.owner,runtime.chat)
    assert context is None or not context.authorized_action
    assert text not in caplog.text


@pytest.mark.parametrize('text,quantity',(
    ('спиши не 200, а 300 г апельсина',300000),
    ('спиши апельсин 200 г, нет, 300 г',300000),
    ('спиши апельсин 200 г, он не свежий',200000),
))
@pytest.mark.parametrize('mode',('local_first','ai_first'))
def test_corrected_and_comment_negation_prepare_exactly_one_draft(runtime,text,quantity,mode):
    _,wire=asyncio.run(send(runtime,text,mode=mode))
    assert counts(runtime)==(1,0,0)
    assert len(runtime.prepared)==1 and len(runtime.prepared[0]['lines'])==1
    assert runtime.prepared[0]['lines'][0]['quantity_scaled']==quantity
    assert wire.calls==(1 if mode=='ai_first' else 0)


def test_provider_cannot_override_server_disposition(runtime):
    from app.models.assistant_provider_planner import GroqPlannerRouteEnvelope
    from app.services.assistant.live_evaluation import LiveConversationEvaluationAdapter
    snapshot,caps=LiveConversationEvaluationAdapter(runtime,object())._inputs('спиши апельсин 200 г')
    envelope=GroqPlannerRouteEnvelope.model_validate(route())
    decision=convert_groq_route(envelope,snapshot=snapshot,capabilities=caps,
        arguments={'product':'апельсин','quantity':'200','unit':'г'})
    cancelled=parse_mutation('спиши апельсин 200 г, не списывай')
    from app.services.access_service import AccessDeniedError
    with pytest.raises(AccessDeniedError):
        runtime.controller.dispatch_plan(decision,snapshot=snapshot,catalog=runtime.storage.load(),
            actor_user_id=runtime.owner,source_chat_id=runtime.chat,source_message_id=3,
            chat_type='private',explicit_action='writeoff',mutation_request=cancelled)
    assert counts(runtime)==(0,0,0)


def test_pending_negative_then_valid_quantity_and_restart(runtime):
    asyncio.run(send(runtime,'спиши апельсин'))
    assert counts(runtime)==(0,0,0)
    pending=runtime.memory.get(runtime.owner,runtime.chat).pending_mutation
    assert pending.lines[0].entity=='апельсин'
    runtime.recreate()
    assert runtime.memory.get(runtime.owner,runtime.chat).pending_mutation==pending
    asyncio.run(send(runtime,'-200 г',mode='ai_first',message_id=72))
    assert counts(runtime)==(0,0,0)
    assert runtime.memory.get(runtime.owner,runtime.chat).pending_mutation==pending
    asyncio.run(send(runtime,'200 г',message_id=73))
    assert counts(runtime)==(1,0,0)
    assert runtime.prepared[-1]['lines'][0]['quantity_scaled']==200000
    assert runtime.memory.get(runtime.owner,runtime.chat).pending_mutation is None


def test_pending_transfer_requires_only_destination(runtime):
    asyncio.run(send(runtime,'перемести кокосовое молоко 0.5 л'))
    context=runtime.memory.get(runtime.owner,runtime.chat)
    assert context.pending_mutation.missing==('destination',)
    asyncio.run(send(runtime,'на кухню',message_id=72))
    assert counts(runtime)==(1,0,0)
    assert runtime.prepared[-1]['lines'][0]['quantity_scaled']==500000


def test_missing_unit_requires_explicit_reply_and_preserves_quantity(runtime):
    asyncio.run(send(runtime,'спиши апельсин 200'))
    assert counts(runtime)==(0,0,0)
    assert runtime.memory.get(runtime.owner,runtime.chat).pending_mutation.missing==('unit',)
    asyncio.run(send(runtime,'ведро',message_id=72))
    assert counts(runtime)==(0,0,0)
    asyncio.run(send(runtime,'г',message_id=73))
    assert counts(runtime)==(1,0,0)
    assert runtime.prepared[-1]['lines'][0]['quantity_scaled']==200000


def test_cancel_existing_batch_is_restart_safe_and_followup_cannot_restore(runtime):
    asyncio.run(send(runtime,'спиши апельсин 200 г'))
    batch_id=runtime.prepared[-1]['id']
    asyncio.run(send(runtime,'нет, отмени',mode='ai_first',message_id=72))
    assert runtime.operations.repository.get_batch(batch_id)['status']=='cancelled'
    runtime.recreate()
    assert runtime.operations.pending_batch(actor_user_id=runtime.owner) is None
    assert runtime.memory.get(runtime.owner,runtime.chat) is None
    class Failed:
        calls=0
        async def post_json(self,*args,**kwargs):
            self.calls+=1
            raise TimeoutError
    asyncio.run(send(runtime,'200 г',mode='ai_first',wire=Failed(),message_id=73))
    assert counts(runtime)==(1,0,0)
    assert runtime.operations.repository.get_batch(batch_id)['status']=='cancelled'


def test_reset_and_other_actor_chat_cannot_continue_pending(runtime):
    asyncio.run(send(runtime,'спиши апельсин'))
    pending=runtime.memory.get(runtime.owner,runtime.chat)
    asyncio.run(send(runtime,'200 г',actor=runtime.employee,message_id=72))
    asyncio.run(send(runtime,'-200 г',chat=999,message_id=73))
    asyncio.run(send(runtime,'отмени',chat_type='group',message_id=74))
    assert counts(runtime)==(0,0,0)
    assert runtime.memory.get(runtime.owner,runtime.chat).pending_mutation==pending.pending_mutation
    asyncio.run(send(runtime,'сбрось контекст',message_id=75))
    assert runtime.memory.get(runtime.owner,runtime.chat) is None


def test_known_product_incompatible_unit_is_rejected_before_draft(runtime):
    asyncio.run(send(runtime,'спиши апельсин 200 мл',mode='ai_first'))
    assert counts(runtime)==(0,0,0) and not runtime.prepared


def test_invalid_new_action_disarms_pending_but_keeps_successful_catalog_entity(runtime):
    stable='preparations/_/sea'
    runtime.memory.remember(runtime.owner,chat_id=runtime.chat,last_intent='catalog.get',
        last_entity_type='catalog',last_query='Облепиха',last_entity_id=stable,
        selected_entity_id=stable,last_entity_label='Облепиха')
    asyncio.run(send(runtime,'спиши апельсин'))
    asyncio.run(send(runtime,'спиши кокосовое молоко -200 мл',message_id=72))
    context=runtime.memory.get(runtime.owner,runtime.chat)
    assert context.last_successful_entity_id==stable
    assert context.pending_mutation is None and not context.authorized_action and not context.pending_task
    assert counts(runtime)==(0,0,0)


def test_multiline_preserves_duplicate_lines_and_corrects_one_only(runtime):
    asyncio.run(send(runtime,'спиши апельсин 200 г\nапельсин 0.2 кг, нет, 300 г',mode='ai_first'))
    assert counts(runtime)==(1,0,0)
    assert [line['quantity_scaled'] for line in runtime.prepared[-1]['lines']]==[200000,300000]
    batch=runtime.prepared[-1]
    first=runtime.operations.commit_batch(batch['id'],actor_user_id=runtime.owner)
    from app.storage.warehouse_operations_database import WarehouseOperationsError
    with pytest.raises(WarehouseOperationsError):
        runtime.operations.commit_batch(batch['id'],actor_user_id=runtime.owner)
    assert len(first)==2 and counts(runtime)==(1,0,2)


@pytest.mark.parametrize('text',(
    'спиши апельсин 200 г, не списывай','спиши апельсин -200 г',
    'спиши апельсин 1,2,3 кг',
))
def test_original_repros_via_real_evaluation_ledger_are_safe(text):
    from tests.test_assistant_live_evaluation import settings
    wire=Wire()
    expected=LiveExpectation(preview=False,clarification=None)
    report,code=asyncio.run(run_live(LiveRunOptions('live_isolated',True,1,'gemini',True),
        settings_loader=settings,transport_factory=lambda:wire,
        corpus=(LiveCase(LiveInput('adversarial','adversarial',text),expected),)))
    assert code==0 and report['ready'] and wire.calls==0
    assert report['false_draft_count']==report['false_mutation_count']==0
    assert report['unexpected_state_change_count']==report['operations_before_confirmation']==0
    assert report['rows'][0]['quantity_text'] is None


def test_natural_cancel_keeps_warehouse_fsm_priority(runtime):
    from app.handlers.warehouse import WarehouseNaturalCancelFilter, warehouse_natural_cancel
    from app.handlers.warehouse_states import WarehouseIntakeStates
    async def run():
        await send(runtime,'спиши апельсин 200 г')
        message=SimpleNamespace(from_user=SimpleNamespace(id=runtime.owner),
            chat=SimpleNamespace(id=runtime.chat,type='private'),text='нет, отмени',answer=AsyncMock())
        state=SimpleNamespace(clear=AsyncMock())
        assert await WarehouseNaturalCancelFilter()(message,runtime.operations,WarehouseIntakeStates.comment_text.state)
        await warehouse_natural_cancel(message,state,runtime.operations,runtime.memory)
        state.clear.assert_awaited_once()
        assert runtime.operations.pending_batch(actor_user_id=runtime.owner) is None
    asyncio.run(run())
    assert counts(runtime)==(1,0,0)
