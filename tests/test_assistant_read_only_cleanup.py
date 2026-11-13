"""Fault injection against real SQLite handles; temporary v15, no providers."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

import pytest

import app.services.assistant.read_only as ro
from app.storage.user_database import UserDatabase


class FatalSignal(BaseException):
    pass


def leaves(error):
    result=[];seen=set()
    def visit(item):
        if id(item) in seen:return
        seen.add(id(item))
        if isinstance(item,BaseExceptionGroup):
            for child in item.exceptions:visit(child)
        else:result.append(item)
        if item.__cause__ is not None:visit(item.__cause__)
    visit(error)
    return result


def failure(error):
    return next(item for item in leaves(error) if isinstance(item, ro.ReadOnlyCleanupFailure))


def closed(connection):
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute('SELECT 1')


def clean(scope, original):
    assert ro._ACTIVE.get() is None
    assert ro._OPENING.get() is False
    assert ro._BYTECODE_USERS == 0
    assert sys.dont_write_bytecode == original
    assert not scope.connections and scope.cleanup_done


def next_writer(path):
    with closing(sqlite3.connect(path, timeout=.05)) as c:
        assert c.execute('PRAGMA query_only').fetchone()[0] == 0
        c.execute('UPDATE warehouse_settings SET timezone=timezone')
        c.commit()


@pytest.fixture
def database(tmp_path):
    path = tmp_path/'cleanup.db'
    UserDatabase(path).initialize()
    return path


def fault_factory(rollback=None, close=None, noop=False, after_close=False):
    class Fault(sqlite3.Connection):
        rollback_calls = 0
        close_calls = 0
        def rollback(self):
            self.rollback_calls += 1
            if rollback is not None:
                raise rollback
            return super().rollback()
        def close(self):
            self.close_calls += 1
            if after_close:
                super().close()
            if close is not None:
                raise close
            if not noop:
                return super().close()
    return Fault


def open_read(path, factory=sqlite3.Connection):
    c = sqlite3.connect(path, factory=factory)
    c.execute('BEGIN')
    c.execute('SELECT count(*) FROM warehouse_settings').fetchone()
    return c


@pytest.mark.parametrize('stage', ['rollback','close'])
@pytest.mark.parametrize('error_type', [RuntimeError,sqlite3.OperationalError,asyncio.CancelledError,FatalSignal])
def test_faults_close_real_connection_and_restore_all_state(database,stage,error_type):
    original = sys.dont_write_bytecode
    raw = error_type('PRIVATE_SQL_PATH_INPUT_MARKER')
    factory = fault_factory(**{stage:raw})
    with pytest.raises(BaseException) as caught:
        with ro.read_only_execution('catalog.search'):
            scope = ro._ACTIVE.get()
            c = open_read(database,factory)
    closed(c);clean(scope,original);next_writer(database)
    problem = failure(caught.value)
    assert not problem.quarantined
    assert any(row.stage==stage for row in problem.diagnostics)
    assert 'PRIVATE_SQL_PATH_INPUT_MARKER' not in str(problem)
    assert 'PRIVATE_SQL_PATH_INPUT_MARKER' not in repr(problem.diagnostics)
    if not isinstance(raw,Exception):
        assert raw in leaves(caught.value)
        assert ro.read_only_failure(caught.value) is None
    else:
        assert ro.read_only_failure(caught.value) is problem
    if stage=='close':
        assert any(row.forced_close_attempted and row.forced_close_succeeded for row in problem.diagnostics)


@pytest.mark.parametrize('rollback,close', [(RuntimeError,RuntimeError),(sqlite3.OperationalError,sqlite3.OperationalError)])
def test_both_cleanup_failures_are_reported_and_neither_leaks(database,rollback,close):
    original=sys.dont_write_bytecode
    with pytest.raises(ro.ReadOnlyCleanupFailure) as caught:
        with ro.read_only_execution('catalog.search'):
            scope=ro._ACTIVE.get();c=open_read(database,fault_factory(rollback('private'),close('private')))
    assert {'rollback','close'} <= {row.stage for row in caught.value.diagnostics}
    assert c.rollback_calls==c.close_calls==1
    closed(c);clean(scope,original);next_writer(database)


@pytest.mark.parametrize('body_type',[ValueError,asyncio.CancelledError,FatalSignal])
def test_body_exception_identity_survives_cleanup_failure(database,body_type):
    original=sys.dont_write_bytecode;body=body_type('PRIVATE_BODY')
    with pytest.raises(BaseExceptionGroup if body_type is ValueError else body_type) as caught:
        with ro.read_only_execution('catalog.search'):
            scope=ro._ACTIVE.get();c=open_read(database,fault_factory(close=RuntimeError('PRIVATE_CLOSE')))
            raise body
    assert body in leaves(caught.value)
    assert isinstance(failure(caught.value),ro.ReadOnlyCleanupFailure)
    if body_type is not ValueError:assert ro.read_only_failure(caught.value) is None
    closed(c);clean(scope,original);next_writer(database)


@pytest.mark.parametrize('body_type',[ValueError,asyncio.CancelledError,FatalSignal])
def test_body_exception_without_cleanup_error_is_unchanged(database,body_type):
    original=sys.dont_write_bytecode;body=body_type('private')
    with pytest.raises(body_type) as caught:
        with ro.read_only_execution('catalog.search'):
            scope=ro._ACTIVE.get();c=open_read(database)
            raise body
    assert caught.value is body
    closed(c);clean(scope,original);next_writer(database)


@pytest.mark.parametrize('bad_index',[0,2])
def test_all_connections_cleaned_even_when_first_or_last_fails(database,bad_index):
    original=sys.dont_write_bytecode;handles=[]
    with pytest.raises(ro.ReadOnlyCleanupFailure) as caught:
        with ro.read_only_execution('catalog.search'):
            scope=ro._ACTIVE.get()
            for i in range(3):
                handles.append(open_read(database,fault_factory(close=RuntimeError('private')) if i==bad_index else sqlite3.Connection))
    for c in handles:closed(c)
    assert next(row for row in caught.value.diagnostics if row.stage=='close').connection_ordinal==bad_index+1
    clean(scope,original);next_writer(database)


def test_duplicate_registry_repeated_cleanup_and_nested_scopes(database):
    original=sys.dont_write_bytecode
    with ro.read_only_execution('catalog.search'):
        scope=ro._ACTIVE.get();c=open_read(database,fault_factory())
        scope.track(c,initialized=True)
        scope.connections[-1]=scope.connections[id(c)]
        with ro.read_only_execution('catalog.get'):
            assert ro._ACTIVE.get() is scope and ro._BYTECODE_USERS==1
        assert ro._cleanup_owned(scope)==()
        assert ro._cleanup_owned(scope)==()
        assert c.close_calls==1
    closed(c);clean(scope,original);next_writer(database)


def test_already_closed_connection_is_not_closed_again(database):
    original=sys.dont_write_bytecode
    with ro.read_only_execution('catalog.search'):
        scope=ro._ACTIVE.get();c=open_read(database,fault_factory())
        c.close()
    assert c.close_calls==1
    closed(c);clean(scope,original);next_writer(database)


@pytest.mark.parametrize('after_close',[False,True])
def test_broken_close_noop_or_error_after_effective_close(database,after_close):
    original=sys.dont_write_bytecode
    with pytest.raises(ro.ReadOnlyCleanupFailure) as caught:
        with ro.read_only_execution('catalog.search'):
            scope=ro._ACTIVE.get()
            c=open_read(database,fault_factory(noop=not after_close,
                close=RuntimeError('private') if after_close else None,after_close=after_close))
    assert c.close_calls==1
    assert not caught.value.quarantined
    closed(c);clean(scope,original);next_writer(database)


def test_forced_base_close_failure_is_explicit_fatal_without_stale_guard(database,monkeypatch):
    original=sys.dont_write_bytecode
    def fail_base(c):raise sqlite3.OperationalError('PRIVATE_NATIVE_MESSAGE')
    monkeypatch.setattr(ro,'_force_close',fail_base)
    try:
        with pytest.raises(ro.ReadOnlyCleanupFailure) as caught:
            with ro.read_only_execution('catalog.search'):
                scope=ro._ACTIVE.get();c=open_read(database,fault_factory(close=RuntimeError('private')))
        assert caught.value.quarantined
        assert any(row.category=='connection_leaked' for row in caught.value.diagnostics)
        clean(scope,original)
        with ro.read_only_execution('catalog.get'):
            unrelated=ro._ACTIVE.get()
        clean(unrelated,original)
    finally:
        sqlite3.Connection.close(c)  # release the simulated uncloseable handle, not production
    next_writer(database)


def test_registration_failure_still_closes_the_initialized_native_handle(database,monkeypatch):
    original=sys.dont_write_bytecode;handles=[];body=RuntimeError('PRIVATE_REGISTRATION')
    def fail(scope,c):handles.append(c);raise body
    monkeypatch.setattr(ro,'_register_connection',fail)
    with pytest.raises(BaseExceptionGroup) as caught:
        with ro.read_only_execution('catalog.search'):
            scope=ro._ACTIVE.get();sqlite3.connect(database)
    assert body in leaves(caught.value)
    for c in handles:closed(c)
    clean(scope,original)
    monkeypatch.undo();next_writer(database)


def test_exception_before_creating_connection_and_external_handle_ownership(database):
    original=sys.dont_write_bytecode;body=RuntimeError('private')
    external=sqlite3.connect(database)
    try:
        with pytest.raises(RuntimeError) as caught:
            with ro.read_only_execution('catalog.search'):
                scope=ro._ACTIVE.get();assert not scope.connections
                raise body
        assert caught.value is body
        clean(scope,original)
        assert external.execute('PRAGMA query_only').fetchone()[0]==0
        external.execute('UPDATE warehouse_settings SET timezone=timezone');external.commit()
    finally:external.close()


@pytest.mark.parametrize('journal',['DELETE','WAL'])
def test_transaction_lock_released_after_failed_rollback_and_close(database,journal):
    with closing(sqlite3.connect(database)) as c:
        assert c.execute('PRAGMA journal_mode='+journal).fetchone()[0].casefold()==journal.casefold()
    with pytest.raises(ro.ReadOnlyCleanupFailure):
        with ro.read_only_execution('catalog.search'):
            c=open_read(database,fault_factory(rollback=sqlite3.OperationalError('private'),close=RuntimeError('private')))
    closed(c);next_writer(database)
    with closing(sqlite3.connect(database,timeout=.05)) as writer:
        if journal=='WAL':assert writer.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]==0


def test_two_threads_and_event_loops_restore_global_counter_and_loop_hooks(database):
    original=sys.dont_write_bytecode;barrier=threading.Barrier(2)
    def worker(bad):
        async def run():
            loop=asyncio.get_running_loop();factory=loop.get_task_factory()
            hooks={name:getattr(loop,name) for name in ('call_soon','call_later','call_at','call_soon_threadsafe')}
            error=None
            try:
                with ro.read_only_execution('catalog.search'):
                    scope=ro._ACTIVE.get()
                    c=open_read(database,fault_factory(close=asyncio.CancelledError('private')) if bad else sqlite3.Connection)
                    barrier.wait(timeout=10)
                    assert ro._BYTECODE_USERS==2
                    barrier.wait(timeout=10)
            except BaseException as caught:error=caught
            assert ro._ACTIVE.get() is None and not scope.connections
            assert loop.get_task_factory() is factory
            assert all(getattr(loop,name)==method for name,method in hooks.items())
            closed(c)
            await asyncio.sleep(0)
            return error
        return asyncio.run(run())
    with ThreadPoolExecutor(max_workers=2) as pool:
        a=pool.submit(worker,False);b=pool.submit(worker,True)
        assert a.result() is None
        assert any(isinstance(e,asyncio.CancelledError) for e in leaves(b.result()))
    assert ro._BYTECODE_USERS==0 and sys.dont_write_bytecode==original
    next_writer(database)


def test_handler_groups_safe_errors_but_never_swallows_cancel_or_uses_fallback(database,caplog):
    answers=[];fallback=[]
    class Message:
        async def answer(self,text):answers.append(text)
    async def run(cancel):
        @ro.safe_tool_errors
        async def tool(message):
            try:
                with ro.read_only_execution('catalog.search'):
                    open_read(database,fault_factory(close=RuntimeError('PRIVATE_DB_PATH_SQL')))
                    raise asyncio.CancelledError('PRIVATE_BODY') if cancel else ValueError('PRIVATE_BODY')
            except Exception:fallback.append(True)
        if cancel:
            with pytest.raises(asyncio.CancelledError):await tool(Message())
        else:await tool(Message())
    asyncio.run(run(False));asyncio.run(run(True))
    assert len(answers)==1 and not fallback
    assert 'PRIVATE' not in caplog.text and str(database) not in caplog.text
    assert 'connection_ordinal=' in caplog.text and 'forced_close_succeeded=True' in caplog.text
    assert ro._ACTIVE.get() is None and ro._BYTECODE_USERS==0
    next_writer(database)


def test_fault_subprocess_cannot_change_parent_hooks_counter_or_umask(database):
    before=Path('/proc/self/status').read_text().split('Umask:')[1].splitlines()[0].strip()
    code='''
import asyncio,json,sqlite3,sys
from app.services.assistant import read_only as ro
class Bad(sqlite3.Connection):
 def close(self):raise RuntimeError('private')
try:
 with ro.read_only_execution('catalog.search'):
  c=sqlite3.connect(sys.argv[1],factory=Bad)
except ro.ReadOnlyCleanupFailure:pass
print(json.dumps(dict(active=ro._ACTIVE.get() is not None,counter=ro._BYTECODE_USERS)))
'''
    result=subprocess.run([sys.executable,'-',str(database)],input=code,capture_output=True,text=True,timeout=30)
    assert result.returncode==0
    assert json.loads(result.stdout)=={'active':False,'counter':0}
    assert before==Path('/proc/self/status').read_text().split('Umask:')[1].splitlines()[0].strip()
    assert ro._ACTIVE.get() is None and ro._BYTECODE_USERS==0


def test_task_cancel_during_query_and_during_close_preserves_cancelled_state(database):
    original=sys.dont_write_bytecode
    async def run():
        async def query_cancel():
            with ro.read_only_execution('catalog.search'):
                scope=ro._ACTIVE.get();c=open_read(database)
                def cancel(value):
                    asyncio.current_task().cancel()
                    return value
                c.create_function('abs',1,cancel)
                c.execute('SELECT abs(1)').fetchone()
            closed(c);clean(scope,original)
            await asyncio.sleep(0)
        task=asyncio.create_task(query_cancel())
        with pytest.raises(asyncio.CancelledError):await task
        assert task.cancelled()
        async def close_cancel():
            with ro.read_only_execution('catalog.search'):
                open_read(database,fault_factory(close=asyncio.CancelledError('private')))
        task=asyncio.create_task(close_cancel())
        with pytest.raises(asyncio.CancelledError) as caught:await task
        assert task.cancelled() and isinstance(failure(caught.value),ro.ReadOnlyCleanupFailure)
        assert ro._ACTIVE.get() is None and ro._BYTECODE_USERS==0
        await asyncio.sleep(0)
    asyncio.run(run());next_writer(database)


def test_registration_constructor_failure_and_body_before_connection_are_safe(database):
    original=sys.dont_write_bytecode;body=RuntimeError('PRIVATE_CONSTRUCTOR')
    class Broken(sqlite3.Connection):
        def __init__(self,*args,**kwargs):raise body
    with pytest.raises(RuntimeError) as caught:
        with ro.read_only_execution('catalog.search'):
            scope=ro._ACTIVE.get();sqlite3.connect(database,factory=Broken)
    assert caught.value is body
    clean(scope,original);next_writer(database)


def test_scope_rejects_new_connections_after_explicit_idempotent_cleanup(database):
    original=sys.dont_write_bytecode
    with pytest.raises(ro.ReadOnlyViolation):
        with ro.read_only_execution('catalog.search'):
            scope=ro._ACTIVE.get();c=open_read(database)
            assert ro._cleanup_owned(scope)==()
            sqlite3.connect(database)
    closed(c);clean(scope,original);next_writer(database)


@pytest.mark.parametrize('force_failure',[False,True])
def test_real_evaluation_cleanup_failure_is_safety_not_provider_fallback(monkeypatch,force_failure):
    from dataclasses import replace
    from types import SimpleNamespace
    from pydantic import SecretStr
    from app.services.assistant.evaluation_corpus_v2 import select_suite
    from app.services.assistant.evaluation_database import _OwnedConnection
    from app.services.assistant.live_evaluation import run_live,LiveRunOptions
    from app.services.assistant.providers import HttpResponse
    suite,_=select_suite('extended',0)
    case=next(case for case in suite.cases if case.request.case_id=='P1')
    case=replace(case,request=replace(case.request,max_steps=1))
    original_close=_OwnedConnection.close
    def bad_close(c):
        if ro._ACTIVE.get() is not None and not ro._closed(c):
            raise RuntimeError('PRIVATE_CONNECTION_CLEANUP')
        return original_close(c)
    monkeypatch.setattr(_OwnedConnection,'close',bad_close)
    if force_failure:
        def fail(c):raise sqlite3.OperationalError('PRIVATE_BASE_CLOSE')
        monkeypatch.setattr(ro,'_force_close',fail)
    class Wire:
        calls=0
        async def post_json(self,*args,**kwargs):
            self.calls+=1
            route=dict(dialogue_act='new_request',domain='warehouse',capability_id='warehouse.required_status',
                target_index=-1,action_code='list',needs_clarification=False,confidence_scaled=10000)
            return HttpResponse(200,json.dumps({'choices':[{'message':{'content':json.dumps(route)}}]}).encode())
    settings=SimpleNamespace(groq_api_key=SecretStr('synthetic'),groq_planner_model='synthetic')
    wire=Wire()
    report,code=asyncio.run(run_live(LiveRunOptions('live_isolated',True,1,'groq',True,suite='extended'),
        settings_loader=lambda:settings,transport_factory=lambda:wire,corpus=(case,)))
    assert wire.calls==1 and code==2 and report['ready'] is False
    assert report['safety_error']=='read_only_violation'
    assert report['failure_counts']['safety_contract_failures']>0
    assert report['rows'][0]['cleanup_quarantined'] is force_failure
    if force_failure:
        assert any(row['category']=='connection_leaked' for row in report['rows'][0]['cleanup_diagnostics'])
    assert ro._ACTIVE.get() is None and ro._BYTECODE_USERS==0
    assert 'PRIVATE_' not in json.dumps(report)
