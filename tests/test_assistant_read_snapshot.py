"""Consistent, bounded, source-RO Backup API and owned-clone lifecycle."""
from contextlib import closing
import contextvars
import os
from pathlib import Path
import sqlite3
import threading
import time
from unittest.mock import patch

import pytest

from app.services.assistant import read_broker as broker,read_snapshot as snapshots
from app.services.assistant.read_only import ReadOnlyViolation
from app.services.assistant.read_broker import ReadOnlyExecutionBroker,ReadWorkerFailure
from tests.test_assistant_read_memory import env,source_bytes,managed_roots
from tests.test_assistant_read_process import request,following_write,coordinator_patch


@pytest.mark.parametrize('mode,pinned',[('DELETE',False),('WAL',False),('WAL',True)])
def test_backup_is_consistent_private_delete_clone_without_source_changes(env,mode,pinned):
    with closing(sqlite3.connect(env.path)) as c:c.execute('PRAGMA journal_mode='+mode)
    pin=contextvars.Context().run(sqlite3.connect,env.path) if pinned else None
    if pin:
        pin.execute("UPDATE users SET first_name='Synthetic WAL commit'")
        pin.commit()
    before=source_bytes(env)
    try:
        snap=snapshots.prepare(env.path,snapshots.file_identity(env.path),deadline=time.monotonic()+8,cancel=None)
        root=snap.root
        try:
            assert source_bytes(env)==before
            assert root.stat().st_mode & 0o777 == 0o700
            assert snap.path.stat().st_mode & 0o777 == 0o600
            with closing(contextvars.Context().run(sqlite3.connect,snap.path.as_uri()+'?mode=ro',uri=True)) as clone:
                assert clone.execute('PRAGMA user_version').fetchone()[0]==15
                assert clone.execute('PRAGMA journal_mode').fetchone()[0]=='delete'
                old=clone.execute('SELECT first_name FROM users ORDER BY telegram_user_id').fetchall()
                with closing(sqlite3.connect(env.path)) as writer:
                    writer.execute("UPDATE users SET first_name='Newer committed value'");writer.commit()
                assert clone.execute('SELECT first_name FROM users ORDER BY telegram_user_id').fetchall()==old
                assert clone.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
                assert not clone.execute('PRAGMA foreign_key_check').fetchall()
            # Snapshot itself never modifies source bytes; the writer above is
            # intentionally later and must not change the already frozen clone.
            assert set(os.listdir(root))=={'read.sqlite','manifest.json'}
        finally:snap.close()
        assert not root.exists()
    finally:
        if pin:pin.close()
    following_write(env)


def test_backup_reads_active_uncheckpointed_frames_without_writing_them(env):
    pin=contextvars.Context().run(sqlite3.connect,env.path)
    pin.execute('PRAGMA wal_autocheckpoint=0')
    pin.execute("UPDATE users SET first_name='Uncheckpointed synthetic'");pin.commit()
    before=source_bytes(env)
    try:
        s=snapshots.prepare(env.path,snapshots.file_identity(env.path),deadline=time.monotonic()+8,cancel=None)
        try:
            with closing(contextvars.Context().run(sqlite3.connect,s.path)) as c:
                assert {r[0] for r in c.execute('SELECT first_name FROM users')}=={'Uncheckpointed synthetic'}
            assert source_bytes(env)==before
        finally:s.close()
    finally:pin.close()
    following_write(env)


def test_busy_source_fails_closed_and_cleans_clone(env):
    b=ReadOnlyExecutionBroker(timeout=3);r=request(env,b)
    with closing(sqlite3.connect(env.path)) as c:c.execute('PRAGMA journal_mode=DELETE')
    before=source_bytes(env)
    pin=contextvars.Context().run(sqlite3.connect,env.path)
    pin.execute('BEGIN EXCLUSIVE')
    roots=managed_roots()
    try:
        with pytest.raises((ReadWorkerFailure,ReadOnlyViolation)):b.execute(r)
        assert source_bytes(env)==before
        assert roots==managed_roots()
    finally:pin.rollback();pin.close()
    following_write(env)


def test_lease_break_discards_clone_and_releases_waiting_writer(env,monkeypatch):
    # No WAL/SHM: the helper's second main open is after it acquired its read
    # lease. A new writer must invalidate immutable eligibility, not race it.
    assert not Path(str(env.path)+'-wal').exists()
    coordinator_patch(monkeypatch,'''
import app.services.assistant.read_snapshot as s,os,threading,time
original=s.SnapshotAuthority._open
opened=0
def writer(path):
 fd=os.open(path,os.O_RDWR);os.close(fd)
def intercept(self,path,flags):
 global opened
 if s.Path(path)==self.destination.parent/'stage.sqlite':
  opened+=1
  if opened==1:
   threading.Thread(target=writer,args=(self.path,)).start();time.sleep(.1)
 return original(self,path,flags)
s.SnapshotAuthority._open=intercept
''')
    b=ReadOnlyExecutionBroker(timeout=5)
    with pytest.raises((ReadWorkerFailure,ReadOnlyViolation)):b.execute(request(env,b))
    following_write(env)


@pytest.mark.parametrize('code',["import os;os._exit(7)","import time;time.sleep(30)","import app.services.assistant.read_snapshot as s;s.MAX_SNAPSHOT=1"])
def test_helper_failure_timeout_or_size_bound_cleans_owned_root(env,monkeypatch,code):
    original=broker.subprocess.Popen
    def spawn(command,*a,**kw):
        command=list(command);marker='from app.services.assistant.read_snapshot import coordinate;'
        if marker in command[-1]:command[-1]=command[-1].replace(marker,marker+'exec('+repr(code)+');')
        return original(command,*a,**kw)
    monkeypatch.setattr(broker.subprocess,'Popen',spawn)
    roots=managed_roots();fds=len(os.listdir('/proc/self/fd'))
    before=source_bytes(env);b=ReadOnlyExecutionBroker(timeout=2)
    with pytest.raises((ReadWorkerFailure,ReadOnlyViolation)):b.execute(request(env,b))
    assert source_bytes(env)==before and roots==managed_roots()
    assert len(os.listdir('/proc/self/fd'))==fds
    following_write(env)


def test_cleanup_refuses_replaced_root_without_deleting_it():
    s=snapshots.OwnedSnapshot();saved=s.root.with_name(s.root.name+'-saved')
    s.root.rename(saved);s.root.mkdir(mode=0o700)
    try:
        with pytest.raises(ValueError):s.close()
        assert s.root.exists() and (saved/'read.sqlite').exists()
    finally:s.root.rmdir();saved.rename(s.root);s.close()
