"""Linux seccomp-notify boundary. File opens are supplied by the parent,
never continued against mutable child path pointers. No fork/inherited DB FDs.
"""
from __future__ import annotations

import array
import ctypes as C
import errno
import os
import platform
import socket
import stat
import signal
import threading
from pathlib import Path


class Data(C.Structure):
    _fields_ = [('nr', C.c_int), ('arch', C.c_uint), ('ip', C.c_ulonglong),
                ('args', C.c_ulonglong * 6)]


class Notification(C.Structure):
    _fields_ = [('id', C.c_ulonglong), ('pid', C.c_uint), ('flags', C.c_uint), ('data', Data)]


class Response(C.Structure):
    _fields_ = [('id', C.c_ulonglong), ('val', C.c_longlong), ('error', C.c_int), ('flags', C.c_uint)]


class AddFD(C.Structure):
    _fields_ = [('id', C.c_ulonglong), ('flags', C.c_uint), ('source', C.c_uint),
                ('target', C.c_uint), ('open_flags', C.c_uint)]


class Compare(C.Structure):
    _fields_ = [('arg', C.c_uint), ('op', C.c_int), ('a', C.c_ulonglong), ('b', C.c_ulonglong)]


NOTIFIED = (
    'open', 'openat', 'openat2', 'creat', 'socket', 'socketpair', 'connect', 'bind',
    'listen', 'accept', 'accept4', 'sendto', 'sendmsg', 'sendmmsg', 'recvmsg',
    'clone', 'clone3', 'fork', 'vfork', 'execve', 'execveat', 'ptrace',
    'process_vm_writev', 'process_vm_readv', 'pidfd_getfd', 'pidfd_send_signal',
    'kill', 'tkill', 'tgkill', 'io_uring_setup', 'io_uring_enter', 'io_uring_register',
    'mount', 'umount2', 'unshare', 'setns', 'bpf', 'userfaultfd',
    'open_by_handle_at', 'name_to_handle_at', 'memfd_create',
    'write', 'writev', 'pwrite64', 'pwritev', 'pwritev2', 'truncate', 'ftruncate',
    'copy_file_range', 'sendfile', 'splice', 'tee', 'vmsplice',
    'link', 'linkat', 'unlink', 'unlinkat', 'rename', 'renameat', 'renameat2',
    'mkdir', 'mkdirat', 'rmdir', 'symlink', 'symlinkat', 'chmod', 'fchmod',
    'fchmodat', 'chown', 'fchown', 'lchown', 'utime', 'utimes', 'utimensat',
    'mknod', 'mknodat', 'ioctl', 'dup', 'dup2', 'dup3',
    'fcntl', 'mmap',
)

# Default is NOTIFY/DENY, not default-ALLOW with a best-effort blacklist. New
# kernel syscalls therefore cannot silently gain authority in a future runtime.
PURE_SYSCALLS = (
    'read', 'readv', 'pread64', 'preadv', 'preadv2', 'close', 'lseek',
    'fstat', 'newfstatat', 'stat', 'lstat', 'statx', 'access', 'faccessat',
    'faccessat2', 'readlink', 'readlinkat', 'getcwd',
    'brk', 'munmap', 'mremap', 'mprotect', 'madvise',
    'futex', 'futex_waitv', 'sched_yield', 'sched_getaffinity',
    'rt_sigaction', 'rt_sigprocmask', 'rt_sigreturn', 'sigaltstack',
    'clock_gettime', 'clock_getres', 'clock_nanosleep', 'nanosleep', 'gettimeofday', 'time',
    'getpid', 'getppid', 'gettid', 'getuid', 'geteuid', 'getgid', 'getegid',
    'getrandom', 'getrusage', 'uname', 'sysinfo',
    'poll', 'ppoll', 'select', 'pselect6', 'restart_syscall', 'exit', 'exit_group',
)


