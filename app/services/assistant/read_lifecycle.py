"""Versioned private roots, serialized allocation and conservative recovery.

Only this fixed per-service-UID base is ever scanned. Unknown/tampered entries
are quarantined (not deleted) and block new allocation. No source path, actor,
prompt, credential or memory value is stored in a manifest.
"""
from contextlib import contextmanager
import ctypes
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import time
from uuid import uuid4

BASE=Path('/tmp')/f'barbot-read-snapshots-v1-{os.geteuid()}'
MAX_ROOTS=2
MAX_SNAPSHOT=16*1024*1024
ROOT_RESERVATION=129*1024*1024
TOTAL_BUDGET=258*1024*1024
TTL_SECONDS=300
BACKUP_SECONDS=5
WORKER_SECONDS=8
FILES=('read.sqlite','read.sqlite-wal','read.sqlite-shm','read.sqlite-journal',
       'stage.sqlite','stage.sqlite-wal','stage.sqlite-shm','stage.sqlite-journal')
STATES={'creating','ready','in_use','closing'}


class LifecycleError(ValueError):
    def __init__(self,category):
        self.category=category
        super().__init__(category)


def process_identity(pid):
    try:
        text=Path(f'/proc/{pid}/stat').read_text()
        fields=text[text.rfind(')')+2:].split()
        if fields[0]=='Z':return None
        boot=hashlib.sha256(Path('/proc/sys/kernel/random/boot_id').read_bytes()).hexdigest()
        return dict(pid=pid,start=fields[19],boot=boot,namespace=os.stat('/proc/self/ns/pid').st_ino)
    except FileNotFoundError:return None
    except (OSError,IndexError,ValueError):raise LifecycleError('snapshot_owner_unknown') from None


def alive(identity):
    if identity is not None and identity['namespace']!=os.stat('/proc/self/ns/pid').st_ino:
        boot=hashlib.sha256(Path('/proc/sys/kernel/random/boot_id').read_bytes()).hexdigest()
        if identity['boot']==boot:raise LifecycleError('snapshot_owner_namespace')
        return False
    return identity is not None and process_identity(identity['pid'])==identity


def valid_process(value):
    return (isinstance(value,dict) and set(value)=={'pid','start','boot','namespace'}
        and type(value['pid']) is int and 0<value['pid']<2**31
        and isinstance(value['start'],str) and bool(re.fullmatch(r'[0-9]{1,32}',value['start']))
        and isinstance(value['boot'],str) and bool(re.fullmatch(r'[a-f0-9]{64}',value['boot']))
        and type(value['namespace']) is int and value['namespace']>0)


def valid_inode(value):
    return isinstance(value,list) and len(value)==2 and all(type(v) is int and v>=0 for v in value) and value[1]>0


def unique_object(pairs):
    out={}
    for key,value in pairs:
        if key in out:raise ValueError('duplicate_manifest_key')
        out[key]=value
    return out


def identity(s):
    return [s.st_dev,s.st_ino]


def regular(fd,expected=None):
    s=os.fstat(fd)
    if (not stat.S_ISREG(s.st_mode) or s.st_nlink!=1 or s.st_uid!=os.geteuid()
            or stat.S_IMODE(s.st_mode)!=0o600 or expected is not None and identity(s)!=expected):
        raise LifecycleError('snapshot_file_identity')
    return s


