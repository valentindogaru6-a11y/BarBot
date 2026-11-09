"""Synthetic Unicode/slot invariants; real temporary v15 FSM/service/ledger."""
import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.assistant_mutation_fuzz import corpus_v11, corpus_v12, V11_SHA256
from tests.test_assistant_compound_cancellation import runtime, send, counts
from app.handlers.warehouse import WarehouseNaturalCancelFilter, warehouse_natural_cancel
from app.handlers.warehouse_states import WarehouseIntakeStates
from app.models.assistant_mutation import MutationDisposition as D, MutationTokenKind as T, MutationError as E
from app.models.warehouse.nomenclature import BaseUnit, ProductOperationType
from app.services.assistant.mutation_language import parse_mutation, cancellation_analysis
from app.services.assistant.mutation_tokens import ClauseTokens, inspect_unicode
from app.services.assistant.evaluation_business_state import unexpected_changes
from app.services.assistant.evaluation_effects import EffectPolicy
from app.storage.warehouse_database import WarehouseDatabase
from app.storage.warehouse_operations_database import WarehouseOperationsError, WarehouseRevisionConflict


def accepted(case, parsed):
    if parsed is None:
        return False
    expected = case['expected']
    if expected == 'blocked':
        return not parsed.allowed and parsed.disposition in (D.CANCEL,D.INVALID,D.AMBIGUOUS)
    if expected == 'partial':
        return parsed.allowed and bool(parsed.missing)
    return (parsed.allowed and not parsed.missing and len(parsed.lines) == 1
        and (expected != 'corrected' or parsed.disposition == D.CORRECTED))


def test_frozen_v11_unchanged_and_all_74_old_failures_removed():
    cases = corpus_v11()  # generator asserts the independently pinned digest
    assert len(cases) == 1410 and len({c['text'] for c in cases}) == 1372
    assert V11_SHA256 == '61625f501c4f6387bbf0c5ab5b1ec51597ffef53a6f3224526d24488a6572625'
    distribution = Counter()
    violations = []
    for c in cases:
        p = parse_mutation(c['text'])
        distribution[str(p.disposition) if p else 'none'] += 1
        if not accepted(c,p):
            violations.append(c['case_id'])
    assert not violations, violations
    assert sum(distribution.values()) == 1410
    print('fuzz v1.1:',dict(distribution),'violations:',len(violations))


def test_extension_has_distinct_unicode_entity_and_context_cases():
    cases = corpus_v12()
    assert len(cases) == 410
    for family,size in (('unicode',210),('entity',100)):
        group = [c for c in cases if c['family']==family]
        assert len(group) == len({c['text'] for c in group}) == size
    contexts = [c for c in cases if c['family']=='fsm']
    assert len(contexts) == len({c['initial_quantity'] for c in contexts}) == 100


@pytest.mark.parametrize('case',[c for c in corpus_v12() if c['family'] != 'fsm'],ids=lambda c:c['case_id'])
def test_v12_unicode_and_service_entities(case):
    parsed = parse_mutation(case['text'])
    assert accepted(case,parsed)
    if case['family'] == 'unicode':
        assert parsed.disposition == D.INVALID and not parsed.lines
        assert parsed.error == E.UNICODE
    else:
        assert parsed.disposition == D.PROCEED
        assert parsed.lines[0].quantity is not None


@pytest.mark.parametrize('word',('отмени','спиши','исправь'))
@pytest.mark.parametrize('control',('\u200b','\u200c','\u200d','\ufeff','\u2060','\xad',
    '\u202e','\u2066','\ufe0f','\U000e0100','\x00','\x1b','\u034f','\u0301'))
def test_control_in_every_service_word_position_never_proceeds(word,control):
    for index in range(len(word)+1):
        corrupted = word[:index]+control+word[index:]
        parsed = parse_mutation('спиши апельсин 1 г, '+corrupted)
        assert parsed is not None and not parsed.allowed
        assert inspect_unicode(corrupted).forbidden


