"""Fail-closed AI tool boundary, not a sandbox for arbitrary Python code.

Repositories use connection-per-operation. Connections opened inside a read tool
are query-only and closed at scope exit; no pooled connection state is changed.
The audit hook is process-wide but inactive outside the context-local tool scope.
Owner replies/rendering and conversation persistence happen OUTSIDE this scope.
"""

import asyncio
from contextlib import contextmanager, ExitStack
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
import os
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor


class ReadOnlyViolation(BaseException):
    """Must bypass provider/general Exception fallback after tool entry."""

    def __init__(self, capability, category):
        self.effects = self.operations = 0
        self.capability, self.category = capability, category
        super().__init__('read_only_capability_violation')


@dataclass
class _Scope:
    capability: str
    category: str | None = None
    connections: list = field(default_factory=list)


_ACTIVE = ContextVar('assistant_read_only_execution', default=None)
_LOCAL = ContextVar('assistant_local_dispatch_boundary', default=None)
_OPENING = ContextVar('assistant_read_only_connect', default=False)
_BYTECODE_LOCK = threading.Lock()
_BYTECODE_USERS = 0
_BYTECODE_ORIGINAL = False


def deny(category):
    scope = _ACTIVE.get()
    if scope is not None:
        scope.category = scope.category or category
        raise ReadOnlyViolation(scope.capability, scope.category)


def authorize_sql(action, name, value, database, source):
    """Only read opcodes and connection-local safe setup; no SQL/value logging."""
    scope = _ACTIVE.get()
    if scope is None:
        return sqlite3.SQLITE_OK
    allowed = action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE, sqlite3.SQLITE_TRANSACTION,
        sqlite3.SQLITE_SAVEPOINT)
    if action == sqlite3.SQLITE_FUNCTION:
        allowed = str(value).casefold() in {
            'count','sum','total','avg','min','max','coalesce','ifnull','nullif',
            'lower','upper','trim','ltrim','rtrim','length','substr','substring',
            'replace','instr','like','glob','abs','round','printf','format',
            'date','datetime','time','strftime','julianday','unixepoch',
            'json_valid','json_extract','json_type','json_array_length',
            'group_concat','hex','typeof','quote','iif','sqlite_version'}
    if action == sqlite3.SQLITE_PRAGMA:
        allowed = (name in {'table_info','table_xinfo','index_info','index_list',
            'foreign_key_list','foreign_key_check','integrity_check','quick_check'} or
            (value is None and name in {'user_version','schema_version','query_only',
                'foreign_keys','busy_timeout','journal_mode'}) or
            (name in {'foreign_keys','query_only'} and str(value).casefold() in {'on','1'}) or
            (name == 'busy_timeout' and str(value).isdigit()))
    if not allowed:
        scope.category = scope.category or 'sqlite_write'
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _audit(event, args):
    scope = _ACTIVE.get()
    if scope is None:
        return
    if event == 'sqlite3.connect':
        if not _OPENING.get():
            deny('sqlite_open_boundary')
        raw = os.fsdecode(args[0])
        # Never create a missing DB as a side effect of a read executor.
        if raw == ':memory:' or raw.startswith('file:') or not os.path.isfile(raw):
            deny('sqlite_open')
    elif event == 'sqlite3.connect/handle':
        scope.connections.append(args[0])
    elif event == 'open':
        flags = args[2]
        if isinstance(flags, int) and flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND):
            deny('filesystem_write')
    elif event in {'os.mkdir','os.remove','os.rmdir','os.rename','os.link','os.symlink',
                   'os.chmod','os.chown','os.truncate','os.utime','subprocess.Popen',
                   'os.system','os.posix_spawn','socket.connect','socket.getaddrinfo',
                   'socket.sendto','ctypes.dlopen'}:
        deny('external_side_effect')


sys.addaudithook(_audit)
_connect = sqlite3.connect


def _query_only_connect(*args, **kwargs):
    token = _OPENING.set(True)
    try:
        connection = _connect(*args, **kwargs)
    finally:
        _OPENING.reset(token)
    if _ACTIVE.get() is not None:
        connection.execute('PRAGMA query_only=ON')
        connection.set_authorizer(authorize_sql)
    return connection


