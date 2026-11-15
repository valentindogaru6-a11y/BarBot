"""Parent-owned, bounded SQLite Backup API snapshot; never an executor host.

The fixed helper has RO authority over the source, including WAL/SHM. SQLite's
RW-SHM probe gets EACCES and may retry RO. If RO WAL cannot be read, fail closed.
Immutable is never used for a source. Idle WAL may use a stable committed main
copy under an unbroken read lease, followed by NORMAL private SQLite/Backup API.
No source sidecars are created/repaired. Only owned staging/destination are RW.
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
from pathlib import Path
import resource
import selectors
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import ctypes as C
from time import monotonic

from app.services.assistant.read_kernel import (
    FileAuthority, NotificationWakeup, file_identity, install, seal,
    receive_listener, receive_seal,
)
from app.services.assistant.read_lifecycle import ManagedRoot, LifecycleError, MAX_SNAPSHOT, FILES, BACKUP_SECONDS
from app.services.assistant.read_journal import classify, RecoveryRequired
from app.services.assistant.read_process import arm_parent_death

MAX_INPUT = 32768


class SnapshotAuthority(FileAuthority):
    def __init__(self, source, destination):
        super().__init__(source)
        self.read_only_fallback = True
        journal=Path(str(self.path)+'-journal')
        try:self.identities[journal.name]=file_identity(journal) if journal.exists() or journal.is_symlink() else None
        except (OSError,ValueError):
            self.close()
            raise RecoveryRequired from None
        self.destination = Path(destination)
        self.destination_identity = file_identity(self.destination)
        self.created = {self.destination.parent/name:file_identity(self.destination.parent/name) for name in FILES}
        self.dest_paths = set(self.created)
        self.destination_root = self.destination.parent.stat()
        if (stat.S_IMODE(self.destination_root.st_mode) != 0o700
                or self.destination_root.st_uid != os.geteuid()):
            self.close()
            raise ValueError('snapshot_root')

    def check(self):
        super().check()
        s = self.destination.parent.lstat()
        expected = self.destination_root
        if (s.st_dev, s.st_ino, s.st_uid, s.st_mode) != (
                expected.st_dev, expected.st_ino, expected.st_uid, expected.st_mode):
            raise ValueError('snapshot_root_changed')
        if file_identity(self.destination) != self.destination_identity:
            raise ValueError('snapshot_replaced')
        for path, identity in self.created.items():
            if file_identity(path) != identity:
                raise ValueError('snapshot_sidecar_replaced')

    def _open(self, path, flags):
        target = Path(path)
        if target == self.destination.parent:
            self.check()
            if self.sealed or flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT):
                raise ValueError('snapshot_directory_open')
            fd=os.open(target,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
            s=os.fstat(fd)
            if (s.st_dev,s.st_ino)!=(self.destination_root.st_dev,self.destination_root.st_ino):
                os.close(fd);raise ValueError('snapshot_directory_identity')
            return fd,True
        if target not in self.dest_paths:
            return super()._open(path, flags)
        self.check()
        if self.sealed or flags & (os.O_TRUNC | os.O_APPEND | getattr(os, 'O_TMPFILE', 0)):
            raise ValueError('snapshot_open')
        expected = self.created.get(target)
        if expected is None and not flags & os.O_CREAT:raise FileNotFoundError
        fd = os.open(target, flags | os.O_NOFOLLOW | os.O_CLOEXEC | (os.O_EXCL if expected is None else 0),0o600)
        actual = os.fstat(fd).st_dev, os.fstat(fd).st_ino
        if (expected is not None and actual != expected) or actual != file_identity(target):
            os.close(fd)
            raise ValueError('snapshot_open_identity')
        self.created[target] = actual
        return fd, bool(flags & (os.O_WRONLY|os.O_RDWR))

    def unlink_owned(self, path):
        if self.sealed or path == self.destination or path not in self.created:
            return False
        self.check()
        path.unlink()
        del self.created[path]
        return True


class OwnedSnapshot(ManagedRoot):
    pass


def _produce(source, identity, owned, *, deadline, cancel):
    """Runs only in the coordinator, never in the live SQLite-owning parent."""
    from app.services.assistant import read_broker as broker
    from app.services.assistant.read_only import ReadOnlyViolation
    authority = child = control = peer = listener = None
    started = monotonic()
    try:
        authority = SnapshotAuthority(source, owned.path)
        if authority.main != identity:
            raise ReadOnlyViolation('read.snapshot', 'source_identity')
        control, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        script = (f'import sys;sys.path.insert(0,{str(broker.PROJECT)!r});'
                  f'from app.services.assistant.read_process import arm_parent_death;arm_parent_death({os.getpid()});'
                  f'from app.services.assistant.read_snapshot import main;main({peer.fileno()},{os.getpid()})')
        child = subprocess.Popen([sys.executable, '-I', '-B', '-c', script], cwd=broker.PROJECT,
            env={'LANG':'C.UTF-8','TZ':'UTC'}, close_fds=True, pass_fds=(peer.fileno(),),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        peer.close(); peer = None
        with broker._LOCK:
            broker._CHILDREN.add(child)
        raw = json.dumps(dict(source=str(source), destination=str(owned.path), deadline=deadline,
            identities=list(v for v in authority.identities.values() if v is not None)+list(authority.created.values()))).encode()
        os.set_blocking(child.stdin.fileno(), False); os.set_blocking(child.stdout.fileno(), False)
        offset = 0; output = bytearray(); sealed = False; eof = False
        with NotificationWakeup(), selectors.DefaultSelector() as selector:
            selector.register(control, selectors.EVENT_READ, 'control')
            selector.register(child.stdin, selectors.EVENT_WRITE, 'input')
            selector.register(child.stdout, selectors.EVENT_READ, 'output')
            while not eof:
                if monotonic() >= deadline or cancel and cancel.is_set():
                    raise broker.ReadWorkerFailure('snapshot_timeout')
                for key, _ in selector.select(.025):
                    if key.data == 'control':
                        if listener is None:
                            listener = receive_listener(control)
                            selector.register(listener, selectors.EVENT_READ, 'kernel')
                        else:
                            packet = receive_seal(control)
                            if not packet:
                                selector.unregister(control);control.close();control=None
                                continue
                            if packet==b'J':
                                # A genuine recovery-required refusal must not
                                # hide an already observed identity replacement.
                                authority.check()
                                raise RecoveryRequired
                            if packet != b'S' or sealed:
                                raise ReadOnlyViolation('read.snapshot','protocol')
                            authority.check(); authority.sealed = True; sealed = True
                            control.send(b'A')
                            selector.unregister(control); control.close(); control = None
                    elif key.data == 'kernel':
                        if child.poll() is not None:
                            selector.unregister(listener); continue
                        try:authority.handle(listener)
                        except OSError as error:
                            if error.errno not in (errno.EINTR, errno.ENOENT):raise
                    elif key.data == 'input':
                        offset += os.write(child.stdin.fileno(), raw[offset:offset+8192])
                        if offset == len(raw):
                            selector.unregister(child.stdin); child.stdin.close()
                    else:
                        chunk = os.read(child.stdout.fileno(), 256)
                        if not chunk:eof = True;break
                        output.extend(chunk)
                        if len(output) > 256:raise broker.ReadWorkerFailure('snapshot_protocol')
        child.wait(timeout=1)
        authority.check()
        if authority.violation:
            raise ReadOnlyViolation('read.snapshot','filesystem_boundary')
        if child.returncode or not sealed or bytes(output) != b'{"ok":true}':
            raise broker.ReadWorkerFailure('snapshot_unavailable')
        s = owned.path.stat()
        if not 0 < s.st_size <= MAX_SNAPSHOT or stat.S_IMODE(s.st_mode) != 0o600:
            raise ReadOnlyViolation('read.snapshot','snapshot_bounds')
        owned.elapsed_ms = (monotonic()-started)*1000
        return owned
    except ValueError:
        raise ReadOnlyViolation('read.snapshot','source_identity') from None
    except (OSError, RuntimeError, subprocess.SubprocessError):
        raise broker.ReadWorkerFailure('snapshot_unavailable') from None
    finally:
        try:
            if child:broker._stop(child)
        finally:
            try:
                if listener is not None:os.close(listener)
                if control:control.close()
                if peer:peer.close()
                if authority:authority.close()
            finally:
                with broker._LOCK:broker._CHILDREN.discard(child)


def prepare(source, identity, *, deadline, cancel):
    """The live parent never opens/closes a source inode (POSIX lock safety)."""
    from app.services.assistant import read_broker as broker
    from app.services.assistant.read_only import ReadOnlyViolation
    deadline=min(deadline,monotonic()+BACKUP_SECONDS)
    try:owned=OwnedSnapshot()
    except LifecycleError as error:raise broker.ReadWorkerFailure(error.category) from None
    child=None;success=False
    try:
        script=(f'import sys;sys.path.insert(0,{str(broker.PROJECT)!r});'
                f'from app.services.assistant.read_process import arm_parent_death;arm_parent_death({os.getpid()},15);'
                f'from app.services.assistant.read_snapshot import coordinate;coordinate({os.getpid()})')
        child=subprocess.Popen([sys.executable,'-I','-B','-c',script],cwd=broker.PROJECT,
            env={'LANG':'C.UTF-8','TZ':'UTC'},close_fds=True,start_new_session=True,
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
        with broker._LOCK:broker._CHILDREN.add(child)
        owned.register(child)
        raw=json.dumps(dict(source=str(source),identity=identity,destination=str(owned.path),
            destination_identity=owned.identity,deadline=deadline)).encode()
        if len(raw)>4096:raise ReadOnlyViolation('read.snapshot','input_bounds')
        child.stdin.write(raw);child.stdin.close()
        os.set_blocking(child.stdout.fileno(),False)
        out=bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout,selectors.EVENT_READ)
            while True:
                if monotonic()>=deadline or cancel and cancel.is_set():
                    raise broker.ReadWorkerFailure('snapshot_timeout')
                if not selector.select(.025):continue
                chunk=os.read(child.stdout.fileno(),512)
                if not chunk:break
                out.extend(chunk)
                if len(out)>512:raise ReadOnlyViolation('read.snapshot','output_bounds')
        child.wait(timeout=1)
        try:result=json.loads(out)
        except (ValueError,TypeError):raise broker.ReadWorkerFailure('snapshot_unavailable') from None
        if result=={'status':'safety'}:raise ReadOnlyViolation('read.snapshot','filesystem_boundary')
        if result=={'status':'source_recovery_required'}:raise broker.ReadWorkerFailure('source_recovery_required')
        if child.returncode or not isinstance(result,dict) or set(result)!={'status','elapsed_ms'} or result['status']!='ok':
            raise broker.ReadWorkerFailure('snapshot_unavailable')
        owned.finalize();owned.check()
        if not 0<owned.path.stat().st_size<=MAX_SNAPSHOT:
            raise ReadOnlyViolation('read.snapshot','snapshot_identity')
        owned.elapsed_ms=result['elapsed_ms'];success=True
        return owned
    except (OSError,ValueError,subprocess.SubprocessError):
        raise broker.ReadWorkerFailure('snapshot_unavailable') from None
    finally:
        try:
            if child:
                if child.poll() is None:
                    child.terminate()
                    try:child.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid,signal.SIGKILL);child.wait(timeout=2)
                broker._stop(child)
        finally:
            with broker._LOCK:broker._CHILDREN.discard(child)
            if not success:
                try:
                    if owned.root.exists():owned.close()
                except (OSError,ValueError):
                    raise ReadOnlyViolation('read.snapshot','snapshot_cleanup') from None


def coordinate(parent):
    """Fixed exec supervisor. No SQLite handles from the live parent survive."""
    from app.services.assistant import read_broker as broker
    from app.services.assistant.read_only import ReadOnlyViolation
    cancel=threading.Event();owned=None;result={'status':'infrastructure'}
    signal.signal(signal.SIGTERM,lambda *_:cancel.set())
    arm_parent_death(parent,signal.SIGTERM)
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    resource.setrlimit(resource.RLIMIT_AS,(192*1024*1024,192*1024*1024))
    try:
        raw=sys.stdin.buffer.read(4097)
        r=json.loads(raw) if len(raw)<=4096 else None
        if not isinstance(r,dict) or set(r)!={'source','identity','destination','destination_identity','deadline'}:
            raise ValueError
        owned=OwnedSnapshot.attach(r['destination'])
        if (owned.path.name!='read.sqlite' or stat.S_IMODE(owned.root_identity.st_mode)!=0o700
                or owned.root_identity.st_uid!=os.geteuid() or file_identity(owned.path)!=owned.identity):
            raise ValueError
        if os.getppid()!=parent:cancel.set()
        _produce(r['source'],tuple(r['identity']),owned,deadline=r['deadline'],cancel=cancel)
        result={'status':'ok','elapsed_ms':owned.elapsed_ms}
    except ReadOnlyViolation:result={'status':'safety'}
    except RecoveryRequired:result={'status':'source_recovery_required'}
    except BaseException:pass
    finally:
        broker.shutdown_workers()
        if os.getppid()!=parent and owned is not None:
            try:owned.close()
            except BaseException:pass
    sys.stdout.buffer.write(json.dumps(result,separators=(',',':')).encode());sys.stdout.buffer.flush()


def _main(control_fd,parent):
    """Fixed trusted backup program. No imports of tools/services/providers."""
    arm_parent_death(parent)
    raw = sys.stdin.buffer.read(MAX_INPUT+1)
    request = json.loads(raw) if len(raw) <= MAX_INPUT else None
    if not isinstance(request,dict) or set(request) != {'source','destination','deadline','identities'}:
        raise SystemExit(2)
    source, destination = Path(request['source']), Path(request['destination'])
    source_uri = source.as_uri()+'?mode=ro'
    destination_uri = destination.as_uri()+'?mode=rw'
    # Parent validates identities and supplies every file descriptor via ADDFD.
    resource.setrlimit(resource.RLIMIT_CORE, (0,0))
    resource.setrlimit(resource.RLIMIT_CPU, (4,4))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_SNAPSHOT,MAX_SNAPSHOT))
    resource.setrlimit(resource.RLIMIT_AS, (192*1024*1024,192*1024*1024))
    resource.setrlimit(resource.RLIMIT_NOFILE,(64,64))
    control = socket.socket(fileno=control_fd)
    broken = False
    def lease_break(*_):
        nonlocal broken
        broken = True
    signal.signal(signal.SIGIO,lease_break)
    warm=sqlite3.connect(':memory:')
    warm.execute('SELECT randomblob(16)').fetchone()
    warm.close()
    install(control)
    lease = None
    database_fd=os.open(source,os.O_RDONLY|os.O_CLOEXEC|os.O_NOFOLLOW|os.O_NOATIME)
    try:classify(Path(str(source)+'-journal'),database_fd)
    except RecoveryRequired:
        control.sendmsg([b'J']);raise SystemExit(2)
    wal = Path(str(source)+'-wal')
    if not wal.exists() or wal.stat().st_size == 0:
        # A kernel READ lease fails if any writable handle/mapping already
        # exists, and blocks new writers with a lease-break notification.
        # An idle WAL DB may need private auxiliary files. Only after excluding
        # writers AND hot journals, copy its stable main bytes to owned staging.
        # Use NORMAL SQLite there, then Backup API to the final clone. Source
        # immutable is never used; staging is never sent to an executor.
        lease = database_fd
        try:fcntl.fcntl(lease,fcntl.F_SETLEASE,fcntl.F_RDLCK)
        except OSError:lease=None
        if lease is not None:
            try:classify(Path(str(source)+'-journal'),database_fd)
            except RecoveryRequired:
                control.sendmsg([b'J']);raise SystemExit(2)
            if wal.exists() and wal.stat().st_size:
                raise SystemExit(2)
            if os.pread(database_fd,20,0)[18:20]==b'\x02\x02':
                stage=destination.parent/'stage.sqlite'
                size=os.fstat(database_fd).st_size
                if not 0<size<=MAX_SNAPSHOT:raise SystemExit(2)
                out=os.open(stage,os.O_RDWR|os.O_NOFOLLOW|os.O_CLOEXEC)
                try:
                    offset=0
                    while offset<size:
                        if broken or monotonic()>=request['deadline']:raise SystemExit(2)
                        chunk=os.pread(database_fd,min(65536,size-offset),offset)
                        if not chunk:raise SystemExit(2)
                        written=0
                        while written<len(chunk):written+=os.write(out,chunk[written:])
                        offset+=len(chunk)
                    os.ftruncate(out,size)
                finally:os.close(out)
                if broken or fcntl.fcntl(lease,fcntl.F_GETLEASE)!=fcntl.F_RDLCK:raise SystemExit(2)
                # All journals/WALs at this path are private, not source files.
                stage_uri=stage.as_uri()
                private=sqlite3.connect(stage_uri+'?mode=rw',uri=True,timeout=.1)
                try:private.execute('PRAGMA journal_mode=DELETE')
                finally:private.close()
                source_uri=stage_uri+'?mode=ro'
    with sqlite3.connect(source_uri,uri=True,timeout=.1) as src:
        src.execute('PRAGMA query_only=ON')
        src.execute('PRAGMA cache_size=-1024')
        src.execute('PRAGMA temp_store=MEMORY')
        src.execute('BEGIN')
        size = src.execute('PRAGMA page_size').fetchone()[0] * src.execute('PRAGMA page_count').fetchone()[0]
        if not 0 < size <= MAX_SNAPSHOT:raise SystemExit(2)
        approved={tuple(v) for v in request['identities']}
        for descriptor in range(3,64):
            try:s=os.fstat(descriptor)
            except OSError:continue
            if stat.S_ISREG(s.st_mode) and (s.st_dev,s.st_ino) not in approved:
                raise SystemExit(2)
        with sqlite3.connect(destination_uri,uri=True,timeout=.1) as dst:
            dst.execute('PRAGMA journal_mode=MEMORY')
            dst.execute('PRAGMA cache_size=-1024')
            dst.execute('PRAGMA temp_store=MEMORY')
            def progress(*_):
                if monotonic() >= request['deadline']:raise TimeoutError
            src.backup(dst,pages=64,progress=progress,sleep=.01)
            dst.execute('PRAGMA journal_mode=DELETE')
            if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or dst.execute('PRAGMA foreign_key_check').fetchone():
                raise SystemExit(2)
        dst.close()
    src.close()
    if lease is not None:
        if broken or fcntl.fcntl(lease,fcntl.F_GETLEASE) != fcntl.F_RDLCK or wal.exists() and wal.stat().st_size:
            raise SystemExit(2)
        fcntl.fcntl(lease,fcntl.F_SETLEASE,fcntl.F_UNLCK)
    os.close(database_fd)
    seal(control)
    sys.stdout.buffer.write(b'{"ok":true}');sys.stdout.buffer.flush()


def main(control_fd,parent):
    try:_main(control_fd,parent)
    except BaseException:
        # No traceback/linecache file opens after confinement, no raw SQL/path.
        raise SystemExit(2) from None
