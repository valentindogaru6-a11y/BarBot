"""Real exec/seccomp tests, synthetic owned v15 only; never network/providers."""
import asyncio
from contextlib import closing
import contextvars
import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import time

import pytest
from pydantic import ValidationError

from app.services.assistant import read_broker as broker_module
from app.services.assistant import read_kernel as kernel
from app.services.assistant.capabilities import CAPABILITIES
from app.services.assistant.evaluation_environment import IsolatedEvaluationEnvironment
from app.services.assistant.read_broker import ReadOnlyExecutionBroker, ReadWorkerFailure, await_read
from app.services.assistant.read_only import ReadOnlyViolation
from app.services.assistant.read_protocol import ReadRequest, encode, manifest
from tests.test_assistant_read_only_boundary import dispatch


@pytest.fixture
def env():
    with IsolatedEvaluationEnvironment() as e:
        e.configure('extended')
        yield e


def request(e, broker, identifier='warehouse.required_status', **kwargs):
    return broker.request(identifier, access=e.access, actor=e.owner, policy=e.policy,
        flags=e.controller.flags, registry=e.registry, catalog=e.storage.load(), **kwargs)


def following_write(e):
    with closing(contextvars.Context().run(sqlite3.connect, e.path, timeout=.2)) as c:
        c.execute('BEGIN IMMEDIATE');c.execute('UPDATE users SET is_active=is_active');c.rollback()
    assert not broker_module._CHILDREN
    assert kernel._WAKE_USERS == 0
    from app.services.assistant import read_only as ro
    assert ro._ACTIVE.get() is None and ro._BYTECODE_USERS == 0


def child_patch(monkeypatch, code):
    """Test-only bootstrap patch. No executable name/flag exists in wire IPC."""
    original = broker_module.subprocess.Popen
    def popen(command, *args, **kwargs):
        command = list(command)
        marker = 'from app.services.assistant.read_worker import main;'
        if not isinstance(command[-1],str) or marker not in command[-1]:
            return original(command,*args,**kwargs)
        command[-1] = command[-1].replace(marker, marker+'exec('+repr(code)+');')
        return original(command, *args, **kwargs)
    monkeypatch.setattr(broker_module.subprocess, 'Popen', popen)


def coordinator_patch(monkeypatch,code):
    original=broker_module.subprocess.Popen
    def popen(command,*args,**kwargs):
        command=list(command);marker='from app.services.assistant.read_snapshot import coordinate;'
        if isinstance(command[-1],str) and marker in command[-1]:
            command[-1]=command[-1].replace(marker,marker+'exec('+repr(code)+');')
        return original(command,*args,**kwargs)
    monkeypatch.setattr(broker_module.subprocess,'Popen',popen)


@pytest.mark.parametrize('identifier', [c.id for c in CAPABILITIES if int(c.risk_level) == 0])
def test_eighteen_real_broker_executors(env, identifier):
    target = None; args = {}
    if identifier == 'catalog.search':args={'query':'Облепиха'}
    if identifier == 'catalog.get':target='preparations/_/sea'
    if identifier == 'users.get':args={'query':'Дмитро'}
    if identifier == 'attendance.get':
        target=str(env.attendance.open_sessions(env.owner)[0]['id']);args={'entity_ref':target}
    if identifier == 'reports.status':
        target=str(env.reports.history(actor_user_id=env.owner,page=0)[0][0]['id']);args={'entity_ref':target}
    before=env.business.snapshot()
    result=dispatch(env,identifier,args,target)
    assert result.capability_id == identifier
    assert before == env.business.snapshot()
    following_write(env)


@pytest.mark.parametrize('journal,pinned', [('DELETE',False),('WAL',False),('WAL',True)])
def test_native_journal_states_without_main_or_business_writes(env,journal,pinned):
    with closing(sqlite3.connect(env.path)) as c:
        c.execute('PRAGMA journal_mode='+journal)
    native=contextvars.Context().run(sqlite3.connect, env.path)
    if pinned:
        native.execute('UPDATE users SET is_active=is_active');native.commit()
    else:
        native.close();native=None
    try:
        before=hashlib.sha256(env.path.read_bytes()).digest()
        b=ReadOnlyExecutionBroker();value=b.execute(request(env,b))
        assert value['data']=={'total':2,'resolved':0}
        assert b.last_metrics['business_writes']==0
        assert before==hashlib.sha256(env.path.read_bytes()).digest()
    finally:
        if native:native.close()
    following_write(env)