def library():
    if platform.system() != 'Linux' or platform.machine() != 'x86_64':
        raise RuntimeError('unsupported_read_sandbox')
    lib = C.CDLL('libseccomp.so.2', use_errno=True)
    lib.seccomp_init.argtypes = [C.c_uint]; lib.seccomp_init.restype = C.c_void_p
    lib.seccomp_rule_add.argtypes = [C.c_void_p, C.c_uint, C.c_int, C.c_uint]
    lib.seccomp_rule_add_array.argtypes = [C.c_void_p, C.c_uint, C.c_int, C.c_uint, C.POINTER(Compare)]
    lib.seccomp_syscall_resolve_name.argtypes = [C.c_char_p]
    lib.seccomp_load.argtypes = [C.c_void_p]
    lib.seccomp_notify_fd.argtypes = [C.c_void_p]
    lib.seccomp_release.argtypes = [C.c_void_p]
    return lib


def install(control: socket.socket):
    """Finish imports first; hand the kernel listener to the trusted broker."""
    import fcntl
    lib = library()
    ctx = lib.seccomp_init(0x7fc00000)
    if not ctx:
        raise RuntimeError('sandbox_init')
    try:
        for name in PURE_SYSCALLS:
            nr = lib.seccomp_syscall_resolve_name(name.encode())
            if nr >= 0 and lib.seccomp_rule_add(ctx, 0x7fff0000, nr, 0):
                raise RuntimeError('sandbox_pure_syscall')
        def allow(name, *comparisons):
            nr = lib.seccomp_syscall_resolve_name(name.encode())
            if nr >= 0:
                array_ = (Compare * len(comparisons))(*comparisons)
                if lib.seccomp_rule_add_array(ctx, 0x7fff0000, nr, len(comparisons), array_):
                    raise RuntimeError('sandbox_conditional_syscall')
        for name in ('write', 'writev'):
            for descriptor in (1, 2):
                allow(name, Compare(0, 4, descriptor, 0))
        for name in ('sendmsg', 'recvfrom'):
            allow(name, Compare(0, 4, control.fileno(), 0))
        for command in (fcntl.F_GETFD, fcntl.F_SETFD, fcntl.F_GETFL,
                        fcntl.F_GETLK, fcntl.F_SETLK, fcntl.F_SETLKW):
            allow('fcntl', Compare(1, 4, command, 0))
        allow('mmap', Compare(2, 7, 2, 0))  # no PROT_WRITE
        allow('mmap', Compare(3, 7, 2, 2))  # MAP_PRIVATE/COW
        allow('mmap', Compare(3, 7, 32, 32))  # anonymous, no external file
        if lib.seccomp_load(ctx):
            raise RuntimeError('sandbox_load')
        listener = lib.seccomp_notify_fd(ctx)
        if listener < 0:
            raise RuntimeError('sandbox_listener')
        control.sendmsg([b'R'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [listener]))])
    finally:
        lib.seccomp_release(ctx)
    # Do not close listener here: the library owns it until process exit; after
    # transfer the child never reads it. All writable parent descriptors absent.
    # Keep only this fixed control socket until the broker acknowledges sealing.


def seal(control):
    control.sendmsg([b'S'])
    if control.recv(1) != b'A':
        raise RuntimeError('sandbox_seal')
    control.close()


def receive_listener(control):
    message, ancillary, flags, _ = control.recvmsg(1, socket.CMSG_SPACE(4))
    descriptors = array.array('i')
    for level, kind, data in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            descriptors.frombytes(data[:len(data)-len(data) % descriptors.itemsize])
    if message != b'R' or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) or len(descriptors) != 1:
        for descriptor in descriptors:
            os.close(descriptor)
        raise RuntimeError('sandbox_handshake')
    os.set_inheritable(descriptors[0], False)
    return descriptors[0]


