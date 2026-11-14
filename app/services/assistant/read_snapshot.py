"""Parent-owned, bounded SQLite Backup API snapshot; never an executor host.

The fixed helper has RO authority over the source, including WAL/SHM. SQLite's
RW-SHM probe gets EACCES and may retry RO. If RO WAL cannot be read, fail closed.
Immutable is limited to absent/empty WAL protected by an unbroken kernel read
lease. No source sidecars are created/repaired. Only the owned clone is RW.
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
import tempfile
import threading
import ctypes as C
from time import monotonic

from app.services.assistant.read_kernel import (
    FileAuthority, NotificationWakeup, file_identity, install, seal,
    receive_listener, receive_seal,
)

MAX_SNAPSHOT = 64 * 1024 * 1024
MAX_INPUT = 32768


class SnapshotAuthority(FileAuthority):
    def __init__(self, source, destination):
        super().__init__(source)
        self.read_only_fallback = True
        self.destination = Path(destination)
        self.destination_identity = file_identity(self.destination)
        self.created = {self.destination: self.destination_identity}
        self.dest_paths = {Path(str(self.destination)+suffix) for suffix in ('','-wal','-shm','-journal')}
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
        return fd, True

    def unlink_owned(self, path):
        if self.sealed or path == self.destination or path not in self.created:
            return False
        self.check()
        path.unlink()
        del self.created[path]
        return True


class OwnedSnapshot:
    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix='barbot-read-snapshot-'))
        self.root_identity = self.root.stat()
        self.path = self.root / 'read.sqlite'
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_RDWR, 0o600)
        except BaseException:
            self.root.rmdir()
            raise
        os.close(descriptor)
        self.identity = file_identity(self.path)

    def check(self):
        s=self.root.lstat();old=self.root_identity
        if (s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_gid)!=(old.st_dev,old.st_ino,old.st_mode,old.st_uid,old.st_gid):
            raise ValueError('snapshot_root_identity')
        f=self.path.lstat()
        if (file_identity(self.path)!=self.identity or stat.S_IMODE(f.st_mode)!=0o600
                or f.st_uid!=old.st_uid):
            raise ValueError('snapshot_file_identity')

    def close(self):
        s = self.root.lstat()
        if (s.st_dev, s.st_ino) != (self.root_identity.st_dev, self.root_identity.st_ino) or stat.S_ISLNK(s.st_mode):
            raise ValueError('snapshot_cleanup_root')
        names=set(os.listdir(self.root))
        if not names <= {self.path.name+suffix for suffix in ('','-wal','-shm','-journal')} or file_identity(self.path) != self.identity:
            raise ValueError('snapshot_cleanup_identity')
        for name in names:
            file_identity(self.root/name)
        for name in names:(self.root/name).unlink()
        self.root.rmdir()


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
                  f'from app.services.assistant.read_snapshot import main;main({peer.fileno()},{os.getpid()})')
        child = subprocess.Popen([sys.executable, '-I', '-B', '-c', script], cwd=broker.PROJECT,
            env={'LANG':'C.UTF-8','TZ':'UTC'}, close_fds=True, pass_fds=(peer.fileno(),),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        peer.close(); peer = None
        with broker._LOCK:
            broker._CHILDREN.add(child)
        raw = json.dumps(dict(source=str(source), destination=str(owned.path), deadline=deadline,
            identities=list(v for v in authority.identities.values() if v is not None))).encode()
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
    owned=OwnedSnapshot();child=None;success=False
    try:
        script=(f'import sys;sys.path.insert(0,{str(broker.PROJECT)!r});'
                f'from app.services.assistant.read_snapshot import coordinate;coordinate({os.getpid()})')
        child=subprocess.Popen([sys.executable,'-I','-B','-c',script],cwd=broker.PROJECT,
            env={'LANG':'C.UTF-8','TZ':'UTC'},close_fds=True,start_new_session=True,
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
        with broker._LOCK:broker._CHILDREN.add(child)
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
        if child.returncode or not isinstance(result,dict) or set(result)!={'status','elapsed_ms'} or result['status']!='ok':
            raise broker.ReadWorkerFailure('snapshot_unavailable')
        owned.check()
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
    C.CDLL(None).prctl(1,signal.SIGTERM,0,0,0)
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    resource.setrlimit(resource.RLIMIT_AS,(192*1024*1024,192*1024*1024))
    try:
        raw=sys.stdin.buffer.read(4097)
        r=json.loads(raw) if len(raw)<=4096 else None
        if not isinstance(r,dict) or set(r)!={'source','identity','destination','destination_identity','deadline'}:
            raise ValueError
        owned=object.__new__(OwnedSnapshot)
        owned.path=Path(r['destination']);owned.root=owned.path.parent
        owned.root_identity=owned.root.lstat();owned.identity=tuple(r['destination_identity'])
        if (owned.path.name!='read.sqlite' or stat.S_IMODE(owned.root_identity.st_mode)!=0o700
                or owned.root_identity.st_uid!=os.geteuid() or file_identity(owned.path)!=owned.identity):
            raise ValueError
        if os.getppid()!=parent:cancel.set()
        _produce(r['source'],tuple(r['identity']),owned,deadline=r['deadline'],cancel=cancel)
        result={'status':'ok','elapsed_ms':owned.elapsed_ms}
    except ReadOnlyViolation:result={'status':'safety'}
    except BaseException:pass
    finally:
        broker.shutdown_workers()
        if os.getppid()!=parent and owned is not None:
            try:owned.close()
            except BaseException:pass
    sys.stdout.buffer.write(json.dumps(result,separators=(',',':')).encode());sys.stdout.buffer.flush()


def _main(control_fd,parent):
    """Fixed trusted backup program. No imports of tools/services/providers."""
    C.CDLL(None).prctl(1,signal.SIGKILL,0,0,0)
    if os.getppid()!=parent:raise SystemExit(2)
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
    wal = Path(str(source)+'-wal')
    if not wal.exists() or wal.stat().st_size == 0:
        # A kernel READ lease fails if any writable handle/mapping already
        # exists, and blocks new writers with a lease-break notification.
        # Only under this lease AND absent/empty WAL may immutable be used:
        # there are provably no WAL frames to omit. Validate again before
        # releasing the lease; a break discards the clone, never succeeds.
        lease = os.open(source,os.O_RDONLY|os.O_CLOEXEC|os.O_NOFOLLOW|os.O_NOATIME)
        try:fcntl.fcntl(lease,fcntl.F_SETLEASE,fcntl.F_RDLCK)
        except OSError:os.close(lease);lease=None
        if lease is not None:
            if wal.exists() and wal.stat().st_size:
                raise SystemExit(2)
            source_uri += '&immutable=1'
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
        os.close(lease)
    seal(control)
    sys.stdout.buffer.write(b'{"ok":true}');sys.stdout.buffer.flush()


def main(control_fd,parent):
    try:_main(control_fd,parent)
    except BaseException:
        # No traceback/linecache file opens after confinement, no raw SQL/path.
        raise SystemExit(2) from None
