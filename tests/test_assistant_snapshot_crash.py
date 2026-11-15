"""Exact hot-journal and parent-death regressions, synthetic v15 only."""
import contextvars
from contextlib import closing
import ctypes
import hashlib
import json
import os
from pathlib import Path
import select
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import struct
import asyncio
import threading
import fcntl

import pytest

from app.services.assistant import read_broker as broker,read_lifecycle as lifecycle
from app.services.assistant.read_broker import ReadOnlyExecutionBroker,ReadWorkerFailure
from tests.test_assistant_read_memory import env,source_bytes,managed_roots
from tests.test_assistant_read_process import request,following_write,coordinator_patch


def crashed_delete_writer(e):
    with closing(sqlite3.connect(e.path)) as c:
        c.execute('PRAGMA journal_mode=DELETE')
        c.execute('CREATE TABLE crash_probe(id INTEGER PRIMARY KEY,value INTEGER,pad TEXT)')
        c.executemany('INSERT INTO crash_probe VALUES(?,0,?)',((i,'x'*1000) for i in range(3000)))
        c.commit()
    code='''
import sqlite3,sys,os
c=sqlite3.connect(sys.argv[1])
c.execute('PRAGMA journal_mode=DELETE')
c.execute('PRAGMA cache_size=8');c.execute('PRAGMA cache_spill=ON')
c.execute('PRAGMA synchronous=FULL');c.execute('BEGIN IMMEDIATE')
c.execute("UPDATE users SET first_name='Uncommitted synthetic'")
c.execute('UPDATE crash_probe SET value=1')
os._exit(0)
'''
    subprocess.run([sys.executable,'-I','-B','-c',code,str(e.path)],check=True,timeout=10)
    journal=Path(str(e.path)+'-journal')
    assert journal.stat().st_size>512
    return journal


def test_exact_hot_journal_never_exposes_uncommitted_or_partial_state(env,monkeypatch):
    b=ReadOnlyExecutionBroker();r=request(env,b,identifier='users.list')
    journal=crashed_delete_writer(env)
    before=source_bytes(env);jhash=hashlib.sha256(journal.read_bytes()).digest()
    calls=[];original=broker.subprocess.Popen
    def spawn(command,*a,**kw):
        if 'read_worker import main' in command[-1]:calls.append('executor')
        return original(command,*a,**kw)
    monkeypatch.setattr(broker.subprocess,'Popen',spawn)
    with pytest.raises(ReadWorkerFailure) as error:b.execute(r)
    assert error.value.category=='source_recovery_required'
    assert not calls and before==source_bytes(env) and jhash==hashlib.sha256(journal.read_bytes()).digest()
    # Recovery is a separate, explicitly synthetic RW operation, never AI flow.
    with closing(contextvars.Context().run(sqlite3.connect,env.path)) as c:
        assert c.execute('SELECT count(*) FROM crash_probe WHERE value=1').fetchone()[0]==0
        assert not c.execute("SELECT 1 FROM users WHERE first_name='Uncommitted synthetic'").fetchall()
    b=ReadOnlyExecutionBroker()
    value=b.execute(request(env,b,identifier='users.list'))
    assert all(row['name']!='Uncommitted synthetic' for row in value['data']['users'])
    following_write(env)


@pytest.mark.parametrize('data,allowed',[(b'',True),(b'\0'*1024,True),(b'broken',False),(b'\0'*8,False),(b'bad magic'+b'x'*1024,False)])
def test_journal_empty_stale_and_uncertain_states(env,data,allowed):
    b=ReadOnlyExecutionBroker();r=request(env,b)
    with closing(sqlite3.connect(env.path)) as c:c.execute('PRAGMA journal_mode=DELETE')
    journal=Path(str(env.path)+'-journal');journal.write_bytes(data)
    before=source_bytes(env);digest=hashlib.sha256(journal.read_bytes()).digest()
    try:
        if allowed:assert b.execute(r)['data']=={'total':2,'resolved':0}
        else:
            with pytest.raises(ReadWorkerFailure) as error:b.execute(r)
            assert error.value.category=='source_recovery_required'
        assert before==source_bytes(env) and digest==hashlib.sha256(journal.read_bytes()).digest()
    finally:journal.unlink(missing_ok=True)
    following_write(env)


def test_hot_journal_concurrent_requests_do_not_start_executor(env):
    from concurrent.futures import ThreadPoolExecutor
    pairs=[]
    for _ in range(2):
        b=ReadOnlyExecutionBroker();pairs.append((b,request(env,b)))
    crashed_delete_writer(env);before=source_bytes(env)
    def run(pair):
        with pytest.raises(ReadWorkerFailure) as error:pair[0].execute(pair[1])
        return error.value.category
    with ThreadPoolExecutor(max_workers=2) as p:
        result=[f.result() for f in [p.submit(run,pair) for pair in pairs]]
    assert result==['source_recovery_required']*2 and source_bytes(env)==before


def test_shared_reserved_byte_is_not_proof_of_sqlite_reserved_writer(env):
    b=ReadOnlyExecutionBroker();r=request(env,b)
    journal=crashed_delete_writer(env)
    before=source_bytes(env);digest=journal.read_bytes()
    fd=os.open(env.path,os.O_RDONLY|os.O_CLOEXEC)
    try:
        # SQLite's RESERVED writer takes an exclusive byte lock. A foreign
        # shared lock is not evidence of a live transaction owning this journal.
        fcntl.lockf(fd,fcntl.LOCK_SH|fcntl.LOCK_NB,1,0x40000001,os.SEEK_SET)
        with pytest.raises(ReadWorkerFailure) as error:b.execute(r)
        assert error.value.category=='source_recovery_required'
        assert before==source_bytes(env) and journal.read_bytes()==digest
    finally:os.close(fd)