def test_unicode_detection_only_safe_metadata_and_spaces():
    text = 'спиши апельсин 1 г, отм\u200bени'
    inspected = inspect_unicode(text)
    assert inspected.forbidden and 'отмени' in inspected.detection_text
    assert '\u200b' in inspected.normalized_text  # never silently repaired
    assert 'text' not in ' '.join(inspected.model_dump())
    assert 'апельсин' not in repr(inspected)
    assert 'private comment' not in repr(ClauseTokens((), 'private comment'))
    for space in (' ','\u00a0','\u2009'):
        assert parse_mutation(space.join(('спиши','стоп','1','г'))).allowed
    assert not inspect_unicode('спиши йогурт 1 г').forbidden
    assert not inspect_unicode('спиши и\u0306огурт 1 г').forbidden  # canonical composition
    assert inspect_unicode('спиши апельсин 1 г, отмeни').forbidden
    assert inspect_unicode('спиши апельсин 1 г, отм ени').forbidden
    assert inspect_unicode('спиши апельсин 1 г, не о тм ени').forbidden
    for code in (0x115F,0x1160,0x3164,0xFFA0,0x2065,0xFFF0,0xE0000):
        assert inspect_unicode('спиши апельсин 1 г'+chr(code)).forbidden


def test_positional_roles_keep_quantities_and_protect_only_entity_slots():
    text = 'спиши стоп 1 г, стоп'
    analysis = cancellation_analysis(text,contextual=True)
    tokens = analysis.tokens
    assert [(t.normalized_value,t.kind) for t in tokens] == [
        ('спиши',T.ACTION),('стоп',T.ENTITY),('1',T.QUANTITY),('г',T.UNIT),('стоп',T.CANCEL)]
    assert analysis.disposition == D.CANCEL
    for t in tokens:
        assert text[t.start:t.end] == t.normalized_value
        assert 'normalized_value' not in t.model_dump()
    assert parse_mutation('спиши стоп 1').missing == ('unit',)
    assert parse_mutation('перемести стоп 1 л').missing == ('destination',)
    assert parse_mutation('спиши напиток стоп-кран 1 г').allowed
    assert parse_mutation('спиши апельсин 1 г (причина: стоп-лист)').allowed
    assert parse_mutation('спиши 1 г стоп').disposition == D.AMBIGUOUS
    exact = parse_mutation('спиши 1 г стоп',exact_entities=('стоп',))
    assert exact.allowed and exact.lines[0].entity == 'стоп'
    assert parse_mutation('спиши 1 г стоп, стоп',exact_entities=('стоп',)).disposition == D.CANCEL
    assert parse_mutation('спиши апельсин не списывай 1 г').disposition == D.CANCEL
    assert parse_mutation('спиши апельсин не отменяй 1 г').disposition == D.AMBIGUOUS


def add_stop(env):
    return WarehouseDatabase(env.path).create_product(official_name='Стоп',normalized_name='стоп',
        base_unit=BaseUnit.KG,aliases=[],operation_types=set(ProductOperationType),
        is_active=True,actor_user_id=env.owner,stable_id_factory=lambda _:'stop-product')


def message(env,text,actor=None,chat=None):
    return SimpleNamespace(from_user=SimpleNamespace(id=env.owner if actor is None else actor),
        chat=SimpleNamespace(id=env.chat if chat is None else chat,type='private'),
        text=text,answer=AsyncMock())


@pytest.mark.parametrize('mode',('local_first','ai_first'))
@pytest.mark.parametrize('text',('спиши апельсин 1 г, отм\u200bени',
    'спи\u200bши апельсин 1 г','спиши апел\u200bьсин 1 г',
    'спиши апельсин 2\u200b00 г','спиши апельсин 1 г\u200bрамм',
    'спиши апельсин 1 г (испор\u200bтился)', 'спиши апельсин 1 г, стоп'))
