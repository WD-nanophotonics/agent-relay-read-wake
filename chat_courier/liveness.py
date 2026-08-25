"""Safe process-liveness checks for Courier's Windows control plane."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os


_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_INVALID_PARAMETER = 87


def _windows_process_alive(pid: int) -> bool:
    """Return false only when Windows proves that the PID no longer exists.

    ``os.kill(pid, 0)`` is not a liveness probe on Windows: Python documents
    that non-console signals use ``TerminateProcess``.  An access-denied or
    otherwise indeterminate query is treated as live so the queue fails closed
    instead of skipping an unknown active Courier.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # ERROR_INVALID_PARAMETER is Windows' documented "no such PID" case.
        # Every other error is an unknown state and must retain the queue entry.
        return ctypes.get_last_error() != _ERROR_INVALID_PARAMETER
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def process_alive(pid: int) -> bool:
    """Read-only liveness check; never signal or terminate a process."""
    if pid <= 0:
        return False
    if os.name == "nt":
        return _windows_process_alive(pid)
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True
