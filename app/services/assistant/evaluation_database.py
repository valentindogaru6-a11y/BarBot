"""Owned temporary SQLite identity and scoped evaluation-only connection guard."""

from __future__ import annotations

import os
import sqlite3
import stat
import threading
import weakref
from contextvars import ContextVar
from pathlib import Path
from tempfile import TemporaryDirectory
from app.services.assistant.evaluation_business_state import MUTATION_LEDGER, Mutation
from app.services.assistant.read_only import authorize_sql

from app.services.assistant.evaluation_budget import EvaluationSafetyError


_OWNER = ContextVar('evaluation_sqlite_owner', default=None)
_LOCK = threading.RLock()
_USERS = 0
_SAVED_CONNECT = sqlite3.connect


class _OwnedCursor(sqlite3.Cursor):
    def execute(self, *args, **kwargs):
        return self.connection.observe_statement(lambda:super(_OwnedCursor,self).execute(*args,**kwargs))

    def executemany(self, *args, **kwargs):
        return self.connection.observe_statement(lambda:super(_OwnedCursor,self).executemany(*args,**kwargs))

    def executescript(self, *args, **kwargs):
        return self.connection.observe_statement(lambda:super(_OwnedCursor,self).executescript(*args,**kwargs))


class _OwnedConnection(sqlite3.Connection):
    """Observe only owned main DB statements, without SQL/parameter capture."""
    def install_observer(self):
        self._targets=None
        def authorize(action,table,column,database,source):
            verbs={sqlite3.SQLITE_INSERT:'insert',sqlite3.SQLITE_UPDATE:'update',sqlite3.SQLITE_DELETE:'delete'}
            if authorize_sql(action,table,column,database,source) == sqlite3.SQLITE_DENY:
                ledger = MUTATION_LEDGER.get()
                if ledger is not None:
                    ledger.append(Mutation(table if action in verbs else 'schema', verbs.get(action,'ddl'), 0))
                return sqlite3.SQLITE_DENY
            if self._targets is not None and database=='main' and action in verbs:
                self._targets.add((table,verbs[action]))
            ddl=(sqlite3.SQLITE_CREATE_TABLE,sqlite3.SQLITE_DROP_TABLE,sqlite3.SQLITE_ALTER_TABLE,
                 sqlite3.SQLITE_CREATE_INDEX,sqlite3.SQLITE_DROP_INDEX,sqlite3.SQLITE_CREATE_TRIGGER,
                 sqlite3.SQLITE_DROP_TRIGGER,sqlite3.SQLITE_CREATE_VIEW,sqlite3.SQLITE_DROP_VIEW)
            if self._targets is not None and (action in ddl or
                (action==sqlite3.SQLITE_PRAGMA and table in ('user_version','journal_mode') and column is not None)):
                self._targets.add(('schema','ddl'))
            return sqlite3.SQLITE_OK
        self.set_authorizer(authorize)

    def observe_statement(self, execute):
        ledger=MUTATION_LEDGER.get()
        if ledger is None or not hasattr(self,'_targets'):
            return execute()
        previous=self._targets
        self._targets=set()
        before=self.total_changes
        cursor=None
        try:
            cursor=execute()
            return cursor
        finally:
            changes=self.total_changes-before
            if changes:
                count=cursor.rowcount if cursor is not None and cursor.rowcount>0 else changes
                ledger.extend(Mutation(table,verb,count) for table,verb in sorted(self._targets))
            elif ('schema','ddl') in self._targets:
                ledger.append(Mutation('schema','ddl',1))
            self._targets=previous

    def cursor(self, factory=_OwnedCursor):
        return super().cursor(factory)

    def execute(self,*args,**kwargs):
        return self.cursor().execute(*args,**kwargs)

    def executemany(self,*args,**kwargs):
        return self.cursor().executemany(*args,**kwargs)

    def executescript(self,*args,**kwargs):
        return self.cursor().executescript(*args,**kwargs)