def test_valid_journal_with_reserved_writer_is_not_misclassified_as_hot(env):
    b=ReadOnlyExecutionBroker();r=request(env,b)
    with closing(sqlite3.connect(env.path)) as c:c.execute('PRAGMA journal_mode=DELETE')
    before=source_bytes(env)
    pin=contextvars.Context().run(sqlite3.connect,env.path);pin.execute('BEGIN IMMEDIATE')
    journal=Path(str(env.path)+'-journal')
    journal.write_bytes(bytes.fromhex('d9d505f920a163d7')+struct.pack('>IIIII',0,0,1,512,4096)+bytes(996))
    digest=journal.read_bytes()
    try:
        assert b.execute(r)['data']=={'total':2,'resolved':0}
        assert before==source_bytes(env) and journal.read_bytes()==digest
    finally:pin.rollback();pin.close();journal.unlink(missing_ok=True)
    following_write(env)


def test_cancellation_during_journal_detection_reaps_every_stage(env,monkeypatch):
    coordinator_patch(monkeypatch,'''
import app.services.assistant.read_snapshot as s,os
original=s.subprocess.Popen
read=os.read
def relay(fd,size):
 value=read(fd,size)
 if value==b'D':os.write(1,b'D')
 return value
os.read=relay
def spawn(command,*a,**kw):
 command=list(command);marker='from app.services.assistant.read_snapshot import main;'
 if marker in command[-1]:
  code="import app.services.assistant.read_snapshot as s,os,time\\ndef slow(*a):\\n os.write(1,b'D');time.sleep(30)\\ns.classify=slow"
  command[-1]=command[-1].replace(marker,marker+'exec('+repr(code)+');')
 return original(command,*a,**kw)
s.subprocess.Popen=spawn
''')
    ready=threading.Event();original=broker.os.read
    def observed(fd,size):
        data=original(fd,size)
        if data==b'D':ready.set()
        return data
    monkeypatch.setattr(broker.os,'read',observed)
    roots=managed_roots();before=source_bytes(env)
    async def run():
        b=ReadOnlyExecutionBroker();r=request(env,b)
        task=asyncio.create_task(broker.await_read(b.execute,r))
        for _ in range(1200):
            if ready.is_set():break
            await asyncio.sleep(.01)
        assert ready.is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
    asyncio.run(run())
    assert before==source_bytes(env) and roots==managed_roots()
    following_write(env)


PARENT_SCRIPT='''
import os,sys,json,time
from pathlib import Path
from app.services.assistant.evaluation_environment import IsolatedEvaluationEnvironment
from app.services.assistant import read_broker as b
e=IsolatedEvaluationEnvironment().__enter__();e.configure('extended')
print(json.dumps(dict(source_root=str(e.root))),flush=True)
original=b.subprocess.Popen;read=b.os.read
worker=None;root=None
def spawn(command,*a,**kw):
 global worker
 command=list(command);mark='from app.services.assistant.read_worker import main;'
 if mark in command[-1]:
  code="import app.services.assistant.read_worker as w,os,time\\ndef slow(*a):\\n os.write(1,b'R');time.sleep(60)\\nw._run=slow"
  command[-1]=command[-1].replace(mark,mark+'exec('+repr(code)+');')
 child=original(command,*a,**kw)
 if mark in command[-1]:worker=child.pid
 return child
orig_auth=b.FileAuthority
class Auth(orig_auth):
 def __init__(self,path):
  global root
  root=str(Path(path).parent);super().__init__(path)
def observed(fd,size):
 data=read(fd,size)
 if data==b'R':print(json.dumps(dict(worker=worker,root=root)),flush=True)
 return data
b.subprocess.Popen=spawn;b.FileAuthority=Auth;b.os.read=observed
bb=b.ReadOnlyExecutionBroker(timeout=30)
bb.execute(bb.request('warehouse.required_status',access=e.access,actor=e.owner,policy=e.policy,flags=e.controller.flags,registry=e.registry,catalog=e.storage.load()))
'''


@pytest.mark.parametrize('death',[signal.SIGTERM,signal.SIGKILL])
def test_parent_death_kills_executor_and_startup_recovers_orphan(death):
    libc=ctypes.CDLL(None);previous=ctypes.c_int()
    assert libc.prctl(37,ctypes.byref(previous),0,0,0)==0
    libc.prctl(36,1,0,0,0)
    child=subprocess.Popen([sys.executable,'-B','-u','-c',PARENT_SCRIPT],stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,cwd='/opt/barbot',start_new_session=True)
    source=None;info=None
    try:
        assert select.select([child.stdout],[],[],15)[0]
        source=Path(json.loads(child.stdout.readline())['source_root'])
        assert select.select([child.stdout],[],[],15)[0]
        info=json.loads(child.stdout.readline());root=Path(info['root'])
        os.kill(child.pid,death);child.wait(3)
        until=time.monotonic()+3
        while lifecycle.process_identity(info['worker']) is not None:
            assert time.monotonic()<until
            time.sleep(.01)
        try:os.waitpid(info['worker'],0)
        except ChildProcessError:pass
        lifecycle.scavenge()
        assert not root.exists()
    finally:
        if child.poll() is None:child.kill();child.wait(3)
        if info:
            try:os.kill(info['worker'],signal.SIGKILL);os.waitpid(info['worker'],0)
            except (ProcessLookupError,ChildProcessError):pass
            lifecycle.scavenge()
        if source is not None and source.exists():
            assert source.parent==Path('/tmp') and source.name.startswith('barbot-live-eval-') and not source.is_symlink()
            shutil.rmtree(source)
        child.stdout.close();libc.prctl(36,previous.value,0,0,0)