def test_vertical_blocked_handler_and_adversarial_provider_zero_ledger(runtime,mode,text):
    before = runtime.business.snapshot()
    with runtime.business.observe() as ledger:
        msg,wire,_ = asyncio.run(send(runtime,text,mode=mode))
    assert counts(runtime) == (0,0,0) and wire.calls == runtime.prepare.call_count == 0
    assert not unexpected_changes(before,runtime.business.snapshot(),ledger,EffectPolicy())
    assert not [e for e in ledger if e.table.startswith('warehouse_') or e.table == 'assistant_action_proposals']
    assert msg.answer.await_count == 1


@pytest.mark.parametrize('mode',('local_first','ai_first'))
def test_exact_stop_creates_one_canonical_draft_only(runtime,mode):
    product = add_stop(runtime)
    _,wire,_ = asyncio.run(send(runtime,'спиши стоп 1 г',mode=mode))
    batch = runtime.operations.pending_batch(actor_user_id=runtime.owner)
    assert batch['lines'][0]['product_id'] == product.id
    assert batch['lines'][0]['quantity_scaled'] == 1000
    assert counts(runtime) == (1,0,0)
    # ai_first may route an already validated safe entity through its fake
    # planner; only suspicious/ambiguous input must veto all provider attempts.
    assert wire.calls == (1 if mode == 'ai_first' else 0)


@pytest.mark.parametrize('case',[c for c in corpus_v12() if c['family']=='fsm'],ids=lambda c:c['case_id'])
def test_v12_real_fsm_roles_ownership_restart_and_ledger(runtime,case):
    add_stop(runtime)
    async def scenario():
        _,_,state = await send(runtime,f"спиши апельсин {case['initial_quantity']} г")
        await state.set_state(getattr(WarehouseIntakeStates,case['initial_state']))
        batch = runtime.operations.pending_batch(actor_user_id=runtime.owner)
        if case['restart']:
            runtime.recreate()
        actor = runtime.employee if case['actor']=='other' else runtime.owner
        chat = 9 if case['actor']=='other_chat' else runtime.chat
        msg = message(runtime,case['text'],actor,chat)
        before = runtime.business.snapshot()
        selected = await WarehouseNaturalCancelFilter()(msg,runtime.operations,await state.get_state())
        with runtime.business.observe() as ledger:
            if selected:
                await warehouse_natural_cancel(msg,state,runtime.operations,runtime.memory)
        current = runtime.operations.repository.get_batch(batch['id'])
        own = case['actor']=='owner'
        cancel = own and parse_mutation(case['text']).disposition == D.CANCEL
        if cancel:
            assert current['status'] == 'cancelled'
            assert counts(runtime) == (1,0,0)
        else:
            assert current == batch
            assert not unexpected_changes(before,runtime.business.snapshot(),ledger,EffectPolicy())
            assert not [e for e in ledger if e.table.startswith('warehouse_')]
        if not own:
            assert not selected
        if own and case['text'] == 'спиши стоп 1':
            assert 'единицу' in msg.answer.call_args.args[0]
            assert await state.get_state() == WarehouseIntakeStates.command_clarification.state
            saved = runtime.memory.get(runtime.owner,runtime.chat).pending_mutation
            runtime.recreate()
            assert runtime.memory.get(runtime.owner,runtime.chat).pending_mutation == saved
            reply = message(runtime,'г')
            assert await WarehouseNaturalCancelFilter()(reply,runtime.operations,await state.get_state())
            with runtime.business.observe() as later:
                await warehouse_natural_cancel(reply,state,runtime.operations,runtime.memory)
            assert runtime.operations.repository.get_batch(batch['id']) == batch
            assert not [e for e in later if e.table.startswith('warehouse_')]
    asyncio.run(scenario())