def receive_seal(control):
    message, ancillary, flags, _ = control.recvmsg(2, socket.CMSG_SPACE(16))
    for level, kind, data in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            descriptors = array.array('i')
            descriptors.frombytes(data[:len(data)-len(data) % descriptors.itemsize])
            for descriptor in descriptors:
                os.close(descriptor)
    if ancillary or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
        raise RuntimeError('sandbox_control')
    return message


def _ioctl(fd, command, obj):
    libc = C.CDLL(None, use_errno=True)
    if libc.ioctl(fd, C.c_ulong(command), C.byref(obj)) < 0:
        raise OSError(C.get_errno(), 'read_kernel_ioctl')


_WAKE_LOCK = threading.Lock()
_WAKE_USERS = 0
_WAKE_SIGNAL = None
_WAKE_OLD = None


class SigSet(C.Structure):
    _fields_ = [('bits', C.c_ulong * 16)]


class SigAction(C.Structure):
    _fields_ = [('handler', C.c_void_p), ('mask', SigSet), ('flags', C.c_int), ('restorer', C.c_void_p)]


class SigEvent(C.Structure):
    _fields_ = [('value', C.c_void_p), ('signo', C.c_int), ('notify', C.c_int),
                ('tid', C.c_int), ('padding', C.c_byte * 44)]


class Timespec(C.Structure):
    _fields_ = [('seconds', C.c_long), ('nanoseconds', C.c_long)]


class TimerSpec(C.Structure):
    _fields_ = [('interval', Timespec), ('value', Timespec)]


class NotificationWakeup:
    """Bound blocking NOTIF_RECV, including the poll→worker-death race.

    A thread-directed POSIX timer interrupts libc ioctl every 100 ms. Its native
    async-signal-safe handler is libc getpid (no Python signal callback/GIL).
    Only an unused realtime signal is borrowed, refcounted and restored. Pending
    thread-directed signals are blocked/drained before restoring dispositions.
    """
    def __enter__(self):
        global _WAKE_USERS, _WAKE_SIGNAL, _WAKE_OLD
        self.lib = C.CDLL(None, use_errno=True)
        self.timer = C.c_void_p(); self.created = False
        with _WAKE_LOCK:
            if not _WAKE_USERS:
                for number in range(signal.SIGRTMIN+4, signal.SIGRTMAX+1):
                    old = SigAction()
                    if self.lib.sigaction(number, None, C.byref(old)) or old.handler:
                        continue
                    action = SigAction()
                    action.handler = C.cast(self.lib.getpid, C.c_void_p).value
                    self.lib.sigemptyset(C.byref(action.mask))
                    if self.lib.sigaction(number, C.byref(action), None):
                        raise RuntimeError('read_wakeup_handler')
                    _WAKE_SIGNAL, _WAKE_OLD = number, old
                    break
                else:
                    raise RuntimeError('read_wakeup_unavailable')
            _WAKE_USERS += 1
            self.signo = _WAKE_SIGNAL
        self.mask = SigSet(); self.old_mask = SigSet()
        self.lib.sigemptyset(C.byref(self.mask)); self.lib.sigaddset(C.byref(self.mask), self.signo)
        self.lib.pthread_sigmask(signal.SIG_UNBLOCK, C.byref(self.mask), C.byref(self.old_mask))
        try:
            event = SigEvent(None, self.signo, 4, threading.get_native_id())
            if self.lib.timer_create(1, C.byref(event), C.byref(self.timer)):
                raise RuntimeError('read_wakeup_timer')
            self.created = True
            spec = TimerSpec(Timespec(0, 100_000_000), Timespec(0, 100_000_000))
            if self.lib.timer_settime(self.timer, 0, C.byref(spec), None):
                raise RuntimeError('read_wakeup_arm')
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *args):
        global _WAKE_USERS, _WAKE_SIGNAL, _WAKE_OLD
        self.lib.pthread_sigmask(signal.SIG_BLOCK, C.byref(self.mask), None)
        try:
            if self.created:
                self.lib.timer_delete(self.timer)
            zero = Timespec(0, 0)
            while self.lib.sigtimedwait(C.byref(self.mask), None, C.byref(zero)) >= 0:
                pass
            with _WAKE_LOCK:
                _WAKE_USERS -= 1
                if not _WAKE_USERS:
                    self.lib.sigaction(_WAKE_SIGNAL, C.byref(_WAKE_OLD), None)
                    _WAKE_SIGNAL = _WAKE_OLD = None
        finally:
            self.lib.pthread_sigmask(signal.SIG_SETMASK, C.byref(self.old_mask), None)