def _guarded_connect(database, *args, **kwargs):
    owner = _OWNER.get()
    if owner is None:
        return _SAVED_CONNECT(database, *args, **kwargs)
    # The official schema validator creates a canonical in-memory reference.
    # It has no filesystem path and cannot redirect the owned live database.
    if database != ':memory:':
        owner.validate_path(database)
    owner.validate_identity()
    kwargs['factory'] = _OwnedConnection
    kwargs['cached_statements'] = 0  # authorizer metadata for every execution
    connection = owner.sqlite_open(database, *args, **kwargs)
    try:
        owner.validate_identity()
    except BaseException:
        connection.close()
        raise
    owner.connections.add(connection)
    if database != ':memory:':
        connection.install_observer()
    return connection


class OwnedEvaluationDatabase:
    """No caller path. Same-user hostile FS races are not remotely controllable."""

    def __init__(self):
        self.temporary = TemporaryDirectory(prefix='barbot-live-eval-')
        self.root = Path(self.temporary.name)
        self.connections = weakref.WeakSet()
        self.fd = None
        self._token = None
        try:
            root_stat = self.root.lstat()
            self.root_identity = root_stat.st_dev, root_stat.st_ino
            if (not stat.S_ISDIR(root_stat.st_mode) or stat.S_IMODE(root_stat.st_mode) != 0o700
                or root_stat.st_uid != os.geteuid() or any(self.root.iterdir())):
                raise EvaluationSafetyError('temporary_root_not_secure')
            self.path = self.root/'evaluation.db'
            self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            created = os.fstat(self.fd)
            self.identity = created.st_dev, created.st_ino
            self.validate_identity()
        except BaseException:
            self.close()
            raise

    def validate_path(self, value, *, must_be_new=False):
        if not isinstance(value, (str, os.PathLike)) or str(value).startswith('file:'):
            raise EvaluationSafetyError('temporary_db_path_denied')
        path = Path(value)
        if (must_be_new or not path.is_absolute() or '..' in path.parts or
            path != self.path or path.parent.resolve(strict=True) != self.root):
            raise EvaluationSafetyError('temporary_db_path_denied')
        self.validate_identity()
        return self.path

    def validate_identity(self):
        try:
            root = self.root.lstat()
            current = self.path.lstat()
            pinned = os.fstat(self.fd)
            if (not stat.S_ISDIR(root.st_mode) or stat.S_IMODE(root.st_mode) != 0o700 or
                (root.st_dev, root.st_ino) != self.root_identity or root.st_uid != os.geteuid() or
                not stat.S_ISREG(current.st_mode) or current.st_nlink != 1 or
                stat.S_IMODE(current.st_mode) != 0o600 or current.st_uid != os.geteuid() or
                (current.st_dev, current.st_ino) != self.identity or
                (pinned.st_dev, pinned.st_ino) != self.identity or pinned.st_nlink != 1):
                raise EvaluationSafetyError('temporary_db_identity_changed')
            for suffix in ('-wal', '-shm', '-journal'):
                auxiliary = Path(str(self.path)+suffix)
                if auxiliary.exists() or auxiliary.is_symlink():
                    info = auxiliary.lstat()
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
                        raise EvaluationSafetyError('temporary_auxiliary_path_denied')
        except (OSError, ValueError, TypeError):
            raise EvaluationSafetyError('temporary_db_identity_changed') from None

    def install(self):
        global _USERS, _SAVED_CONNECT
        with _LOCK:
            if _USERS == 0:
                _SAVED_CONNECT = sqlite3.connect
                sqlite3.connect = _guarded_connect
            _USERS += 1
            self.sqlite_open = _SAVED_CONNECT
        self._token = _OWNER.set(self)

    def close(self):
        global _USERS
        for connection in list(self.connections):
            connection.close()
        if self._token is not None:
            _OWNER.reset(self._token)
            self._token = None
            with _LOCK:
                _USERS -= 1
                if _USERS == 0 and sqlite3.connect is _guarded_connect:
                    sqlite3.connect = _SAVED_CONNECT
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if hasattr(self, 'root_identity'):
            info = self.root.lstat()
            if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != self.root_identity:
                self.temporary._finalizer.detach()
                raise EvaluationSafetyError('temporary_root_identity_changed')
        self.temporary.cleanup()
