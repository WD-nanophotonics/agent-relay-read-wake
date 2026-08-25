from __future__ import annotations

from dataclasses import dataclass, asdict
import ctypes
from datetime import UTC, datetime
import os
from pathlib import Path
import secrets
import subprocess
from typing import Any

from .liveness import process_alive
from .model import atomic_json, runtime_root


class OwnerBusy(RuntimeError):
    """A live Courier owns the dedicated browser profile."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def owner_path() -> Path:
    return runtime_root() / "owner.json"


def mutex_name() -> str:
    # Windows object namespaces permit the Local/Global prefix followed by a
    # single object name. Keep the name stable and avoid nested separators.
    return "Local\\ChatCourier-DedicatedChatProfile"


@dataclass
class OwnerRecord:
    project_id: str
    request_id: str
    owner_pid: int
    owner_nonce: str
    phase: str
    heartbeat_at: str
    cdp_port: int | None = None
    browser_pid: int | None = None
    profile: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def read_owner() -> OwnerRecord | None:
    path = owner_path()
    if not path.exists():
        return None
    try:
        raw = __import__("json").loads(path.read_text(encoding="utf-8"))
        return OwnerRecord(
            project_id=str(raw["project_id"]), request_id=str(raw["request_id"]),
            owner_pid=int(raw["owner_pid"]), owner_nonce=str(raw["owner_nonce"]),
            phase=str(raw["phase"]), heartbeat_at=str(raw["heartbeat_at"]),
            cdp_port=int(raw["cdp_port"]) if raw.get("cdp_port") is not None else None,
            browser_pid=int(raw["browser_pid"]) if raw.get("browser_pid") is not None else None,
            profile=str(raw["profile"]) if raw.get("profile") is not None else None,
        )
    except (OSError, KeyError, TypeError, ValueError, __import__("json").JSONDecodeError) as exc:
        raise OwnerBusy(f"owner metadata is malformed: {path}") from exc


class OwnerLease:
    def __init__(self, project_id: str, request_id: str, *, profile: str | None = None):
        self.project_id = project_id
        self.request_id = request_id
        self.profile = profile
        self.record: OwnerRecord | None = None
        self._handle: int | None = None
        self._fallback_path: Path | None = None

    def acquire(self, phase: str = "starting") -> OwnerRecord:
        runtime_root().mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
            kernel32.CreateMutexW.restype = ctypes.c_void_p
            handle = kernel32.CreateMutexW(None, True, ctypes.c_wchar_p(mutex_name()))
            if not handle:
                raise OwnerBusy(f"could not create browser mutex: {ctypes.get_last_error()}")
            if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
                kernel32.CloseHandle(handle)
                raise OwnerBusy("dedicated ChatGPT browser is owned by another live Courier")
            self._handle = int(handle)
        else:
            fallback = runtime_root() / "owner.fallback.lock"
            try:
                fd = os.open(fallback, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError as exc:
                raise OwnerBusy("dedicated ChatGPT browser is owned by another Courier") from exc
            os.close(fd)
            self._fallback_path = fallback
        self.record = OwnerRecord(self.project_id, self.request_id, os.getpid(), secrets.token_hex(16), phase, _now(), profile=self.profile)
        self._write()
        return self.record

    def _write(self) -> None:
        if self.record is not None:
            self.record.heartbeat_at = _now()
            atomic_json(owner_path(), self.record.as_dict())

    def update(self, phase: str, *, cdp_port: int | None = None, browser_pid: int | None = None) -> OwnerRecord | None:
        if self.record is None:
            return None
        self.record.phase = phase
        if cdp_port is not None:
            self.record.cdp_port = cdp_port
        if browser_pid is not None:
            self.record.browser_pid = browser_pid
        self._write()
        return self.record

    def release(self) -> None:
        record = self.record
        self.record = None
        try:
            current = read_owner()
            if record is not None and current is not None and current.owner_nonce == record.owner_nonce:
                owner_path().unlink(missing_ok=True)
        except (OSError, OwnerBusy):
            pass
        if self._handle is not None and os.name == "nt":
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(self._handle)
            self._handle = None
        if self._fallback_path is not None:
            self._fallback_path.unlink(missing_ok=True)
            self._fallback_path = None


def terminate_orphan_browser(record: OwnerRecord) -> bool:
    """Terminate only a recorded Chrome whose command line still owns the profile."""
    if record.browser_pid is None or not record.profile or not process_alive(record.browser_pid):
        return True
    if os.name != "nt":
        return False
    command = "(Get-CimInstance Win32_Process -Filter 'ProcessId=%d').CommandLine" % record.browser_pid
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    command_line = result.stdout.strip().lower()
    if "chrome.exe" not in command_line or record.profile.lower() not in command_line:
        return False
    try:
        os.kill(record.browser_pid, 15)
    except OSError:
        return False
    return True
