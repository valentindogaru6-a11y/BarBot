"""Linux native fault probes. Synthetic v15 only, no providers or production."""
import asyncio
import contextvars
from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3
import sys
import threading

import pytest

from app.services.assistant import read_broker as broker, read_kernel as kernel
from app.services.assistant.evaluation_environment import IsolatedEvaluationEnvironment
from app.services.assistant.read_broker import ReadOnlyExecutionBroker, ReadWorkerFailure, await_read
from app.services.assistant.read_only import ReadOnlyViolation
from tests.test_assistant_read_process import child_patch, request, following_write

pytestmark = pytest.mark.skipif(sys.platform != 'linux' or os.uname().machine != 'x86_64',reason='Linux x86-64 sandbox')


@pytest.fixture
def env():
    with IsolatedEvaluationEnvironment() as e:
        e.configure('extended')
        yield e


def source_bytes(e):
    return {suffix:hashlib.sha256(Path(str(e.path)+suffix).read_bytes()).hexdigest()
            for suffix in ('','-wal','-shm') if Path(str(e.path)+suffix).exists()}


NATIVE = '''
import ctypes as C,os
import app.services.assistant.read_worker as w
lib=C.CDLL(None,use_errno=True)
mm=lib.mmap;mm.restype=C.c_void_p;mm.argtypes=[C.c_void_p,C.c_size_t,C.c_int,C.c_int,C.c_int,C.c_longlong]
mp=lib.mprotect;mp.argtypes=[C.c_void_p,C.c_size_t,C.c_int]
unmap=lib.munmap;unmap.argtypes=[C.c_void_p,C.c_size_t]
remap=lib.mremap;remap.restype=C.c_void_p;remap.argtypes=[C.c_void_p,C.c_size_t,C.c_size_t,C.c_int]
sync=lib.msync;sync.argtypes=[C.c_void_p,C.c_size_t,C.c_int]
call=lib.syscall;call.restype=C.c_long
native_open=lib.open;native_open.argtypes=[C.c_char_p,C.c_int]
old=w._run
def injected(r,c):
 fd=next(int(f) for f in range(3,128) if _matches(f,r.database))
 p=mm(None,4096,1,1,fd,0)
 if p==C.c_void_p(-1).value:raise AssertionError('missing read map')
 try:
  {body}
 finally:unmap(p,4096)
 return old(r,c)
def _matches(fd,path):
 try:return os.readlink('/proc/self/fd/'+str(fd))==path
 except OSError:return False
w._run=injected
'''


PROBES = [
    ('shared','mm(None,4096,3,1,fd,0)'),
    ('shared_validate','mm(None,4096,3,3,fd,0)'),
    ('upgrade','mp(p,4096,3)'),
    ('partial_upgrade','mp(p+1,2048,3)'),
    ('unknown_range','mp(1,4096,3)'),
    ('pkey_upgrade','call(329,C.c_void_p(p),4096,3,-1)'),
    ('extra_shared_flags','mm(None,4096,3,1|0x8000,fd,0)'),
    ('unknown_sharing','mm(None,4096,3,4,fd,0)'),
    ('extra_private_flags','mm(None,4096,3,2|0x80000000,fd,0)'),
    ('anonymous_shared','mm(None,4096,3,1|0x20,-1,0)'),
    ('mremap','remap(p,4096,8192,0)'),
    ('msync','sync(p,4096,4)'),
    ('remap_file_pages','call(216,C.c_void_p(p),4096,0,0,0)'),
    ('process_vm_writev','call(311,os.getpid(),0,0,0,0,0)'),
    ('proc_mem','native_open(b"/proc/self/mem",2)'),
    ('memfd','call(319,b"synthetic",0)'),
    ('sysv_shm','call(29,0,4096,0o1600)'),
    ('sysv_attach','call(30,0,0,0)'),
    ('posix_shm','native_open(b"/dev/shm/barbot-denied-probe",os.O_RDWR|os.O_CREAT)'),
    ('userfaultfd','call(323,0)'),
    ('io_uring','call(425,1,0)'),
    ('dup','call(32,fd)'),
    ('dup_fcntl','call(72,fd,0,3)'),
    ('fd_passing','call(46,1,0,0)'),
]


@pytest.mark.parametrize('name,body',PROBES,ids=[p[0] for p in PROBES])
def test_native_post_seal_attempt_is_fatal_without_any_backing_change(env,monkeypatch,name,body):
    pin=contextvars.Context().run(sqlite3.connect,env.path)
    pin.execute('SELECT count(*) FROM users').fetchone()
    before=source_bytes(env);state=env.business.snapshot()
    roots=set(Path('/tmp').glob('barbot-read-snapshot-*'));fds=len(os.listdir('/proc/self/fd'))
    child_patch(monkeypatch,NATIVE.replace('{body}',body))
    try:
        b=ReadOnlyExecutionBroker()
        # Ignoring a native syscall's errno must not hide the attempt.
        with pytest.raises(ReadOnlyViolation):b.execute(request(env,b))
        assert before==source_bytes(env) and state==env.business.snapshot()
        assert set(Path('/tmp').glob('barbot-read-snapshot-*'))==roots
        assert len(os.listdir('/proc/self/fd'))==fds
        following_write(env)
    finally:pin.close()


def test_private_cow_cannot_modify_backing_file(env,monkeypatch):
    body='''q=mm(None,4096,3,2,fd,0)
  if q==C.c_void_p(-1).value:raise AssertionError('COW rejected')
  C.c_ubyte.from_address(q).value ^= 0xff
  unmap(q,4096)'''
    child_patch(monkeypatch,NATIVE.replace('{body}',body))
    before=source_bytes(env);state=env.business.snapshot()
    b=ReadOnlyExecutionBroker()
    assert b.execute(request(env,b))['data']=={'total':2,'resolved':0}
    assert before==source_bytes(env) and state==env.business.snapshot()
    following_write(env)


@pytest.mark.parametrize('module',['ctypes','_ctypes','cffi','fractions'])
def test_sealed_imports_fail_closed(env,monkeypatch,module):
    child_patch(monkeypatch,'import app.services.assistant.read_worker as w\nw._run=lambda *a:__import__(%r)' % module)
    b=ReadOnlyExecutionBroker()
    with pytest.raises(ReadOnlyViolation):b.execute(request(env,b))
    following_write(env)


@pytest.mark.parametrize('failure',['error','cancel'])
def test_mapping_cleanup_after_error_or_cancellation(env,monkeypatch,failure):
    body="raise RuntimeError('synthetic')" if failure=='error' else "os.write(1,b'R');__import__('time').sleep(30)"
    child_patch(monkeypatch,NATIVE.replace('{body}',body))
    ready=threading.Event();original=broker.os.read
    def observe(fd,size):
        value=original(fd,size)
        if value==b'R':ready.set()
        return value
    monkeypatch.setattr(broker.os,'read',observe)
    before=source_bytes(env);roots=set(Path('/tmp').glob('barbot-read-snapshot-*'))
    async def run():
        b=ReadOnlyExecutionBroker();r=request(env,b)
        task=asyncio.create_task(await_read(b.execute,r))
        if failure=='cancel':
            for _ in range(1200):
                if ready.is_set():break
                await asyncio.sleep(.01)
            assert ready.is_set()  # Native read mapping exists before cancel.
            task.cancel()
            with pytest.raises(asyncio.CancelledError):await task
        else:
            with pytest.raises(ReadWorkerFailure):await task
    asyncio.run(run())
    assert before==source_bytes(env)
    assert roots==set(Path('/tmp').glob('barbot-read-snapshot-*'))
    following_write(env)