def test_active_wal_frames_are_read_not_ignored(env):
    with closing(contextvars.Context().run(sqlite3.connect,env.path)) as c:
        c.execute('PRAGMA wal_autocheckpoint=0')
        c.execute("UPDATE users SET first_name='Synthetic WAL value' WHERE telegram_user_id=?",(env.employee,))
        c.commit()
        assert Path(str(env.path)+'-wal').stat().st_size>32
        result=dispatch(env,'users.get',{'query':'Synthetic WAL value'})
        assert any(row['name']=='Synthetic WAL value' for row in result.data['users'])
    following_write(env)


def test_exact_external_parent_handle_and_closure_cannot_reach_worker(env,monkeypatch):
    native=contextvars.Context().run(sqlite3.connect,env.path)
    assert type(native) is sqlite3.Connection
    assert native.execute('PRAGMA query_only').fetchone()[0]==0
    native.execute('SELECT count(*) FROM users').fetchone()
    calls=[]
    def malicious(**kwargs):
        calls.append(True)
        native.execute('UPDATE users SET is_active=0');native.commit()
        raise AssertionError('must never execute in parent')
    monkeypatch.setattr(env.closing,'get_required_status_snapshot',malicious)
    before=env.business.snapshot()
    try:
        result=dispatch(env,'warehouse.required_status')
        assert result.data=={'total':2,'resolved':0}
        assert calls==[] and before==env.business.snapshot()
        assert native.execute('PRAGMA query_only').fetchone()[0]==0
    finally:
        native.close()
    following_write(env)


@pytest.mark.parametrize('change',[
    {'capability_id':'shell.execute'}, {'capability_id':'warehouse.commit_batch'},
    {'manifest_hash':'0'*64}, {'arguments':{'sql':'DELETE FROM users'}},
    {'arguments':{'database':'file:/tmp/other?mode=rw'}}, {'version':2},
])
def test_forged_or_stale_ipc_never_starts_process(env,monkeypatch,change):
    b=ReadOnlyExecutionBroker();r=request(env,b).model_copy(update=change)
    monkeypatch.setattr(broker_module.subprocess,'Popen',lambda *a,**kw:pytest.fail('unexpected exec'))
    with pytest.raises(ReadOnlyViolation):b.execute(r)
    following_write(env)


def test_wire_has_no_executable_or_secret_fields(env):
    b=ReadOnlyExecutionBroker();r=request(env,b)
    value=r.model_dump(mode='json')
    for name in ('executor','callable','module','connection','prompt','token','api_key'):
        with pytest.raises(ValidationError):ReadRequest.model_validate({**value,name:'forbidden'})
    with closing(sqlite3.connect(env.path)) as c:
        with pytest.raises((ValidationError,TypeError,ValueError)):
            request(env,b,arguments={'external':c})
    assert r.manifest_hash==manifest()


@pytest.mark.parametrize('patch,expected',[
    ("import app.services.assistant.read_worker as w\nw._run=lambda *a: __import__('time').sleep(30)", 'timeout'),
    ("import app.services.assistant.read_worker as w\nw._run=lambda *a: __import__('os')._exit(7)", 'crash'),
    ("import app.services.assistant.read_worker as w\nw.encode=lambda *a:b'{'", 'malformed_ipc'),
])
def test_bounded_timeout_crash_malformed_cleanup(env,monkeypatch,patch,expected):
    child_patch(monkeypatch,patch)
    b=ReadOnlyExecutionBroker(timeout=5)
    with pytest.raises(ReadWorkerFailure) as error:b.execute(request(env,b))
    assert error.value.category==expected
    following_write(env)


def test_cancel_waits_for_worker_reap(env,monkeypatch):
    child_patch(monkeypatch,"import app.services.assistant.read_worker as w\nw._run=lambda *a: __import__('time').sleep(30)")
    async def run():
        b=ReadOnlyExecutionBroker();r=request(env,b)
        task=asyncio.create_task(await_read(b.execute,r))
        for _ in range(500):
            if broker_module._CHILDREN:break
            await asyncio.sleep(.01)
        assert broker_module._CHILDREN
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert task.cancelled()
    asyncio.run(run());following_write(env)


