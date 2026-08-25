"""Small cross-process locks for Courier's local control-plane files."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import time


class RuntimeLock:
    """A short-lived named mutex on Windows, with a portable file fallback."""
    def __init__(self, name: str, root: Path, *, timeout_seconds: float = 5.0):
        self.name, self.root, self.timeout_seconds = name, root, timeout_seconds
        self.handle: int | None = None
        self.path: Path | None = None

    def __enter__(self) -> "RuntimeLock":
        if os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
            kernel32.CreateMutexW.restype = ctypes.c_void_p
            kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            kernel32.WaitForSingleObject.restype = ctypes.c_uint32
            kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = kernel32.CreateMutexW(None, False, f"Local\\{self.name}")
            if not handle:
                raise RuntimeError(f"could not create Courier mutex {self.name}: {ctypes.get_last_error()}")
            result = kernel32.WaitForSingleObject(handle, int(self.timeout_seconds * 1000))
            if result not in {0, 0x80}:  # WAIT_OBJECT_0 / WAIT_ABANDONED
                kernel32.CloseHandle(handle)
                raise RuntimeError(f"timed out acquiring Courier mutex {self.name}")
            self.handle = int(handle)
            return self
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / f".{self.name}.lock"
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                return self
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"timed out acquiring Courier lock {self.name}")
                time.sleep(0.05)

    def __exit__(self, *_: object) -> None:
        if self.handle is not None and os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.ReleaseMutex(ctypes.c_void_p(self.handle))
            kernel32.CloseHandle(ctypes.c_void_p(self.handle))
            self.handle = None
        if self.path is not None:
            self.path.unlink(missing_ok=True)
            self.path = None
