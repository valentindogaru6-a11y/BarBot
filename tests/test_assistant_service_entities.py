"""Service words belong to entity spans, not a second command parser."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from collections import Counter
from decimal import Decimal

import pytest
from pydantic import ValidationError

from tests.assistant_entity_fuzz import ENTITIES,corpus_v13,checksum_v13,V13_SHA256
from tests.test_assistant_compound_cancellation import runtime,counts,ForcedWire
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from app.handlers.assistant import handle_assistant_text
from app.services.assistant.coordinator import AssistantRequestCoordinator
from app.services.assistant.orchestrator import AssistantOrchestrator
from app.services.assistant.planner_providers import GeminiPlannerProvider,MultiProviderPlanner
from app.models.assistant_mutation import MutationSyntax,MutationSpan,MutationCommand
from app.models.warehouse.nomenclature import BaseUnit,ProductOperationType
from app.models.warehouse.operations import WarehouseOperationType
from app.services.assistant import mutation_language as language
from app.services.assistant.mutation_language import parse_mutation,mutation_syntax
from app.services.warehouse.resolution_service import ProductOption,resolve_product
from app.storage.warehouse_database import WarehouseDatabase


def add_product(env,name,action='writeoff',aliases=()):
    return WarehouseDatabase(env.path).create_product(official_name=name,normalized_name=name.casefold(),
        base_unit=BaseUnit.KG if action=='writeoff' else BaseUnit.L,
        aliases=[(alias,alias.casefold()) for alias in aliases],operation_types=set(ProductOperationType),
        is_active=True,actor_user_id=env.owner,stable_id_factory=lambda _:'service_entity')


async def send(env,text,*,mode='local_first',message_id=71,action='writeoff'):
    # The fake provider receives the intended outer action explicitly. Product
    # text must not control the fake's route through substring matching either.
    wire=ForcedWire(action)
    planner=MultiProviderPlanner(GeminiPlannerProvider(wire,api_key='synthetic',model='synthetic'),mode='ai_first')
    orchestrator=AssistantOrchestrator(None,planner_mode=mode,planner_client=planner)
    message=SimpleNamespace(from_user=SimpleNamespace(id=env.owner),chat=SimpleNamespace(id=env.chat,type='private'),
        text=text,message_id=message_id,answer=AsyncMock())
    state=FSMContext(env.fsm,StorageKey(bot_id=7,chat_id=env.chat,user_id=env.owner))
    await handle_assistant_text(message,state,env.storage.path,env.access,orchestrator,
        AssistantRequestCoordinator(),env.policy,env.memory,warehouse_service=env.operations,
        assistant_capability_controller=env.controller)
    return message,wire,state


@pytest.mark.parametrize('name',ENTITIES)
@pytest.mark.parametrize('header',('спиши ','списание: ','списание:\n'))
def test_full_entity_order_repetition_and_roundtrip(name,header):
    text=header+name+' 1 г'
    parsed=parse_mutation(text,exact_entities=(name,))
    assert parsed.allowed and parsed.action=='writeoff' and not parsed.missing
    assert parsed.lines[0].entity==name and parsed.lines[0].quantity==Decimal('1')
    syntax=mutation_syntax(text,exact_entities=(name,))
    assert ' '.join(syntax.normalized_text[s.start:s.end] for s in syntax.entity_spans)==name
    again=parse_mutation(parsed.command(),exact_entities=(name,))
    assert again.allowed and again.lines==parsed.lines
    assert name not in repr(syntax) and 'normalized_text' not in syntax.model_dump()


def test_action_is_extracted_once_per_source_clause_not_body(monkeypatch):
    original=language._action
    calls=[]
    def instrumented(text):
        calls.append(text)
        return original(text)
    monkeypatch.setattr(language,'_action',instrumented)
    parsed=parse_mutation('спиши списание апельсин 1 г')
    assert parsed.lines[0].entity=='списание апельсин'
    assert calls==['спиши списание апельсин 1 г']
    assert 'списание апельсин 1 г' not in calls


def test_ast_is_closed_bounded_and_old_persistent_facts_stay_compatible():
    for cls,kwargs in ((MutationSpan,dict(start=0,end=1)),
        (MutationCommand,dict(action='writeoff',clause_index=0,action_span=dict(start=0,end=5),body_span=dict(start=6,end=12))),
        (MutationSyntax,dict(normalized_text=''))):
        with pytest.raises(ValidationError):cls(**kwargs,provider_role='entity')
    parsed=parse_mutation('спиши списание апельсин 1')
    assert 'syntax' not in parsed.model_dump()
    assert parse_mutation('г',previous=type(parsed).model_validate(parsed.model_dump())).lines[0].entity=='списание апельсин'


def test_comment_numbers_and_destinations_do_not_change_argument_spans():
    text='перемести перемещение лимон 1 л на кухню (комментарий: передано 200 мл на кухню)'
    syntax=mutation_syntax(text)
    assert len(syntax.quantity_spans)==len(syntax.unit_spans)==1
    assert ' '.join(syntax.normalized_text[s.start:s.end] for s in syntax.entity_spans)=='перемещение лимон'
    assert ' '.join(syntax.normalized_text[s.start:s.end] for s in syntax.destination_spans)=='кухню'
    assert syntax.comment_spans


@pytest.mark.parametrize('case',corpus_v13(),ids=lambda c:c['case_id'])
def test_frozen_v13_parser_metamorphic_cases(case):
    p=parse_mutation(case['text'],exact_entities=(case['entity'],))
    assert p is not None
    if case['expected']=='blocked':
        assert not p.allowed and not p.lines
        return
    assert p.allowed and p.action==case['action'] and len(p.lines)==1
    assert p.lines[0].entity==case['entity']
    assert p.lines[0].quantity==Decimal(case['quantity']+(case['expected']=='corrected'))
    if case['expected']=='partial':
        assert p.missing==('unit',)
        reply=parse_mutation(case['unit'],previous=type(p).model_validate(p.model_dump()))
        assert reply.allowed and not reply.missing and reply.lines[0].entity==case['entity']
    else:
        assert not p.missing and p.lines[0].unit==case['unit']


def test_v13_shape_checksum_distribution():
    cases=corpus_v13()
    assert len(cases)==len({c['text'] for c in cases})==360
    assert set(c['variant'] for c in cases)==set(range(12))
    assert Counter(c['mode'] for c in cases)=={'local_first':180,'ai_first':180}
    assert checksum_v13()==V13_SHA256
    distribution=Counter(str(parse_mutation(c['text'],exact_entities=(c['entity'],)).disposition) for c in cases)
    print('fuzz v1.3',dict(distribution),'sha256',checksum_v13())


@pytest.mark.parametrize('mode',('local_first','ai_first'))
@pytest.mark.parametrize('name',ENTITIES)
@pytest.mark.parametrize('header',('спиши ','списание:\n'))
def test_real_handler_selects_exact_full_product_never_shorter(runtime,mode,name,header):
    product=add_product(runtime,name)
    with runtime.business.observe() as ledger:
        _,wire,_=asyncio.run(send(runtime,header+name+' 1 г',mode=mode))
    batch=runtime.operations.pending_batch(actor_user_id=runtime.owner)
    assert counts(runtime)==(1,0,0) and batch['lines'][0]['product_id']==product.id
    assert batch['lines'][0]['quantity_scaled']==1000
    assert sum(e.table=='warehouse_intake_lines' and e.verb=='insert' for e in ledger)==1
    if mode=='local_first':assert wire.calls==0


@pytest.mark.parametrize('mode',('local_first','ai_first'))
def test_real_transfer_service_name_and_restart_missing_unit(runtime,mode):
    product=add_product(runtime,'Перемещение лимон',action='transfer')
    asyncio.run(send(runtime,'перемести перемещение лимон 1 на кухню',mode=mode,action='transfer'))
    assert counts(runtime)==(0,0,0)
    before=runtime.memory.get(runtime.owner,runtime.chat).pending_mutation
    runtime.recreate()
    assert runtime.memory.get(runtime.owner,runtime.chat).pending_mutation==before
    asyncio.run(send(runtime,'л',mode=mode,message_id=902,action='transfer'))
    batch=runtime.operations.pending_batch(actor_user_id=runtime.owner)
    assert counts(runtime)==(1,0,0) and batch['lines'][0]['product_id']==product.id
    assert batch['lines'][0]['destination_location_id'] is not None


@pytest.mark.parametrize('resolution',('alias','missing'))
def test_alias_full_name_and_missing_full_name_do_not_shorten(runtime,resolution):
    product=add_product(runtime,'Полный рецепт',aliases=('списание апельсин',)) if resolution=='alias' else None
    asyncio.run(send(runtime,'спиши списание апельсин 1 г'))
    batch=runtime.operations.pending_batch(actor_user_id=runtime.owner)
    assert counts(runtime)==(1,0,0)
    if product:
        assert batch['lines'][0]['product_id']==product.id
    else:
        assert batch['status']=='needs_resolution' and batch['lines'][0]['product_id'] is None
        assert batch['lines'][0]['product_text']=='списание апельсин'


def test_alias_collision_requires_choice_official_full_name_wins():
    a=ProductOption(1,'Апельсин','апельсин',BaseUnit.KG,('списание апельсин',),frozenset({'product_writeoff'}))
    b=ProductOption(2,'Списание апельсин','списание апельсин',BaseUnit.KG,(),a.operation_types)
    assert resolve_product('списание апельсин',WarehouseOperationType.WRITEOFF,(a,b)).exact_id==2
    b=ProductOption(2,'Полный рецепт','полный рецепт',BaseUnit.KG,a.aliases,a.operation_types)
    result=resolve_product('списание апельсин',WarehouseOperationType.WRITEOFF,(a,b))
    assert result.exact_id is None and len(result.candidates)==2


@pytest.mark.parametrize('hostile',('product','target'))
def test_provider_cannot_replace_full_entity_or_bind_short_id(runtime,hostile):
    from app.models.assistant_provider_planner import GroqPlannerRouteEnvelope
    from app.models.assistant_planner import PlannerTargetOption
    from app.services.assistant.live_evaluation import LiveConversationEvaluationAdapter
    from app.services.assistant.planner_providers import convert_groq_route
    from app.services.access_service import AccessDeniedError
    add_product(runtime,'Списание апельсин')
    text='спиши списание апельсин 1 г'
    snapshot,caps=LiveConversationEvaluationAdapter(runtime,object())._inputs(text)
    snapshot=snapshot.model_copy(update={'target_options':(PlannerTargetOption(stable_id='orange',
        display_name='Апельсин',entity_kind='warehouse_product',relation_label='candidate',domain='warehouse'),)})
    wire=GroqPlannerRouteEnvelope.model_validate(dict(dialogue_act='new_command',domain='warehouse',
        capability_id='warehouse.prepare_writeoff',target_index=0 if hostile=='target' else -1,
        action_code='create',needs_clarification=False,confidence_scaled=10000))
    decision=convert_groq_route(wire,snapshot=snapshot,capabilities=caps,
        arguments=dict(product='апельсин' if hostile=='product' else 'списание апельсин',quantity='1',unit='г'))
    with runtime.business.observe() as ledger,pytest.raises(AccessDeniedError):
        runtime.controller.dispatch_plan(decision,snapshot=snapshot,catalog=runtime.storage.load(),
            actor_user_id=runtime.owner,source_chat_id=runtime.chat,source_message_id=903,chat_type='private',
            explicit_action='writeoff',mutation_request=parse_mutation(text))
    assert not ledger and counts(runtime)==(0,0,0)


def test_registry_renders_server_facts_without_another_action_pass(runtime,monkeypatch):
    from app.services.assistant.orchestrator import AssistantToolRegistry
    parsed=parse_mutation('спиши списание апельсин 1 г')
    def forbidden(_):raise AssertionError('typed body was reparsed')
    monkeypatch.setattr(language,'_action',forbidden)
    action=AssistantToolRegistry(runtime.storage.load()).execute(
        AssistantOrchestrator.mutation_intent(parsed),mutation_request=parsed)
    assert action.raw_intake==parsed.command()


@pytest.mark.parametrize('ending',('success','exception','cancellation'))
def test_bound_facts_are_exact_task_local_and_always_released(ending):
    from app.services.assistant.mutation_language import isolated_mutation_facts,bind_mutation_facts
    records=[]
    @isolated_mutation_facts
    async def worker(name):
        parsed=parse_mutation('спиши '+name+' 1 г',exact_entities=(name,))
        records.append(parsed)
        bind_mutation_facts(parsed)
        await asyncio.sleep(0)
        assert parse_mutation(parsed.command()) is parsed
        for control in ('\x1c','\x1f','\u200b','\ufeff'):
            assert not parse_mutation(parsed.command()+control).allowed
            assert not parse_mutation(control+parsed.command()).allowed
        changed=parse_mutation('спиши апельсин 2 г')
        assert changed is not parsed and changed.lines[0].entity=='апельсин'
        if ending=='exception':raise RuntimeError('synthetic')
        if ending=='cancellation':raise asyncio.CancelledError
    async def run():
        results=await asyncio.gather(worker('не списывать'),worker('перемести нектар'),return_exceptions=True)
        assert all((r is None if ending=='success' else isinstance(r,BaseException)) for r in results)
    asyncio.run(run())
    for record in records:
        assert parse_mutation(record.command()) is not record
    with pytest.raises(ValueError):language.bind_mutation_facts(records[0])


@pytest.mark.parametrize('case',corpus_v13()[:24],ids=lambda c:c['case_id'])
def test_v13_vertical_variants_modes_alias_missing_compound_and_restart(runtime,case):
    product=(None if case['resolution']=='missing' else add_product(runtime,
        'Полный рецепт' if case['resolution']=='alias' else case['entity'],case['action'],
        aliases=(case['entity'],) if case['resolution']=='alias' else ()))
    with runtime.business.observe() as ledger:
        asyncio.run(send(runtime,case['text'],mode=case['mode'],action=case['action']))
    if case['expected']=='blocked':
        assert counts(runtime)==(0,0,0) and not ledger
        return
    if case['expected']=='partial':
        assert counts(runtime)==(0,0,0)
        saved=runtime.memory.get(runtime.owner,runtime.chat).pending_mutation
        if case['restart']:
            runtime.recreate()
            assert runtime.memory.get(runtime.owner,runtime.chat).pending_mutation==saved
        asyncio.run(send(runtime,case['unit'],mode=case['mode'],action=case['action'],message_id=905))
    batch=runtime.operations.pending_batch(actor_user_id=runtime.owner)
    assert counts(runtime)==(1,0,0)
    if product:
        assert batch['lines'][0]['product_id']==product.id
        assert batch['lines'][0]['quantity_scaled']==(case['quantity']+(case['expected']=='corrected'))*(1000 if case['unit']=='г' else 1000000)
    else:
        assert batch['status']=='needs_resolution' and batch['lines'][0]['product_id'] is None
        assert batch['lines'][0]['product_text']==case['entity']
