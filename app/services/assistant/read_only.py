"""Worker defense in depth; process/OS authority lives in read_broker.

Repositories use connection-per-operation. Every native handle opened inside a
read tool (including by a helper) is guard-owned, query-only, and closed before
scope exit. Cleanup failure is reported, never treated as successful release.
This in-process scope alone cannot protect arbitrary pre-opened writable handles.
Proxy/pool objects and escaping cursors are unsupported within this guard;
helpers consume/close cursors within their operation. No pooled state is changed.
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


@dataclass(frozen=True)
class CleanupDiagnostic:
    capability: str
    stage: str
    category: str
    connection_ordinal: int | None = None
    forced_close_attempted: bool = False
    forced_close_succeeded: bool = False


class ReadOnlyCleanupFailure(ReadOnlyViolation):
    """Normalized resource failure; never retains raw cleanup error messages."""

    def __init__(self, capability, diagnostics):
        super().__init__(capability, 'cleanup_failure')
        self.diagnostics = tuple(diagnostics)
        self.quarantined = any(row.category == 'connection_leaked' for row in self.diagnostics)
        self.args = ('read_only_cleanup_failure',)


@dataclass
class _OwnedConnection:
    connection: sqlite3.Connection = field(repr=False)
    ordinal: int
    initialized: bool = False
    finished: bool = False


@dataclass
class _Scope:
    capability: str
    category: str | None = None
    connections: dict = field(default_factory=dict)
    cleanup_done: bool = False
    cleanup_errors: tuple = ()

    def track(self, connection, *, initialized=False):
        if self.cleanup_done:
            raise ReadOnlyViolation(self.capability, 'scope_closed')
        key = id(connection)
        if key not in self.connections:
            self.connections[key] = _OwnedConnection(connection, len(self.connections) + 1)
        record = self.connections[key]
        record.initialized |= initialized
        return record


# Capture C descriptors, never trust an overridden instance cleanup/probe.
_BASE_CLOSE = sqlite3.Connection.close
_BASE_TRANSACTION = sqlite3.Connection.in_transaction


def _force_close(connection):
    _BASE_CLOSE(connection)


def _closed(connection):
    try:
        _BASE_TRANSACTION.__get__(connection, sqlite3.Connection)
    except sqlite3.ProgrammingError:
        return True
    return False


def _error_category(error):
    if isinstance(error, asyncio.CancelledError):
        return 'cancelled'
    if isinstance(error, sqlite3.Error):
        return 'sqlite_error'
    return 'internal' if isinstance(error, Exception) else 'base_exception'


def _cleanup_owned(scope):
    """Scope owns NEW handles, including helper-created ones, until closed.

    Cursors are consumed/closed by repository operations, not separately registered.
    External handles/proxies/pools are not adopted. Repeated cleanup is a no-op
    with the same normalized result. Keep no handle references in diagnostics.
    """
    if scope.cleanup_done:
        return scope.cleanup_errors
    events, controls, seen = [], [], set()
    def record(stage, error, ordinal, attempted=False, succeeded=False):
        events.append(CleanupDiagnostic(scope.capability, stage, _error_category(error),
            ordinal, attempted, succeeded))
        if not isinstance(error, Exception):
            controls.append(error)
    try:
        for resource in tuple(scope.connections.values()):
            connection = resource.connection
            if resource.finished or id(connection) in seen:
                continue
            seen.add(id(connection))
            try:
                if not isinstance(connection, sqlite3.Connection):
                    events.append(CleanupDiagnostic(scope.capability, 'ownership',
                        'unsupported_connection', resource.ordinal))
                    continue
                if _closed(connection):
                    if not resource.initialized:
                        # A failed C constructor may have emitted the handle
                        # audit event. Base close distinguishes an unusable,
                        # already-closed handle from an uncloseable partial one.
                        try:
                            _force_close(connection)
                        except BaseException as error:
                            record('forced_close', error, resource.ordinal, True, False)
                            events.append(CleanupDiagnostic(scope.capability, 'verify',
                                'connection_leaked', resource.ordinal, True, False))
                    continue
                # Rollback failure (including cancellation) NEVER skips close.
                try:
                    if _BASE_TRANSACTION.__get__(connection, sqlite3.Connection):
                        connection.rollback()
                except BaseException as error:
                    record('rollback', error, resource.ordinal)
                close_error = None
                try:
                    connection.close()
                except BaseException as error:
                    close_error = error
                try:
                    closed = _closed(connection)
                except BaseException as error:
                    record('verify', error, resource.ordinal)
                    closed = False
                forced = not closed
                if forced:
                    try:
                        _force_close(connection)
                    except BaseException as error:
                        record('forced_close', error, resource.ordinal, True, False)
                    try:
                        closed = _closed(connection)
                    except BaseException as error:
                        record('verify', error, resource.ordinal, True, False)
                        closed = False
                if close_error is not None:
                    record('close', close_error, resource.ordinal, forced, forced and closed)
                elif forced:
                    events.append(CleanupDiagnostic(scope.capability, 'close',
                        'instance_close_incomplete', resource.ordinal, True, closed))
                if not closed:
                    events.append(CleanupDiagnostic(scope.capability, 'verify',
                        'connection_leaked', resource.ordinal, forced, False))
            except BaseException as error:
                # Still clean every following connection and application state.
                record('resource_cleanup', error, resource.ordinal)
            finally:
                resource.finished = True
    finally:
        scope.connections.clear()
        scope.cleanup_done = True
        scope.cleanup_errors = tuple(controls) + ((ReadOnlyCleanupFailure(scope.capability, events),) if events else ())
    return scope.cleanup_errors


def read_only_failure(error):
    """Find a safe tool failure, but NEVER consume cancellation/fatal leaves."""
    leaves = []
    def visit(item):
        if isinstance(item, BaseExceptionGroup):
            for child in item.exceptions:
                visit(child)
        else:
            leaves.append(item)
    visit(error)
    if any(not isinstance(item, (Exception, ReadOnlyViolation)) for item in leaves):
        return None
    return next((item for item in leaves if isinstance(item, ReadOnlyCleanupFailure)),
        next((item for item in leaves if isinstance(item, ReadOnlyViolation)), None))


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
        if scope.cleanup_done:
            deny('scope_closed')
        if not _OPENING.get():
            deny('sqlite_open_boundary')
        raw = os.fsdecode(args[0])
        # Never create a missing DB as a side effect of a read executor.
        if raw == ':memory:' or raw.startswith('file:') or not os.path.isfile(raw):
            deny('sqlite_open')
    elif event == 'sqlite3.connect/handle':
        # The handle is not initialized yet; defer all SQLite calls until the
        # factory returns. A failed post-construction registration is still owned.
        scope.track(args[0])
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
    scope = _ACTIVE.get()
    if scope is not None:
        try:
            _register_connection(scope, connection)
            connection.execute('PRAGMA query_only=ON')
            connection.set_authorizer(authorize_sql)
        except BaseException as error:
            failure = ReadOnlyCleanupFailure(scope.capability, (
                CleanupDiagnostic(scope.capability, 'register', _error_category(error)),))
            raise BaseExceptionGroup('read_only_connection_setup_failed', [error, failure]) from None
    return connection


def _register_connection(scope, connection):
    if not isinstance(connection, sqlite3.Connection):
        raise ReadOnlyViolation(scope.capability, 'unsupported_connection')
    scope.track(connection, initialized=True)


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
    bytecode_acquired = False
    loop = None
    factory = None
    factory_installed = False
    scheduling = {}
    body_error = None
    errors = []
    try:
        with _BYTECODE_LOCK:
            if not _BYTECODE_USERS:
                _BYTECODE_ORIGINAL = sys.dont_write_bytecode
            _BYTECODE_USERS += 1
            bytecode_acquired = True
            sys.dont_write_bytecode = True
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
            factory_installed = True
            for name in ('call_soon','call_later','call_at','call_soon_threadsafe'):
                scheduling[name] = getattr(loop, name)
                setattr(loop, name, _background_boundary(scheduling[name]))
        yield
    except BaseException as error:
        body_error = error
    finally:
        # Resource teardown is INSIDE an independent application-state finally.
        # No rollback/close/cancellation may skip restoration or later handles.
        try:
            errors.extend(_cleanup_owned(scope))
        except BaseException as error:
            errors.extend((error, ReadOnlyCleanupFailure(capability, (
                CleanupDiagnostic(capability, 'resource_cleanup', _error_category(error)),))))
        finally:
            restoration = []
            def restore(stage, action):
                try:
                    action()
                except BaseException as error:
                    restoration.append(CleanupDiagnostic(capability, stage, _error_category(error)))
                    if not isinstance(error, Exception):
                        errors.append(error)
            def reset_context():
                try:
                    _ACTIVE.reset(token)
                except BaseException:
                    _ACTIVE.set(None)  # outer scope only; nested scopes never own this token
                    raise
            def release_bytecode():
                global _BYTECODE_USERS
                with _BYTECODE_LOCK:
                    _BYTECODE_USERS -= 1
                    if not _BYTECODE_USERS:
                        sys.dont_write_bytecode = _BYTECODE_ORIGINAL
            try:
                restore('registry', scope.connections.clear)
            finally:
                try:
                    restore('context', reset_context)
                finally:
                    try:
                        if loop is not None:
                            if factory_installed:
                                restore('task_factory', lambda:loop.set_task_factory(factory))
                            for name, method in scheduling.items():
                                restore('loop_hook', lambda name=name,method=method:setattr(loop,name,method))
                    finally:
                        if bytecode_acquired:
                            restore('counter', release_bytecode)
                            bytecode_acquired = False
            if restoration:
                errors.append(ReadOnlyCleanupFailure(capability, restoration))
    if body_error is not None:
        errors.insert(0, body_error)
    if scope.category is not None and not any(isinstance(error,ReadOnlyViolation) for error in errors):
        errors.append(ReadOnlyViolation(capability, scope.category))
    if len(errors) == 1:
        raise errors[0]
    if errors:
        # Preserve asyncio Task/timeout cancellation semantics, not merely a
        # CancelledError leaf inside a non-cancelled Task's exception group.
        primary = next((error for error in errors
            if not isinstance(error, (Exception, ReadOnlyViolation))), None)
        if primary is not None:
            remaining = [error for error in errors if error is not primary]
            if primary.__cause__ is not None and primary.__cause__ not in remaining:
                remaining.append(primary.__cause__)
            for error in remaining:
                if isinstance(error, ReadOnlyCleanupFailure):
                    _log_cleanup(error)
            cause = remaining[0] if len(remaining)==1 else BaseExceptionGroup('read_only_cleanup_failures', remaining)
            raise primary from cause
        raise BaseExceptionGroup('read_only_execution_failed', errors) from None


def read_tool(capability):
    def decorate(function):
        @wraps(function)
        def call(self, actor_user_id, payload=None):
            from app.services.assistant.read_broker import IN_WORKER, ReadOnlyExecutionBroker
            if not IN_WORKER.get():
                from pydantic import TypeAdapter
                from typing import get_type_hints
                from app.services.access_service import AccessDeniedError
                definition=self.capabilities.require(capability)
                if self.access_policy is not None:
                    self.access_policy.require_assistant_access(actor_user_id,chat_type='private')
                available=(self.access_policy.available_capabilities(actor_user_id) if self.access_policy else
                    self.capabilities.available_for_actor(self.access,actor_user_id,flags=self.flags))
                if definition not in available or not all(self.flags.get(flag,True) for flag in definition.feature_flags):
                    raise AccessDeniedError('Capability is unavailable')
                arguments = payload.model_dump(mode='json', exclude_none=True) if payload is not None else {}
                if capability == 'catalog.get':
                    arguments = {'entity_ref': arguments['card_id']}
                if capability == 'capabilities.list':
                    arguments.setdefault('query', 'возможности')
                broker = ReadOnlyExecutionBroker()
                request = broker.request(capability, access=self.access, actor=actor_user_id,
                    policy=self.access_policy, flags=self.flags, registry=self.capabilities,
                    catalog=self.retriever.catalog, arguments=arguments, mode='tool')
                result = broker.execute(request)
                try:
                    decoded=TypeAdapter(get_type_hints(function)['return']).validate_python(result)
                    if capability in {'catalog.search','catalog.get'}:
                        from app.services.assistant.read_broker import catalog_keys
                        returned=decoded if isinstance(decoded,tuple) else (decoded,) if decoded is not None else ()
                        if any(row.card_id not in catalog_keys(self.retriever.catalog) for row in returned):
                            raise ReadOnlyViolation(capability,'result_target')
                    return decoded
                except (ValueError,TypeError):
                    from app.services.assistant.read_broker import ReadWorkerFailure
                    raise ReadWorkerFailure('result_contract') from None
            with read_only_execution(capability):
                return function(self, actor_user_id, payload)
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
        from app.services.assistant.read_broker import IN_WORKER
        if IN_WORKER.get() and definition is not None and int(definition.risk_level) == 0:
            with read_only_execution(definition.id):
                return function(self, decision, *args, **kwargs)
        return function(self, decision, *args, **kwargs)
    return call


def catalog_boundary(function):
    @wraps(function)
    def call(self, intent, *args, **kwargs):
        if intent.intent.value == 'find_tech_card':
            from app.services.assistant.read_broker import IN_WORKER, ReadOnlyExecutionBroker
            if not IN_WORKER.get():
                return ReadOnlyExecutionBroker().catalog(self.retriever.catalog, intent, context=self.read_context)
            identifier = 'catalog.get' if intent.selected_entity_id else 'catalog.search'
            with read_only_execution(identifier):
                return function(self, intent, *args, **kwargs)
        return function(self, intent, *args, **kwargs)
    return call


def local_boundary(function):
    @wraps(function)
    def call(self, text, *args, **kwargs):
        with ExitStack() as stack:
            token = _LOCAL.set(stack)
            try:
                try:
                    return function(self, text, *args, **kwargs)
                except _LocalReadRoute as route:
                    from app.services.assistant.read_broker import ReadOnlyExecutionBroker
                    from app.models.assistant_capabilities import QueryCapabilityInput, EntityCapabilityInput
                    definition = self.registry.require(route.capability)
                    arguments = {}
                    if definition.input_model is QueryCapabilityInput:
                        arguments['query'] = (next((word for word in ('смен','отч','сотруд','напомин','каталог')
                            if word in text.casefold()), 'возможности')
                            if route.capability == 'capabilities.list' else text[:500])
                    if definition.input_model is EntityCapabilityInput:
                        # Existing untyped routes are navigation/clarification,
                        # not authority to pick a business entity from raw text.
                        from app.services.assistant.capability_controller import CapabilityDispatch
                        return CapabilityDispatch(route.capability, 'Уточните нужную запись.')
                    return ReadOnlyExecutionBroker().dispatch(self, route.capability,
                        kwargs['actor_user_id'], arguments=arguments)
            finally:
                _LOCAL.reset(token)
    return call


def enter_local_read(capability):
    stack = _LOCAL.get()
    if stack is not None:
        from app.services.assistant.read_broker import IN_WORKER
        if IN_WORKER.get():
            stack.enter_context(read_only_execution(capability))
        else:
            raise _LocalReadRoute(capability)


class _LocalReadRoute(BaseException):
    def __init__(self, capability):
        self.capability = capability


def safe_tool_errors(function):
    @wraps(function)
    async def call(*args, **kwargs):
        from app.services.assistant.read_broker import ReadWorkerFailure
        try:
            return await function(*args, **kwargs)
        except ReadWorkerFailure:
            import logging
            logging.getLogger(__name__).warning('Isolated read worker unavailable')
            event = args[0] if args else kwargs.get('message', kwargs.get('callback'))
            await event.answer('Не удалось безопасно прочитать данные. Используйте меню или попробуйте позже.')
        except (ReadOnlyViolation, BaseExceptionGroup) as error:
            failure = read_only_failure(error)
            if failure is None:
                raise
            import logging
            logger = logging.getLogger(__name__)
            if isinstance(failure, ReadOnlyCleanupFailure):
                _log_cleanup(failure)
            else:
                logger.warning('Read-only tool denied capability=%s category=%s',
                    failure.capability, failure.category)
            event = args[0] if args else kwargs.get('message', kwargs.get('callback'))
            await event.answer('Не удалось безопасно прочитать данные. Используйте меню или попробуйте позже.')
    return call


def _log_cleanup(failure):
    import logging
    for row in failure.diagnostics:
        logging.getLogger(__name__).warning('Read-only cleanup capability=%s stage=%s category=%s '
            'connection_ordinal=%s forced_close_attempted=%s forced_close_succeeded=%s',
            row.capability,row.stage,row.category,row.connection_ordinal,
            row.forced_close_attempted,row.forced_close_succeeded)
