"""Real executors, owned v15 DB and fake transports only; no production I/O."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from contextvars import copy_context
from dataclasses import FrozenInstanceError
import hashlib
import json
import sqlite3
import threading

import pytest
from app.services.assistant import read_only as ro

from app.models.assistant_planner import PlannerDecision, PlannerContextSnapshot
from app.services.assistant.capabilities import CAPABILITIES
from app.services.assistant.evaluation_business_state import unexpected_changes
from app.services.assistant.evaluation_effects import READ_ONLY
from app.services.assistant.evaluation_environment import IsolatedEvaluationEnvironment
from app.services.assistant.read_only import read_only_execution, ReadOnlyViolation, read_only_failure
from app.services.assistant.providers import HttpResponse


@pytest.fixture
def env():
    with IsolatedEvaluationEnvironment() as e:
        e.configure('extended')
        yield e


def dispatch(e, identifier, arguments=None, target=None):
    definition=e.registry.require(identifier)
    domain={'navigation':'general','help':'general','recipients':'administration',
        'reminders':'administration','schedules':'administration'}.get(definition.category,definition.category)
    snapshot=PlannerContextSnapshot(actor_role='super_admin',
        available_capability_ids=e.controller.available_ids(e.owner),
        candidate_entity_ids=(target,) if target else ())
    decision=PlannerDecision(dialogue_act='new_request',domain=domain,capability_id=identifier,
        target_entity_id=target,arguments=arguments or {},confidence=10000,needs_clarification=False,
        clarification_question=None,read_only=int(definition.risk_level)==0,
        mutation=int(definition.risk_level)!=0,context_updates={})
    return e.controller.dispatch_plan(decision,snapshot=snapshot,catalog=e.storage.load(),
        actor_user_id=e.owner,source_chat_id=e.chat,source_message_id=1,chat_type='private')


def artifacts(e):
    return {str(p.relative_to(e.root)):hashlib.sha256(p.read_bytes()).hexdigest()
        for p in e.root.rglob('*') if p.is_file()}


READS=tuple(d.id for d in CAPABILITIES if int(d.risk_level)==0)


@pytest.mark.parametrize('identifier',READS)
def test_all_bound_owner_read_executors_are_effect_free(env,identifier):
    e=env
    # The dynamic enumeration is checked against the parametrized set, so a new
    # bound owner-readable capability cannot silently miss this fixture audit.
    actual={d.id for d in e.policy.available_capabilities(e.owner)
        if int(d.risk_level)==0 and d.id in e.registry.bindings}
    assert actual==set(READS)
    target=None;args={}
    if identifier=='catalog.search':args={'query':'Облепиха'}
    if identifier=='catalog.get':target='preparations/_/sea'
    if identifier=='users.get':args={'query':'Дмитро'}
    if identifier=='attendance.get':
        target=str(e.attendance.open_sessions(e.owner)[0]['id']);args={'entity_ref':target}
    if identifier=='reports.status':
        target=str(e.reports.history(actor_user_id=e.owner,page=0)[0][0]['id']);args={'entity_ref':target}
    before=e.business.snapshot();files=artifacts(e)
    with e.business.observe() as ledger:
        result=dispatch(e,identifier,args,target)
    assert result.capability_id==identifier
    assert not ledger
    assert before==e.business.snapshot()
    assert artifacts(e)==files
    assert e.observer.counts()==(0,0)
    assert ro._ACTIVE.get() is None and ro._BYTECODE_USERS==0 and not ro._OPENING.get()
    # Exercise the real following writer after the zero-effect read checks.
    e.closing.start(actor_user_id=e.owner)


def test_P1_status_repeat_reopen_then_explicit_start(env):
    e=env
    before=e.business.snapshot()
    with e.business.observe() as ledger:
        a=dispatch(e,'warehouse.required_status')
        b=dispatch(e,'warehouse.required_status')
        snap=e.closing.get_required_status_snapshot(actor_user_id=e.owner)
    assert a.data==b.data=={'total':2,'resolved':0}
    assert not ledger and before==e.business.snapshot()
    assert all(not row.materialized and row.status=='pending' for row in snap.requirements)
    with pytest.raises(FrozenInstanceError):snap.writeoffs=7
    with pytest.raises(TypeError):snap.shift['status']='closed'
    e.recreate()
    assert dispatch(e,'warehouse.required_status').data==a.data
    with e.business.observe() as ledger:
        preview=dispatch(e,'warehouse.start_required_flow')
    assert preview.ui_action=='required_writeoffs' and not ledger
    session=e.closing.start(actor_user_id=e.owner)
    again=e.closing.start(actor_user_id=e.owner)
    assert session['id']==again['id']
    before=e.business.snapshot()
    with e.business.observe() as ledger:
        assert dispatch(e,'warehouse.required_status').data==a.data
    assert not ledger and before==e.business.snapshot()
    with closing(sqlite3.connect(e.path)) as c:
        assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert not c.execute('PRAGMA foreign_key_check').fetchall()


@pytest.mark.parametrize('sql',[
    "INSERT INTO warehouse_shift_requirements SELECT * FROM warehouse_shift_requirements",
    'UPDATE users SET is_active=0', 'DELETE FROM users', 'CREATE TABLE injected(a)',
    'DROP TABLE users','PRAGMA user_version=1','PRAGMA query_only=OFF',
    'PRAGMA journal_mode=DELETE','PRAGMA wal_checkpoint',"ATTACH DATABASE ':memory:' AS other",
])
def test_sql_guard_blocks_real_nested_helpers_and_records_denied_attempt(env,sql):
    before=env.business.snapshot()
    with env.business.observe() as ledger, pytest.raises(ReadOnlyViolation) as error:
        with read_only_execution('warehouse.required_status'):
            with closing(sqlite3.connect(env.path)) as c:
                try:c.execute(sql)
                except sqlite3.Error:pass  # swallowing SQLite's denial cannot hide it
    assert error.value.category=='sqlite_write'
    assert ledger and all(event.changes==0 for event in ledger)
    assert env.business.snapshot()==before
    assert sql not in str(error.value)
    # No query_only state leaks to the next legitimate write connection.
    env.closing.start(actor_user_id=env.owner)


@pytest.mark.parametrize('kind',['file','audit','directory','network','thread'])
def test_guard_blocks_non_sql_effects(env,kind):
    import socket
    target=env.root/'forbidden'
    with pytest.raises(ReadOnlyViolation):
        with read_only_execution('catalog.search'):
            if kind=='file':target.write_text('not written')
            elif kind=='audit':env.admin.audit._append({'event':'synthetic'})
            elif kind=='directory':target.mkdir()
            elif kind=='network':socket.getaddrinfo('invalid.example',443)
            else:threading.Thread(target=lambda:None).start()
    assert not target.exists()


def test_executor_injected_write_is_blocked_not_only_observed(env,monkeypatch):
    before=env.business.snapshot()
    def defective(**kwargs):
        with closing(sqlite3.connect(env.path)) as c:c.execute('UPDATE users SET is_active=0')
    monkeypatch.setattr(env.closing,'get_required_status_snapshot',defective)
    with env.business.observe() as ledger, pytest.raises(BaseExceptionGroup) as caught:
        dispatch(env,'warehouse.required_status')
    assert isinstance(read_only_failure(caught.value),ReadOnlyViolation)
    assert any(isinstance(error,sqlite3.Error) for error in caught.value.exceptions)
    assert ledger and env.business.snapshot()==before
    assert unexpected_changes(before,env.business.snapshot(),ledger,READ_ONLY)


def test_legacy_local_status_uses_the_same_boundary(env,monkeypatch):
    def bad(**kwargs):
        with closing(sqlite3.connect(env.path)) as c:c.execute('UPDATE users SET is_active=0')
    monkeypatch.setattr(env.closing,'overview',bad)
    with pytest.raises(BaseExceptionGroup) as caught:
        env.controller.dispatch('Покажи обязательные списания',actor_user_id=env.owner,
            source_chat_id=env.chat,source_message_id=1)
    assert isinstance(read_only_failure(caught.value),ReadOnlyViolation)


def test_local_catalog_registry_cannot_create_artifacts(env,monkeypatch):
    from app.models.assistant import AssistantIntent
    from app.services.assistant.orchestrator import AssistantToolRegistry
    registry=AssistantToolRegistry(env.storage.load())
    def bad(*args,**kwargs):(env.root/'forbidden').write_text('not written')
    monkeypatch.setattr(registry.retriever,'retrieve',bad)
    with pytest.raises(ReadOnlyViolation):
        registry.execute(AssistantIntent(intent='find_tech_card',query='Облепиха'))
    assert not (env.root/'forbidden').exists()


def test_concurrent_status_and_start_are_consistent(env):
    def status():
        return env.closing.get_required_status_snapshot(actor_user_id=env.owner)
    # Keep SQLite's own WAL/SHM alive during overlap. The owned-path verifier
    # intentionally fails closed if a last-close removes an auxiliary between
    # its lstat probes; this fixture must not confuse that with SQL consistency.
    with closing(sqlite3.connect(env.path)) as pin, ThreadPoolExecutor(max_workers=3) as pool:
        pin.execute('SELECT count(*) FROM users').fetchone()
        a=pool.submit(copy_context().run,lambda:dispatch(env,'warehouse.required_status'))
        b=pool.submit(copy_context().run,lambda:dispatch(env,'warehouse.required_status'))
        assert a.result().data==b.result().data=={'total':2,'resolved':0}
        a=pool.submit(copy_context().run,status)
        b=pool.submit(copy_context().run,lambda:env.closing.start(actor_user_id=env.owner))
        snap=a.result();b.result()
        assert len(snap.requirements)==2
        assert len({r.materialized for r in snap.requirements})==1


def test_virtual_shift_and_pending_rules_do_not_initialize_anything(env):
    from datetime import UTC,datetime,timedelta
    class Future:
        def now(self):return datetime.now(UTC)+timedelta(days=14)
    env.closing.clock=Future()
    before=env.business.snapshot()
    with env.business.observe() as ledger, read_only_execution('warehouse.required_status'):
        snap=env.closing.get_required_status_snapshot(actor_user_id=env.owner)
    assert snap.shift['id'] is None and len(snap.requirements)==2
    assert not ledger and before==env.business.snapshot()


def test_status_during_uncommitted_closing_reads_consistent_snapshot(env):
    with env.closing.repository._transaction() as c:
        shift=env.operations.current_shift(actor_user_id=env.owner)
        env.closing.repository._ensure_snapshot(c,c.execute('SELECT * FROM warehouse_shifts WHERE id=?',(shift['id'],)).fetchone(),'2026-01-01T00:00:00Z')
        snapshot=env.closing.get_required_status_snapshot(actor_user_id=env.owner)
        assert all(not row.materialized for row in snapshot.requirements)
    assert all(row.materialized for row in env.closing.get_required_status_snapshot(actor_user_id=env.owner).requirements)


def test_cancel_timeout_task_creation_and_guard_release(env):
    async def run():
        # Executors are synchronous; cancellation/timeout injected at the tool
        # boundary must unwind exactly as the surrounding async handler does.
        for error in (asyncio.CancelledError(),TimeoutError()):
            with pytest.raises(type(error)):
                with read_only_execution('catalog.search'):raise error
        with pytest.raises(ReadOnlyViolation):
            with read_only_execution('catalog.search'):asyncio.create_task(asyncio.sleep(0))
        with pytest.raises(ReadOnlyViolation):
            with read_only_execution('catalog.search'):asyncio.get_running_loop().call_soon(lambda:None)
        await asyncio.sleep(0)
    asyncio.run(run())
    env.closing.start(actor_user_id=env.owner)


def test_guard_rejects_reused_pool_background_work(env):
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(lambda:True).result()
        with pytest.raises(ReadOnlyViolation):
            with read_only_execution('catalog.search'):pool.submit(lambda:True)


def test_raw_cached_sqlite_connector_cannot_skip_boundary(env):
    import _sqlite3
    with pytest.raises(ReadOnlyViolation):
        with read_only_execution('catalog.search'):_sqlite3.connect(env.path)


def test_safe_error_does_not_run_generic_fallback_or_reveal_details():
    from app.services.assistant.read_only import safe_tool_errors
    answers=[];fallback=[]
    class Message:
        async def answer(self,text):answers.append(text)
    @safe_tool_errors
    async def execute(message):
        try:
            with read_only_execution('catalog.search'):
                raise ReadOnlyViolation('catalog.search','sqlite_write')
        except Exception:fallback.append(True)
    asyncio.run(execute(Message()))
    assert not fallback and len(answers)==1 and 'SQL' not in answers[0]


@pytest.mark.parametrize('defective',[False,True])
def test_P1_real_evaluation_guard_violation_is_safety_exit_two(monkeypatch,defective):
    from types import SimpleNamespace
    from pydantic import SecretStr
    from app.services.assistant.evaluation_corpus_v2 import select_suite
    from app.services.assistant.live_evaluation import run_live,LiveRunOptions
    from app.services.warehouse.closing_service import WarehouseClosingService
    suite,_=select_suite('extended',0)
    case=next(c for c in suite.cases if c.request.case_id=='P1')
    from dataclasses import replace
    case=replace(case,request=replace(case.request,max_steps=1))
    class Wire:
        calls=0
        async def post_json(self,*args,**kwargs):
            self.calls+=1
            data=dict(dialogue_act='new_request',domain='warehouse',capability_id='warehouse.required_status',
                target_index=-1,action_code='list',needs_clarification=False,confidence_scaled=10000)
            return HttpResponse(200,json.dumps({'choices':[{'message':{'content':json.dumps(data)}}]}).encode())
    if defective:
        def bad(self,**kwargs):
            with closing(sqlite3.connect(self.repository.path)) as c:c.execute('UPDATE users SET is_active=0')
        monkeypatch.setattr(WarehouseClosingService,'get_required_status_snapshot',bad)
    wire=Wire()
    settings=SimpleNamespace(groq_api_key=SecretStr('synthetic'),groq_planner_model='synthetic')
    report,code=asyncio.run(run_live(LiveRunOptions('live_isolated',True,1,'groq',True,suite='extended'),
        settings_loader=lambda:settings,transport_factory=lambda:wire,corpus=(case,)))
    assert wire.calls==1
    if defective:
        assert code==2 and report['ready'] is False and report['failure_counts']['safety_contract_failures']>0
    else:
        assert report['safety_error'] is None
        assert report['unexpected_state_change_count']==0
        assert report['rows'][0]['capability']=='warehouse.required_status', (report['errors'],report['provider_validation'])