def test_global_task_shutdown_still_waits_for_native_reap(env,monkeypatch):
    child_patch(monkeypatch,"import app.services.assistant.read_worker as w\nw._run=lambda *a:__import__('time').sleep(30)")
    original=broker_module._stop;completed=[]
    def slow_stop(child):
        time.sleep(.1);original(child);completed.append(True)
    monkeypatch.setattr(broker_module,'_stop',slow_stop)
    async def run():
        b=ReadOnlyExecutionBroker();r=request(env,b)
        task=asyncio.create_task(await_read(b.execute,r))
        while not broker_module._CHILDREN:await asyncio.sleep(.01)
        for pending in asyncio.all_tasks():
            if pending is not asyncio.current_task():pending.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert completed and not broker_module._CHILDREN
    asyncio.run(run());following_write(env)


@pytest.mark.parametrize('patch',[
    "import app.services.assistant.read_worker as w\nw._run=lambda r,c:c.execute('UPDATE users SET is_active=0')",
    "import app.services.assistant.read_worker as w\nw._run=lambda r,c:__import__('sqlite3').connect(r.database)",
    "import app.services.assistant.read_worker as w\nw._run=lambda r,c:__import__('socket').socket()",
    "import app.services.assistant.read_worker as w\nw._run=lambda r,c:object()",
])
def test_worker_side_effect_or_invalid_result_is_fatal(env,monkeypatch,patch):
    child_patch(monkeypatch,patch)
    before=env.business.snapshot();b=ReadOnlyExecutionBroker()
    with pytest.raises((ReadOnlyViolation,ReadWorkerFailure)):b.execute(request(env,b))
    assert before==env.business.snapshot();following_write(env)


def test_spawn_environment_and_parent_fds_are_not_inherited(env,monkeypatch):
    monkeypatch.setenv('SYNTHETIC_PRIVATE_KEY','not-for-worker')
    external=contextvars.Context().run(sqlite3.connect,env.path)
    external.execute('SELECT count(*) FROM users').fetchone()
    # Fail unless the child was exec'd, given only a minimal environment, and
    # inherited no SQLite data descriptor. The control socket is the sole pass_fd.
    original=broker_module.subprocess.Popen
    seen=[]
    def inspect(command,*a,**kw):
        assert kw['close_fds']
        coordinator='read_snapshot import coordinate' in command[-1]
        assert not kw.get('pass_fds') if coordinator else len(kw['pass_fds'])==1
        assert set(kw['env'])=={'LANG','TZ'}
        assert '-I' in command and '-B' in command
        seen.append(True);return original(command,*a,**kw)
    monkeypatch.setattr(broker_module.subprocess,'Popen',inspect)
    try:assert dispatch(env,'warehouse.required_status').data['total']==2
    finally:external.close()
    assert seen==[True,True];following_write(env)


@pytest.mark.parametrize('suffix', ['-wal','-shm'])
@pytest.mark.parametrize('kind', ['symlink','hardlink'])
def test_sidecar_alias_is_fatal_before_open(env,monkeypatch,suffix,kind):
    b=ReadOnlyExecutionBroker();r=request(env,b)
    sidecar=Path(str(env.path)+suffix)
    assert not sidecar.exists()
    victim=env.root/'synthetic-victim';victim.write_bytes(b'unchanged')
    if kind=='symlink':sidecar.symlink_to(victim)
    else:os.link(victim,sidecar)
    try:
        with pytest.raises(ReadOnlyViolation):b.execute(r)
        assert victim.read_bytes()==b'unchanged'
    finally:sidecar.unlink()
    following_write(env)


