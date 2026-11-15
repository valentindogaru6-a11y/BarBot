"""No caller path in production API; tests isolate the fixed base constant."""
import json
import os
import time

import pytest

from app.services.assistant import read_lifecycle as life


@pytest.fixture
def manager(tmp_path,monkeypatch):
    monkeypatch.setattr(life,'BASE',tmp_path/'fixed-managed-v1')
    return life


def test_versioned_manifest_private_permissions_and_no_sensitive_fields(manager):
    root=manager.ManagedRoot()
    try:
        assert root.root.stat().st_mode & 0o777 == 0o700
        for p in root.root.iterdir():assert p.stat().st_mode & 0o777 == 0o600
        m=json.loads((root.root/'manifest.json').read_text().splitlines()[0])
        assert m['version']==1 and m['state']=='creating'
        assert set(m['owner'])=={'pid','start','boot','namespace'}
        assert not any(word in json.dumps(m) for word in ('source','prompt','telegram','token','/opt/'))
        root.append({'state':'ready'});root.append({'state':'in_use'})
        assert manager.scavenge()==1
    finally:root.close()


def test_count_and_reserved_disk_budget_are_serialized(manager):
    first=manager.ManagedRoot();second=manager.ManagedRoot()
    try:
        with pytest.raises(manager.LifecycleError):manager.ManagedRoot()
        assert manager.MAX_ROOTS==2 and manager.TOTAL_BUDGET==2*manager.ROOT_RESERVATION
    finally:second.close();first.close()
    replacement=manager.ManagedRoot();replacement.close()


def test_live_expired_owner_retained_but_reuse_denied(manager,monkeypatch):
    root=manager.ManagedRoot();now=time.time()
    monkeypatch.setattr(manager.time,'time',lambda:now+manager.TTL_SECONDS+1)
    try:
        assert manager.scavenge()==1 and root.root.exists()
        with pytest.raises(manager.LifecycleError):root.append({'state':'ready'})
    finally:root.close()


def test_dead_owner_or_reused_pid_recovered(manager,monkeypatch):
    root=manager.ManagedRoot();path=root.root;original=manager.process_identity
    monkeypatch.setattr(manager,'process_identity',lambda pid:None if pid==os.getpid() else original(pid))
    assert manager.scavenge()==0 and not path.exists()


@pytest.mark.parametrize('tamper',['symlink','hardlink','unknown','root_replace','manifest_replace','clone_replace'])
def test_scavenge_never_deletes_unknown_or_replaced_entries(manager,monkeypatch,tmp_path,tamper):
    root=manager.ManagedRoot();foreign=tmp_path/'foreign';foreign.write_bytes(b'unchanged')
    saved=None
    if tamper in ('symlink','hardlink','clone_replace'):
        saved=tmp_path/'held-clone';root.path.rename(saved)
        if tamper=='symlink':root.path.symlink_to(foreign)
        elif tamper=='hardlink':os.link(foreign,root.path)
        else:root.path.write_bytes(b'replaced');root.path.chmod(0o600)
    elif tamper=='unknown':(root.root/'unknown').write_bytes(b'untouched')
    elif tamper=='root_replace':
        saved=tmp_path/'held-root';root.root.rename(saved);root.root.mkdir(mode=0o700)
    else:
        saved=tmp_path/'held-manifest';(root.root/'manifest.json').rename(saved)
        (root.root/'manifest.json').write_text(saved.read_text());(root.root/'manifest.json').chmod(0o600)
    original=manager.process_identity
    monkeypatch.setattr(manager,'process_identity',lambda pid:None if pid==os.getpid() else original(pid))
    try:
        with pytest.raises(manager.LifecycleError):manager.scavenge()
        assert root.root.exists() and foreign.read_bytes()==b'unchanged'
    finally:
        if tamper in ('symlink','hardlink','clone_replace'):root.path.unlink();saved.rename(root.path)
        elif tamper=='unknown':(root.root/'unknown').unlink()
        elif tamper=='root_replace':root.root.rmdir();saved.rename(root.root)
        else:(root.root/'manifest.json').unlink();saved.rename(root.root/'manifest.json')
        root.close()


def test_only_dedicated_base_and_known_names_are_scanned(manager,tmp_path):
    outside=tmp_path/'outside';outside.mkdir();(outside/'valuable').write_text('keep')
    with manager.locked():pass
    unknown=manager.BASE/'not-owned';unknown.mkdir()
    try:
        with pytest.raises(manager.LifecycleError):manager.scavenge()
        assert unknown.exists() and (outside/'valuable').read_text()=='keep'
    finally:unknown.rmdir()
    with pytest.raises(manager.LifecycleError):manager.ManagedRoot.attach(outside/'read.sqlite')


def test_torn_last_state_does_not_prevent_dead_owner_recovery(manager,monkeypatch):
    root=manager.ManagedRoot()
    with (root.root/'manifest.json').open('ab') as f:f.write(b'{"state":')
    original=manager.process_identity
    monkeypatch.setattr(manager,'process_identity',lambda pid:None if pid==os.getpid() else original(pid))
    assert manager.scavenge()==0 and not root.root.exists()


@pytest.mark.parametrize('stage',['data_removed','manifest_retired','directory_removed'])
def test_interrupted_cleanup_is_recoverable_without_guessing_unknown_root(manager,monkeypatch,stage):
    root=manager.ManagedRoot();root.append({'state':'closing'})
    for name in manager.FILES:(root.root/name).unlink()
    retired=manager.BASE/('retired-'+root.header['id']+'.json')
    if stage in ('manifest_retired','directory_removed'):(root.root/'manifest.json').rename(retired)
    if stage=='directory_removed':root.root.rmdir()
    old=manager.process_identity
    monkeypatch.setattr(manager,'process_identity',lambda pid:None if pid==os.getpid() else old(pid))
    assert manager.scavenge()==0 and not root.root.exists() and not retired.exists()


@pytest.mark.parametrize('header',[[],None,'unknown',False,42])
def test_unknown_retired_manifest_is_quarantined_with_safe_category(manager,header):
    with manager.locked():pass
    path=manager.BASE/('retired-'+'a'*32+'.json')
    path.write_text(json.dumps(header)+'\n');path.chmod(0o600)
    try:
        with pytest.raises(manager.LifecycleError):manager.scavenge()
        assert path.exists()
    finally:path.unlink()
