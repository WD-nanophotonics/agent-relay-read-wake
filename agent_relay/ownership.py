from __future__ import annotations

import os
from pathlib import Path


def exact_owner_live(owner: dict | None) -> bool:
    """Validate one recorded PID and executable; never use age-only liveness."""
    if not isinstance(owner, dict) or not isinstance(owner.get("pid"), int) or owner["pid"] <= 0:
        return False
    pid = owner["pid"]
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False
    try:
        import ctypes
        from ctypes import wintypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            size = wintypes.DWORD(32768)
            buf = ctypes.create_unicode_buffer(size.value)
            if not ctypes.windll.kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return False
            expected = str(owner.get("exe") or "")
            return not expected or Path(buf.value).name.casefold() == Path(expected).name.casefold()
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except (AttributeError, OSError):
        return False
