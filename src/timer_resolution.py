"""Process-wide, reference-counted Windows multimedia timer resolution."""

import atexit
import os
import threading


_lock = threading.Lock()
_users = 0
_suspended = False


def acquire() -> bool:
    global _users
    try:
        import power_saving
        if not power_saving.precision_timing_allowed():
            return False
    except Exception:
        pass
    if os.name != "nt":
        return False
    with _lock:
        if _suspended:
            return False
        if _users == 0 and not _suspended:
            try:
                import ctypes
                if ctypes.windll.winmm.timeBeginPeriod(1) != 0:
                    return False
            except Exception:
                return False
        _users += 1
        return True


def release() -> None:
    global _users
    if os.name != "nt":
        return
    with _lock:
        if _users <= 0:
            return
        _users -= 1
        if _users == 0 and not _suspended:
            try:
                import ctypes
                ctypes.windll.winmm.timeEndPeriod(1)
            except Exception:
                pass


def set_suspended(suspended: bool) -> None:
    """Temporarily drop the OS timer request without losing existing leases."""
    global _suspended
    if os.name != "nt":
        return
    with _lock:
        suspended = bool(suspended)
        if suspended == _suspended:
            return
        _suspended = suspended
        try:
            import ctypes
            if suspended and _users > 0:
                ctypes.windll.winmm.timeEndPeriod(1)
            elif not suspended and _users > 0:
                ctypes.windll.winmm.timeBeginPeriod(1)
        except Exception:
            pass


def _release_all() -> None:
    global _users
    if os.name != "nt":
        return
    with _lock:
        if _users <= 0:
            return
        _users = 0
        if not _suspended:
            try:
                import ctypes
                ctypes.windll.winmm.timeEndPeriod(1)
            except Exception:
                pass


atexit.register(_release_all)
