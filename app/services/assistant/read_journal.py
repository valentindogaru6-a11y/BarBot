"""Conservative rollback-journal gate, never recovery or immutable source IO.

SQLite lockingv3.html#the_rollback_journal and fileformat.html#the_rollback_journal:
header/size and RESERVED-lock state matter, not filename existence alone.
Super-journal/ambiguous/truncated states are deliberately recovery-required.
"""
import fcntl
import os
import struct
import stat


class RecoveryRequired(Exception):
    pass


def classify(journal, database_fd):
    """Only approved RO descriptors; source bytes/locks are never modified."""
    try:
        fd=os.open(journal,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC|os.O_NOATIME)
    except FileNotFoundError:
        return 'absent'
    except OSError:
        raise RecoveryRequired from None
    try:
        before=os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1:raise RecoveryRequired
        header=os.pread(fd,28,0)
        after=os.fstat(fd)
        if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(
                after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):
            raise RecoveryRequired
        if not before.st_size:
            return 'empty'
        # A completely zero header is a documented invalidated/stale journal.
        # Any non-zero truncated/unknown header is not evidence of safety.
        if before.st_size>=512 and len(header)==28 and not any(header):
            return 'stale'
        if len(header)!=28 or before.st_size<=512 or header[:8]!=bytes.fromhex('d9d505f920a163d7'):
            raise RecoveryRequired
        _,_,_,sector,page=struct.unpack('>IIIII',header[8:])
        if (not 512<=sector<=65536 or sector & (sector-1)
                or not 512<=page<=65536 or page & (page-1)):
            raise RecoveryRequired
        # Native Linux struct flock, x86-64 runtime is already mandatory.
        query=struct.pack('hhqqi',fcntl.F_WRLCK,os.SEEK_SET,0x40000001,1,0)
        answer=fcntl.fcntl(database_fd,fcntl.F_GETLK,query)
        lock=struct.unpack('hhqqi',answer[:struct.calcsize('hhqqi')])[0]
        if lock==fcntl.F_UNLCK:
            raise RecoveryRequired
        if lock!=fcntl.F_WRLCK:
            raise RecoveryRequired
        return 'live_writer'  # Normal SQLite locking still arbitrates reading.
    except (OSError,ValueError,struct.error):
        raise RecoveryRequired from None
    finally:
        os.close(fd)