@pytest.mark.parametrize('suffix',['','-wal','-shm'])
def test_db_or_sidecar_replacement_between_validation_and_open_is_fatal(env,monkeypatch,suffix):
    pin=contextvars.Context().run(sqlite3.connect,env.path)
    pin.execute('SELECT count(*) FROM users').fetchone()
    b=ReadOnlyExecutionBroker();r=request(env,b)
    marker=env.root/'race-marker'
    coordinator_patch(monkeypatch,'''
import app.services.assistant.read_snapshot as s
original=s.SnapshotAuthority._open
swapped=False
def replace(self,path,flags):
 global swapped
 target=s.Path(str(self.path)+%r)
 if path==str(target) and not swapped:
  swapped=True
  saved=target.parent/'saved-original';target.rename(saved)
  target.write_bytes(saved.read_bytes());(target.parent/'race-marker').write_text('called')
  try:return original(self,path,flags)
  finally:target.unlink();saved.rename(target)
 return original(self,path,flags)
s.SnapshotAuthority._open=replace
''' % suffix)
    try:
        with pytest.raises(ReadOnlyViolation):b.execute(r)
    finally:pin.close()
    assert marker.exists();marker.unlink()
    following_write(env)


@pytest.mark.parametrize('suffix',['','-wal','-shm'])
def test_db_or_sidecar_replacement_after_open_is_fatal(env,monkeypatch,suffix):
    pin=contextvars.Context().run(sqlite3.connect,env.path)
    pin.execute('SELECT count(*) FROM users').fetchone()
    b=ReadOnlyExecutionBroker();r=request(env,b)
    marker=env.root/'race-marker'
    coordinator_patch(monkeypatch,'''
import app.services.assistant.read_snapshot as s,atexit
original=s.SnapshotAuthority._open
swapped=False
def replace(self,path,flags):
 global swapped
 result=original(self,path,flags)
 if path==str(self.path)+%r and not swapped:
  swapped=True
  p=s.Path(path);saved=p.parent/'saved-open-file';p.rename(saved)
  p.write_bytes(saved.read_bytes());(p.parent/'race-marker').write_text('called')
  def restore():p.unlink();saved.rename(p)
  atexit.register(restore)
 return result
s.SnapshotAuthority._open=replace
''' % suffix)
    try:
        with pytest.raises(ReadOnlyViolation):b.execute(r)
        assert marker.exists()
    finally:
        if marker.exists():marker.unlink()
        pin.close()
    following_write(env)


def test_sealed_executor_cannot_use_native_sidecar_write_fd(env,monkeypatch):
    child_patch(monkeypatch,"""
import app.services.assistant.read_worker as w,os,ctypes as C
lib=C.CDLL(None,use_errno=True)
native_open=lib.open;native_open.argtypes=[C.c_char_p,C.c_int]
def wrong(r,c):
 for fd in range(3,64):
  try:target=os.readlink('/proc/self/fd/'+str(fd))
  except OSError:continue
  if target.endswith('-shm'):
   raise AssertionError('live SHM inherited')
 # Clone is DELETE journal and has no SHM. Even a known source sidecar cannot
 # be opened by the executor, regardless of prepared native function pointers.
 native_open(%r,os.O_RDWR)
 return {'capability_id':r.capability_id,'message':'ignored','data':{'total':2,'resolved':0},'ui_action':None,'tool_action':None}
w._run=wrong
""" % os.fsencode(str(env.path)+'-shm'))
    before=env.business.snapshot();b=ReadOnlyExecutionBroker()
    with pytest.raises(ReadOnlyViolation):b.execute(request(env,b))
    assert before==env.business.snapshot();following_write(env)


def test_parallel_readers_have_no_cross_request_state(env):
    from concurrent.futures import ThreadPoolExecutor
    pin=contextvars.Context().run(sqlite3.connect,env.path)
    pin.execute('SELECT count(*) FROM users').fetchone()
    def run():
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(contextvars.copy_context().run,lambda:dispatch(env,'warehouse.required_status')) for _ in range(2)]
            assert [f.result().data for f in futures]==[{'total':2,'resolved':0}]*2
    try:run()
    finally:pin.close()
    following_write(env)


@pytest.mark.parametrize('sql',[
    'DELETE FROM users','CREATE TABLE unexpected(value)',
    'PRAGMA user_version=1','PRAGMA query_only=OFF',
    "ATTACH DATABASE ':memory:' AS other",'VACUUM',
])
def test_real_worker_authorizer_rejects_every_mutation_class(env,monkeypatch,sql):
    child_patch(monkeypatch,'import app.services.assistant.read_worker as w\nw._run=lambda r,c:c.execute('+repr(sql)+')')
    before=env.business.snapshot();b=ReadOnlyExecutionBroker()
    with pytest.raises(ReadOnlyViolation):b.execute(request(env,b))
    assert before==env.business.snapshot();following_write(env)