@contextmanager
def locked(deadline=None):
    if BASE.is_symlink() or BASE.parent.is_symlink():raise LifecycleError('snapshot_base_identity')
    try:BASE.mkdir(mode=0o700)
    except FileExistsError:pass
    directory=os.open(BASE,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
    lock=None
    try:
        s=os.fstat(directory)
        if s.st_uid!=os.geteuid() or stat.S_IMODE(s.st_mode)!=0o700:
            raise LifecycleError('snapshot_base_identity')
        lock=os.open('manager.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_CLOEXEC,0o600,dir_fd=directory)
        regular(lock)
        until=min(deadline or time.monotonic()+2,time.monotonic()+2)
        while True:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
            except BlockingIOError:
                if time.monotonic()>=until:raise LifecycleError('snapshot_manager_busy')
                time.sleep(.005)
        current=BASE.lstat()
        if identity(current)!=identity(s):raise LifecycleError('snapshot_base_identity')
        if identity(os.stat('manager.lock',dir_fd=directory,follow_symlinks=False))!=identity(os.fstat(lock)):
            raise LifecycleError('snapshot_lock_identity')
        yield directory
    except OSError:raise LifecycleError('snapshot_manager_unavailable') from None
    finally:
        if lock is not None:os.close(lock)
        os.close(directory)


def _read(root):
    if root.parent!=BASE or not re.fullmatch(r'snapshot-[a-f0-9]{32}',root.name):
        raise LifecycleError('snapshot_root_name')
    d=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        s=os.fstat(d)
        if s.st_uid!=os.geteuid() or stat.S_IMODE(s.st_mode)!=0o700:
            raise LifecycleError('snapshot_root_identity')
        retired=False
        try:fd=os.open('manifest.json',os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=d)
        except FileNotFoundError:
            retired=True
            fd=os.open(BASE/('retired-'+root.name.removeprefix('snapshot-')+'.json'),os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
        try:
            mstat=regular(fd)
            raw=os.read(fd,4097)
        finally:os.close(fd)
        if len(raw)>4096:raise LifecycleError('snapshot_manifest_invalid')
        # Newline is the commit marker for an append record. A torn final
        # record is ignored; malformed complete records are never accepted.
        rows=raw.split(b'\n')[:-1];records=[]
        for line in rows:records.append(json.loads(line,object_pairs_hook=unique_object))
        if not records:raise LifecycleError('snapshot_manifest_invalid')
        header=records[0]
        if (set(header)!={'version','id','owner','created','root','manifest','files','state'}
                or type(header['version']) is not int or header['version']!=1 or root.name!='snapshot-'+header['id']
                or header['root']!=identity(s) or header['manifest']!=identity(mstat)
                or set(header['files'])!=set(FILES) or header['state']!='creating'
                or not all(valid_inode(v) for v in header['files'].values())
                or type(header['created']) not in (int,float) or not math.isfinite(header['created'])
                or header['created']<=0 or header['created']>time.time()+60
                or not valid_process(header['owner'])):
            raise LifecycleError('snapshot_manifest_invalid')
        state='creating';processes=[]
        for row in records[1:]:
            if set(row)=={'state'} and row['state'] in STATES:state=row['state']
            elif set(row)=={'process'} and valid_process(row['process']):processes.append(row['process'])
            else:raise LifecycleError('snapshot_manifest_invalid')
        names=set(os.listdir(d))
        if state!='closing' and not {'manifest.json','read.sqlite'}<=names:raise LifecycleError('snapshot_missing_entry')
        if retired and state!='closing':raise LifecycleError('snapshot_manifest_invalid')
        if not names <= set(FILES)|{'manifest.json'}:raise LifecycleError('snapshot_unknown_entry')
        total=0
        for name in names-{'manifest.json'}:
            f=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=d)
            try:total+=regular(f,header['files'][name]).st_size
            finally:os.close(f)
        if total>ROOT_RESERVATION:raise LifecycleError('snapshot_disk_budget')
        return header,state,processes,total
    except (OSError,ValueError,TypeError,KeyError):
        raise LifecycleError('snapshot_manifest_invalid') from None
    finally:os.close(d)


def _remove(root,header):
    actual,state,_,_=_read(root)
    if actual!=header:raise LifecycleError('snapshot_cleanup_identity')
    if state!='closing':_append(root,header,dict(state='closing'))
    d=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        if identity(os.fstat(d))!=header['root']:raise LifecycleError('snapshot_cleanup_identity')
        for name in os.listdir(d):
            expected=header['manifest'] if name=='manifest.json' else header['files'][name]
            f=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=d)
            try:regular(f,expected)
            finally:os.close(f)
        for name in os.listdir(d):
            if name!='manifest.json':os.unlink(name,dir_fd=d)
        retired=BASE/('retired-'+header['id']+'.json')
        if 'manifest.json' in os.listdir(d):
            # Atomic no-replace move keeps an ownership proof outside the root
            # across the unavoidable final unlink/rmdir crash window.
            base=os.open(BASE,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
            try:
                libc=ctypes.CDLL(None,use_errno=True)
                if libc.renameat2(d,b'manifest.json',base,os.fsencode(retired.name),1):
                    raise LifecycleError('snapshot_retire_failed')
            finally:os.close(base)
        if identity(root.lstat())!=header['root']:raise LifecycleError('snapshot_cleanup_identity')
        root.rmdir()
        retired.unlink()
    finally:os.close(d)


def _append(root,header,row):
    f=os.open(root/'manifest.json',os.O_RDWR|os.O_APPEND|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        s=regular(f,header['manifest']);raw=json.dumps(row,separators=(',',':')).encode()+b'\n'
        current=os.pread(f,4097,0);committed=current.rfind(b'\n')+1
        if not committed or committed+len(raw)>4096:raise LifecycleError('snapshot_manifest_budget')
        os.ftruncate(f,committed)
        if os.write(f,raw)!=len(raw):raise LifecycleError('snapshot_manifest_write')
        os.fsync(f)
    finally:os.close(f)


def _reap(processes):
    for proc in processes:
        if not alive(proc):continue
        if proc['pid']==os.getpid():continue  # Trusted producer cleanup after owner death.
        try:
            if os.getpgid(proc['pid'])!=proc['pid']:raise LifecycleError('snapshot_process_identity')
            os.killpg(proc['pid'],signal.SIGKILL)
        except ProcessLookupError:continue
        until=time.monotonic()+1
        while alive(proc):
            if time.monotonic()>=until:raise LifecycleError('snapshot_process_cleanup')
            time.sleep(.005)


def _scan():
    roots=[]
    names=os.listdir(BASE)
    if len(names)>64:raise LifecycleError('snapshot_root_budget')
    for name in names:
        if name=='manager.lock':continue
        if re.fullmatch(r'retired-[a-f0-9]{32}\.json',name):continue
        root=BASE/name;header,state,processes,size=_read(root)
        if not alive(header['owner']):
            _reap(processes);_remove(root,header)
        else:roots.append((root,header,size))
    for name in names:
        if not re.fullmatch(r'retired-[a-f0-9]{32}\.json',name):continue
        p=BASE/name
        try:f=os.open(p,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
        except FileNotFoundError:continue
        try:
            s=regular(f);raw=os.read(f,4097)
            records=[json.loads(row,object_pairs_hook=unique_object) for row in raw.split(b'\n')[:-1]]
            if len(raw)>4096 or not records:raise LifecycleError('snapshot_retired_invalid')
            h=records[0]
            if (not isinstance(h,dict) or h.get('version')!=1 or h.get('manifest')!=identity(s)
                    or name!='retired-'+h.get('id','')+'.json' or not valid_inode(h.get('root'))
                    or not valid_process(h.get('owner')) or records[-1]!={'state':'closing'}):
                raise LifecycleError('snapshot_retired_invalid')
            if (BASE/('snapshot-'+h['id'])).exists():raise LifecycleError('snapshot_retired_active')
        except (OSError,ValueError,TypeError,KeyError):raise LifecycleError('snapshot_retired_invalid') from None
        finally:os.close(f)
        p.unlink()
    return roots


def scavenge():
    """Safe startup/lazy-start recovery, never arbitrary /tmp scanning."""
    try:
        with locked():return len(_scan())
    except (OSError,ValueError):raise LifecycleError('snapshot_recovery_unavailable') from None


class ManagedRoot:
    def __init__(self):
        with locked():
            active=_scan()
            if len(active)>=MAX_ROOTS or (len(active)+1)*ROOT_RESERVATION>TOTAL_BUDGET:
                raise LifecycleError('snapshot_budget')
            self.root=BASE/('snapshot-'+uuid4().hex)
            self.root.mkdir(mode=0o700)
            root_identity=identity(self.root.lstat());created={};files={}
            try:
                for name in FILES:
                    fd=os.open(self.root/name,os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
                    try:created[name]=identity(os.fstat(fd));files[name]=identity(regular(fd))
                    finally:os.close(fd)
                fd=os.open(self.root/'manifest.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
                try:
                    created['manifest.json']=identity(os.fstat(fd))
                    header=dict(version=1,id=self.root.name.removeprefix('snapshot-'),owner=process_identity(os.getpid()),
                        created=time.time(),root=root_identity,manifest=identity(regular(fd)),files=files,state='creating')
                    raw=json.dumps(header,separators=(',',':')).encode()+b'\n'
                    if os.write(fd,raw)!=len(raw):raise LifecycleError('snapshot_manifest_write')
                    os.fsync(fd)
                finally:os.close(fd)
            except BaseException:
                # Construction has not copied source data. Delete only files
                # whose creation identity this live constructor witnessed.
                if identity(self.root.lstat())==root_identity and set(os.listdir(self.root))<=set(created):
                    for name,expected in created.items():
                        f=os.open(self.root/name,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
                        try:regular(f,expected)
                        finally:os.close(f)
                    for name in created:(self.root/name).unlink()
                    self.root.rmdir()
                raise LifecycleError('snapshot_creation_failed') from None
            self.header=header
            self.path=self.root/'read.sqlite';self.identity=tuple(files['read.sqlite'])
            self.root_identity=self.root.lstat()

    @classmethod
    def attach(cls,path):
        obj=object.__new__(cls);obj.path=Path(path);obj.root=obj.path.parent
        with locked():
            obj.header,_,_,_=_read(obj.root)
            obj.identity=tuple(obj.header['files']['read.sqlite']);obj.root_identity=obj.root.lstat()
        return obj

    def check(self):
        header,_,_,_=_read(self.root)
        if header!=self.header:raise LifecycleError('snapshot_root_identity')

    def append(self,row):
        with locked():
            self.check()
            if time.time()-self.header['created']>TTL_SECONDS:raise LifecycleError('snapshot_expired')
            _append(self.root,self.header,row)

    def register(self,child):
        proc=process_identity(child.pid)
        if proc is None:raise LifecycleError('snapshot_child_unavailable')
        self.append(dict(process=proc))

    def finalize(self):
        with locked():
            self.check()
            for name in FILES[1:]:
                p=self.root/name
                if p.exists():
                    if name.startswith('read.sqlite') and p.stat().st_size:
                        raise LifecycleError('snapshot_incomplete')
                    p.unlink()
        self.append(dict(state='ready'))

    def close(self):
        with locked():
            self.check();_remove(self.root,self.header)