def read_path(pid, address):
    class IOVec(C.Structure):
        _fields_ = [('base', C.c_void_p), ('length', C.c_size_t)]
    buf = C.create_string_buffer(4096)
    local = IOVec(C.addressof(buf), 4096); remote = IOVec(address, 4096)
    libc = C.CDLL(None, use_errno=True)
    libc.process_vm_readv.argtypes = [C.c_int, C.POINTER(IOVec), C.c_ulong,
                                    C.POINTER(IOVec), C.c_ulong, C.c_ulong]
    size = libc.process_vm_readv(pid, C.byref(local), 1, C.byref(remote), 1, 0)
    if size <= 0 or b'\0' not in buf.raw[:size]:
        raise ValueError('read_path_pointer')
    return os.fsdecode(buf.raw[:size].split(b'\0', 1)[0])


def file_identity(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts or str(path).startswith('file:'):
        raise ValueError('read_path')
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError('read_symlink')
    s = path.stat()
    if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1:
        raise ValueError('read_file_identity')
    return s.st_dev, s.st_ino


class FileAuthority:
    """Only main RO and exact SQLite WAL/SHM, opened without following links.

    ADDFD replaces open(), rather than approving a racy child syscall. A second
    path/identity check detects replacement; the supplied fd is the validated
    inode even when another process swaps the pathname back afterwards.
    """
    def __init__(self, path):
        self.path = Path(path)
        self.main = file_identity(path)
        self.identities = {self.path.name: self.main}
        for suffix in ('-wal', '-shm'):
            p = Path(str(self.path)+suffix)
            self.identities[p.name] = file_identity(p) if p.exists() or p.is_symlink() else None
        self.allowed_fds = {}
        self.violation = False
        self.sealed = False
        self.denials = []
        self.notifications = 0
        lib = library()
        self.names = {lib.seccomp_syscall_resolve_name(n.encode()): n for n in NOTIFIED}
        self.root = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        self.root_identity = os.fstat(self.root)
        if self.root_identity.st_mode & 0o022:
            self.close()
            raise ValueError('read_untrusted_root')

    def close(self):
        if self.root >= 0:
            os.close(self.root); self.root = -1

    def check(self):
        s = self.path.parent.stat()
        if (s.st_dev,s.st_ino,s.st_uid,s.st_gid,stat.S_IMODE(s.st_mode)) != (
                self.root_identity.st_dev,self.root_identity.st_ino,self.root_identity.st_uid,
                self.root_identity.st_gid,stat.S_IMODE(self.root_identity.st_mode)):
            raise ValueError('read_root_changed')
        for name, expected in self.identities.items():
            p = self.path.parent/name
            if expected is None:
                if p.exists() or p.is_symlink():
                    raise ValueError('read_sidecar_changed')
            elif file_identity(p) != expected:
                raise ValueError('read_inode_changed')

    def _open(self, path, flags):
        self.check()
        target = Path(path)
        if target.parent != self.path.parent or target.name not in self.identities:
            raise ValueError('read_open_denied_' + ('entropy' if path == '/dev/urandom' else 'other'))
        name = target.name
        is_main = name == self.path.name
        if self.sealed and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
            raise ValueError('read_sealed_open')
        if flags & (os.O_TRUNC | os.O_APPEND | getattr(os, 'O_TMPFILE', 0)):
            raise ValueError('read_open_flags')
        if is_main and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
            raise ValueError('read_main_write')
        expected = self.identities[name]
        actual_flags = flags | os.O_NOFOLLOW | os.O_CLOEXEC
        if is_main:
            actual_flags |= os.O_NOATIME
        if expected is None:
            if not flags & os.O_CREAT or is_main:
                raise FileNotFoundError
            actual_flags |= os.O_EXCL
        fd = os.open(name, actual_flags, 0o600, dir_fd=self.root)
        try:
            s = os.fstat(fd)
            actual = s.st_dev, s.st_ino
            if (not stat.S_ISREG(s.st_mode) or s.st_nlink != 1
                    or (not is_main and actual == self.main)
                    or (expected is not None and actual != expected)):
                raise ValueError('read_open_inode')
            if actual != file_identity(target):
                raise ValueError('read_open_replaced')
            self.identities[name] = actual
            return fd, not is_main
        except BaseException:
            os.close(fd)
            raise

    def handle(self, listener):
        notification = Notification()
        _ioctl(listener, 0xc0502100, notification)
        self.notifications += 1
        name = self.names.get(notification.data.nr, 'unknown')
        response = Response(notification.id, 0, -errno.EPERM, 0)
        try:
            if name in {'open', 'openat'}:
                args = notification.data.args
                path = read_path(notification.pid, args[0 if name == 'open' else 1])
                flags = args[1 if name == 'open' else 2]
                opened, writable = self._open(path, flags)
                try:
                    # SEND atomically replies with an injected, CLOEXEC fd.
                    injection = AddFD(notification.id, 2, opened, 0, os.O_CLOEXEC)
                    libc = C.CDLL(None, use_errno=True)
                    target = libc.ioctl(listener, C.c_ulong(0x40182103), C.byref(injection))
                    if target < 0:
                        raise OSError(C.get_errno(), 'read_addfd')
                    self.allowed_fds[target] = writable
                    return
                finally:
                    os.close(opened)
            # SQLite mmap/write operations may update ONLY approved sidecars.
            if name in {'write', 'writev', 'pwrite64', 'pwritev', 'pwritev2', 'ftruncate'}:
                if not self.sealed and self.allowed_fds.get(notification.data.args[0], False):
                    # CONTINUE is safe here: fd number cannot be duplicated or
                    # replaced; every subsequent open is broker-owned too.
                    response.error = 0; response.flags = 1
                else:
                    self.violation = True
            elif name == 'mmap' and not self.sealed and self.allowed_fds.get(notification.data.args[4], False):
                response.error = 0; response.flags = 1
            elif name == 'fchown' and not self.sealed and self.allowed_fds.get(notification.data.args[0], False):
                owner = self.path.stat()
                if notification.data.args[1] in (owner.st_uid, 0xffffffff) and notification.data.args[2] in (owner.st_gid, 0xffffffff):
                    response.error = 0; response.flags = 1
                else:
                    self.violation = True
            elif name == 'fchmod' and not self.sealed and self.allowed_fds.get(notification.data.args[0], False):
                # Files were already created 0600. Do not broaden their mode to
                # the main DB's potentially more permissive group/other bits.
                response.error = 0
            elif name in {'unlink', 'unlinkat'}:
                # RO SQLite's optional last-close cache deletion is denied. Never
                # unlink a live runtime sidecar or a replaced pathname.
                address = notification.data.args[0 if name == 'unlink' else 1]
                path = Path(read_path(notification.pid, address))
                if path not in {Path(str(self.path)+'-wal'), Path(str(self.path)+'-shm')}:
                    self.violation = True
            else:
                self.violation = True
                self.denials.append(name)
        except FileNotFoundError:
            response.error = -errno.ENOENT
        except (ValueError, OSError) as error:
            self.violation = True
            self.denials.append(str(error) if isinstance(error, ValueError) else 'os_'+str(error.errno))
        try:
            _ioctl(listener, 0xc0182101, response)
        except OSError as error:
            if error.errno != errno.ENOENT:
                raise