def test_cancel_snapshot_chat_expiry_role_and_idempotency(runtime):
    asyncio.run(send(runtime,'спиши апельсин 1 г'))
    batch = runtime.operations.pending_batch(actor_user_id=runtime.owner)
    version = runtime.operations.repository.cancellation_version(batch)
    kwargs = dict(actor_user_id=runtime.owner,source_chat_id=runtime.chat,expected_version=version)
    with pytest.raises(WarehouseOperationsError):
        runtime.operations.cancel_batch(batch['id'],**{**kwargs,'source_chat_id':9})
    runtime.operations.update_intake_line_comment(batch['id'],batch['lines'][0]['id'],comment='причина',actor_user_id=runtime.owner)
    with pytest.raises(WarehouseRevisionConflict):
        runtime.operations.cancel_batch(batch['id'],**kwargs)
    fresh = runtime.operations.pending_batch(actor_user_id=runtime.owner)
    kwargs['expected_version'] = runtime.operations.repository.cancellation_version(fresh)
    old_clock = runtime.operations.clock
    runtime.operations.clock = SimpleNamespace(now=lambda:datetime.now(UTC)+timedelta(days=2))
    with pytest.raises(WarehouseOperationsError):
        runtime.operations.cancel_batch(batch['id'],**kwargs)
    runtime.operations.clock = old_clock
    cancelled = runtime.operations.cancel_batch(batch['id'],**kwargs)
    with runtime.business.observe() as ledger:
        assert runtime.operations.cancel_batch(batch['id'],**kwargs) == cancelled
    assert not ledger and counts(runtime) == (1,0,0)


def test_direct_provider_cannot_upgrade_unicode_to_mutation(runtime):
    from tests.test_assistant_compound_cancellation import test_planned_dispatch_cannot_override_blocked_local_clause
    test_planned_dispatch_cannot_override_blocked_local_clause(runtime,'спиши апельсин 1 г, отм\u200bени')


def test_cancel_rechecks_revoked_role_and_concurrent_replay(runtime):
    from concurrent.futures import ThreadPoolExecutor
    from app.services.access_service import AccessDeniedError
    from app.models.user import Role
    batch = runtime.operations.prepare_batch(actor_user_id=runtime.employee,
        source_chat_id=runtime.employee,source_message_id=801,raw_text='списание:\nапельсин 1 г')
    version = runtime.operations.repository.cancellation_version(batch)
    kwargs = dict(actor_user_id=runtime.employee,source_chat_id=runtime.employee,expected_version=version)
    runtime.users.set_role(runtime.employee,Role.USER,actor_user_id=runtime.owner)
    with runtime.business.observe() as denied, pytest.raises(AccessDeniedError):
        runtime.operations.cancel_batch(batch['id'],**kwargs)
    assert not denied and runtime.operations.repository.get_batch(batch['id']) == batch
    runtime.users.set_role(runtime.employee,Role.BARTENDER,actor_user_id=runtime.owner)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _:runtime.operations.cancel_batch(batch['id'],**kwargs),range(2)))
    assert results[0] == results[1] and results[0]['status'] == 'cancelled'
    assert counts(runtime) == (1,0,0)


@pytest.mark.parametrize('field',('entity','comment','destination'))
@pytest.mark.parametrize('mode',('local_first','ai_first'))
def test_restart_cannot_resume_legacy_typed_unicode_pending(runtime,field,mode):
    from app.models.assistant_mutation import ParsedMutation
    base = parse_mutation('спиши апельсин 1')
    line = base.lines[0].model_copy(update={field:'отм\u200bени'})
    previous = ParsedMutation.model_validate({**base.model_dump(),'lines':[line.model_dump()]})
    assert previous.clauses  # not merely the old untyped-comment guard
    runtime.memory.remember(runtime.owner,chat_id=runtime.chat,last_intent='prepare_writeoff',
        last_entity_type='warehouse_product',last_query=line.entity,authorized_action='prepare_writeoff',
        pending_slots=('unit',),pending_mutation=previous)
    runtime.recreate()
    before = runtime.business.snapshot()
    with runtime.business.observe() as ledger:
        _,wire,_ = asyncio.run(send(runtime,'г',mode=mode))
    assert counts(runtime) == (0,0,0) and wire.calls == 0
    assert not unexpected_changes(before,runtime.business.snapshot(),ledger,EffectPolicy())
    assert runtime.memory.get(runtime.owner,runtime.chat).pending_mutation is None
    assert parse_mutation('спиши стоп 1 г',previous=previous).allowed
