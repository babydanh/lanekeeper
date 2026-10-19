"""Cross-platform check that a process id still belongs to a running process.

Used for stale-lock detection and for telling which agent sessions are still alive.
"""

from __future__ import annotations

import os

_WINDOWS_STILL_ACTIVE = 259
_WINDOWS_QUERY_LIMITED_INFORMATION = 0x1000


def pid_alive(pid: object) -> bool:
    """True when `pid` names a running process. Anything that is not a positive int is False."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if os.name == "nt":
        return _windows_alive(pid)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True  # the process exists but belongs to another user
    except OSError:
        return False
    return True


def _windows_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(_WINDOWS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == _WINDOWS_STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)