sqlite3.connect = _query_only_connect
_thread_start = threading.Thread.start


def _start_thread(thread, *args, **kwargs):
    deny('background_task')
    return _thread_start(thread, *args, **kwargs)


threading.Thread.start = _start_thread


def _background_boundary(function):
    @wraps(function)
    def call(*args, **kwargs):
        deny('background_task')
        return function(*args, **kwargs)
    return call


ThreadPoolExecutor.submit = _background_boundary(ThreadPoolExecutor.submit)
ProcessPoolExecutor.submit = _background_boundary(ProcessPoolExecutor.submit)


@contextmanager
def read_only_execution(capability):
    global _BYTECODE_USERS, _BYTECODE_ORIGINAL
    if _ACTIVE.get() is not None:
        yield
        return
    scope = _Scope(capability)
    token = _ACTIVE.set(scope)
    with _BYTECODE_LOCK:
        if not _BYTECODE_USERS:
            _BYTECODE_ORIGINAL = sys.dont_write_bytecode
        _BYTECODE_USERS += 1
        sys.dont_write_bytecode = True
    loop = None
    factory = None
    scheduling = {}
    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            pass
        if loop is not None:
            factory = loop.get_task_factory()
            def task_factory(loop, coro, **kwargs):
                if _ACTIVE.get() is not None:
                    coro.close()
                    deny('background_task')
                return factory(loop, coro, **kwargs) if factory else asyncio.Task(coro, loop=loop, **kwargs)
            loop.set_task_factory(task_factory)
            for name in ('call_soon','call_later','call_at','call_soon_threadsafe'):
                scheduling[name] = getattr(loop, name)
                setattr(loop, name, _background_boundary(scheduling[name]))
        yield
    finally:
        for connection in scope.connections:
            try:
                connection.rollback()
                connection.close()
            except sqlite3.Error:
                pass
        if loop is not None:
            loop.set_task_factory(factory)
            for name, method in scheduling.items():
                setattr(loop, name, method)
        _ACTIVE.reset(token)
        with _BYTECODE_LOCK:
            _BYTECODE_USERS -= 1
            if not _BYTECODE_USERS:
                sys.dont_write_bytecode = _BYTECODE_ORIGINAL
        if scope.category is not None:
            raise ReadOnlyViolation(scope.capability, scope.category)


def read_tool(capability):
    def decorate(function):
        @wraps(function)
        def call(*args, **kwargs):
            with read_only_execution(capability):
                return function(*args, **kwargs)
        return call
    return decorate


def planned_boundary(function):
    @wraps(function)
    def call(self, decision, *args, **kwargs):
        try:
            definition = self.registry.require(decision.capability_id) if decision.capability_id else None
        except (KeyError, ValueError):
            # Preserve the controller's access-first rejection contract.
            definition = None
        if definition is not None and int(definition.risk_level) == 0:
            with read_only_execution(definition.id):
                return function(self, decision, *args, **kwargs)
        return function(self, decision, *args, **kwargs)
    return call


def catalog_boundary(function):
    @wraps(function)
    def call(self, intent, *args, **kwargs):
        if intent.intent.value == 'find_tech_card':
            identifier = 'catalog.get' if intent.selected_entity_id else 'catalog.search'
            with read_only_execution(identifier):
                return function(self, intent, *args, **kwargs)
        return function(self, intent, *args, **kwargs)
    return call


def local_boundary(function):
    @wraps(function)
    def call(*args, **kwargs):
        with ExitStack() as stack:
            token = _LOCAL.set(stack)
            try:
                return function(*args, **kwargs)
            finally:
                _LOCAL.reset(token)
    return call


def enter_local_read(capability):
    stack = _LOCAL.get()
    if stack is not None:
        stack.enter_context(read_only_execution(capability))


def safe_tool_errors(function):
    @wraps(function)
    async def call(*args, **kwargs):
        try:
            return await function(*args, **kwargs)
        except ReadOnlyViolation as error:
            import logging
            logging.getLogger(__name__).warning('Read-only tool denied capability=%s category=%s',
                error.capability, error.category)
            event = args[0] if args else kwargs.get('message', kwargs.get('callback'))
            await event.answer('Не удалось безопасно прочитать данные. Используйте меню или попробуйте позже.')
    return call