@pytest.mark.parametrize('changes', [dict(chat_type='group'),dict(flags={'ai_assistant_enabled':False,'warehouse_enabled':True}),
                                     dict(flags={'ai_assistant_enabled':True,'warehouse_enabled':False})])
def test_worker_rechecks_private_and_feature_facts(env,changes):
    b=ReadOnlyExecutionBroker()
    kwargs=dict(access=env.access,actor=env.owner,policy=env.policy,
                flags=env.controller.flags,registry=env.registry,catalog=env.storage.load())
    kwargs.update(changes)
    r=b.request('warehouse.required_status',**kwargs)
    with pytest.raises(ReadOnlyViolation):b.execute(r)
    following_write(env)


def test_worker_rechecks_role_after_request_snapshot(env):
    from app.models.user import Role
    env.policy.settings.ai_access_mode='role_based'
    b=ReadOnlyExecutionBroker()
    r=b.request('users.list',access=env.access,actor=4,policy=env.policy,
                flags=env.controller.flags,registry=env.registry)
    env.users.set_role(4,Role.USER,actor_user_id=env.owner)
    with pytest.raises(ReadOnlyViolation):b.execute(r)
    following_write(env)


def test_concurrent_committed_parent_write_does_not_change_frozen_clone(env,monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    child_patch(monkeypatch,"""
import app.services.assistant.read_worker as w,time
original=w._run
def delayed(*args):
 time.sleep(.7)
 return original(*args)
w._run=delayed
""")
    ready=threading.Event();original=broker_module.FileAuthority
    class Probe(original):
        def __setattr__(self,name,value):
            super().__setattr__(name,value)
            if name=='sealed' and value:ready.set()
    monkeypatch.setattr(broker_module,'FileAuthority',Probe)
    pin=contextvars.Context().run(sqlite3.connect,env.path)
    pin.execute('SELECT count(*) FROM users').fetchone()
    b=ReadOnlyExecutionBroker();r=request(env,b)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(contextvars.copy_context().run,b.execute,r)
            assert ready.wait(8)
            pin.execute("UPDATE users SET first_name='Concurrent synthetic writer' WHERE telegram_user_id=?",(env.employee,))
            pin.commit()
            assert future.result(timeout=10)['data']=={'total':2,'resolved':0}
        assert pin.execute('SELECT first_name FROM users WHERE telegram_user_id=?',(env.employee,)).fetchone()[0]=='Concurrent synthetic writer'
    finally:pin.close()
    following_write(env)


def test_renderer_never_reloads_materialized_catalog(env,monkeypatch):
    from app.handlers import catalog as renderer
    from unittest.mock import AsyncMock
    result=dispatch(env,'catalog.get',target='preparations/_/sea').tool_action.search_results[0]
    monkeypatch.setattr(renderer,'load_catalog',lambda *a:pytest.fail('renderer re-query'))
    send=AsyncMock();monkeypatch.setattr(renderer,'edit_or_send_text',send)
    asyncio.run(renderer._show_item(object(),env.storage.path,result.category.id,result.item.id,materialized=result))
    assert send.await_count==1
    assert 'Облепиха' in send.call_args.args[1]


def test_media_reference_survives_real_worker_without_renderer_requery(env,monkeypatch):
    from app.handlers import catalog as renderer
    from app.models.assistant import AssistantIntent
    from app.services.assistant.orchestrator import AssistantToolRegistry
    from unittest.mock import AsyncMock
    from types import SimpleNamespace
    catalog=env.storage.load()
    media='AgACAgSyntheticOpaqueMediaReference'
    catalog.categories[0].items[0].photo_file_id=media
    action=AssistantToolRegistry(catalog).execute(AssistantIntent(intent='find_tech_card',query='Облепиха'))
    result=action.search_results[0]
    assert result.item.photo_file_id==media
    monkeypatch.setattr(renderer,'load_catalog',lambda *a:pytest.fail('renderer re-query'))
    message=SimpleNamespace(answer_photo=AsyncMock(),answer=AsyncMock())
    asyncio.run(renderer._show_item(message,env.storage.path,result.category.id,result.item.id,materialized=result))
    assert message.answer_photo.await_count==1
    assert message.answer_photo.call_args.kwargs['photo']==media


@pytest.mark.parametrize('unsafe',['/tmp/private.png','file:/tmp/private.png','https://invalid.example/private.png'])
def test_worker_media_result_never_contains_filesystem_or_url_authority(env,unsafe):
    from app.models.assistant import AssistantIntent
    from app.services.assistant.orchestrator import AssistantToolRegistry
    catalog=env.storage.load();catalog.categories[0].items[0].photo_file_id=unsafe
    result=AssistantToolRegistry(catalog).execute(AssistantIntent(intent='find_tech_card',query='Облепиха'))
    assert result.search_results[0].item.photo_file_id is None


@pytest.mark.parametrize('unsafe',['/tmp/private.png','https://invalid.example/private.png'])
def test_parent_rejects_forged_media_in_an_otherwise_valid_result(env,unsafe):
    from app.services.assistant.orchestrator import AssistantToolAction
    from app.services.search_service import SearchResult
    catalog=env.storage.load()
    action=AssistantToolAction(kind='find_tech_card',search_results=(SearchResult(
        category=catalog.categories[0],item=catalog.categories[0].items[0]),))
    wire=broker_module.encode_action(action)
    wire['search_results'][0]['item']['photo_file_id']=unsafe
    with pytest.raises(ReadOnlyViolation):broker_module.decode_action(wire)


def test_protocol_bounds_reject_malformed_or_raw_values():
    from app.services.assistant.read_protocol import ReadResponse,bounded
    for value in ([None]*1001,'x'*12001,{'sql':object()}):
        with pytest.raises(ValueError):bounded(value)
    deep=None
    for _ in range(18):deep=[deep]
    with pytest.raises(ValueError):bounded(deep)


@pytest.mark.parametrize('change',['target','capability'])
def test_worker_cannot_invent_result_target_or_capability(env,monkeypatch,change):
    child_patch(monkeypatch,"""
import app.services.assistant.read_worker as w
original=w._run
def wrong(*args):
 result=original(*args)
 if %r=='target':result['tool_action']['search_results'][0]['item']['id']='invented'
 else:result['capability_id']='warehouse.commit_batch'
 return result
w._run=wrong
""" % change)
    with pytest.raises(ReadOnlyViolation):
        ReadOnlyExecutionBroker().dispatch(env.controller,'catalog.get',env.owner,
            catalog=env.storage.load(),arguments={'entity_ref':'preparations/_/sea'})
    following_write(env)


def test_real_evaluation_worker_infrastructure_failure_is_exit_three(monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace
    from pydantic import SecretStr
    import json
    from app.services.assistant.evaluation_corpus_v2 import select_suite
    from app.services.assistant.live_evaluation import run_live,LiveRunOptions
    from app.services.assistant.providers import HttpResponse
    suite,_=select_suite('extended',0)
    case=next(c for c in suite.cases if c.request.case_id=='P1')
    case=replace(case,request=replace(case.request,max_steps=1))
    child_patch(monkeypatch,"import app.services.assistant.read_worker as w\nw._run=lambda *a:__import__('os')._exit(7)")
    class Wire:
        calls=0
        async def post_json(self,*a,**kw):
            self.calls+=1
            route=dict(dialogue_act='new_request',domain='warehouse',capability_id='warehouse.required_status',
                       target_index=-1,action_code='list',needs_clarification=False,confidence_scaled=10000)
            return HttpResponse(200,json.dumps({'choices':[{'message':{'content':json.dumps(route)}}]}).encode())
    wire=Wire();settings=SimpleNamespace(groq_api_key=SecretStr('synthetic'),groq_planner_model='synthetic')
    report,code=asyncio.run(run_live(LiveRunOptions('live_isolated',True,1,'groq',True,suite='extended'),
        settings_loader=lambda:settings,transport_factory=lambda:wire,corpus=(case,)))
    assert code==3 and not report['ready'] and wire.calls==1
    assert report['rows'][0]['error']=='read_worker_infrastructure'
    assert not broker_module._CHILDREN and kernel._WAKE_USERS==0
