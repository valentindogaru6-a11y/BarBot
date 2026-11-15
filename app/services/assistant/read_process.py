"""Linux child containment; fixed bootstrap only, no application settings."""
import ctypes
import os
import signal


def arm_parent_death(parent, death_signal=signal.SIGKILL):
    if os.getppid() != parent:
        raise SystemExit(73)
    if ctypes.CDLL(None, use_errno=True).prctl(1, death_signal, 0, 0, 0):
        raise SystemExit(73)
    if os.getppid() != parent:
        raise SystemExit(73)
