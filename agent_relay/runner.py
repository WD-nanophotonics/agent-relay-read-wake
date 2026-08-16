from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from threading import Event, Thread
import uuid

from .config import RelayConfig
from .gmail import GoogleGmailGateway
from .storage import atomic_json, now
from .supervisor import Supervisor, SupervisorState
from .wake import CodexAppServerWakeAdapter, CodexCliWakeAdapter, CodexTarget, MockWakeAdapter


METADATA_PROTOCOL = "AGENTRELAY_RUNNER/1"
HEARTBEAT_STALE_SECONDS = 15


def _lock_file(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    # O_EXCL is an atomic cross-process ownership primitive on Windows and
    # POSIX. It avoids the process-scoped semantics of msvcrt byte locks.
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
    except (FileExistsError, PermissionError):
        return None
    return os.fdopen(fd, "r+", encoding="utf-8")


def _unlock_file(handle) -> None:
    try:
        path = Path(handle.name)
    except (AttributeError, TypeError):
        path = None
    handle.close()
    if path is not None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


@dataclass
class RunnerOwnership:
    storage: Path
    project_id: str
    runner_id: str = ""
    handle: object | None = None

    @property
    def lock_path(self) -> Path: return self.storage / "runner.lock"
    @property
    def metadata_path(self) -> Path: return self.storage / "runner.json"
    @property
    def control_path(self) -> Path: return self.storage / "runner.control.json"

    def acquire(self) -> bool:
        self.handle = _lock_file(self.lock_path)
        if self.handle is None:
            # Recover an orphaned lock only when its recorded owner is no
            # longer alive. A live owner is never stolen or terminated.
            try:
                metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))
                stale_pid = int(metadata.get("pid", 0))
            except (OSError, ValueError, json.JSONDecodeError):
                stale_pid = 0
            if not _pid_alive(stale_pid):
                try:
                    self.lock_path.unlink()
                except FileNotFoundError:
                    pass
                self.handle = _lock_file(self.lock_path)
        if self.handle is None:
            return False
        self.runner_id = str(uuid.uuid4())
        self.update(state="STARTING", pid=os.getpid(), started_at=now(), heartbeat_at=now())
        return True

    def update(self, *, state: str, **values) -> None:
        data = {"protocol": METADATA_PROTOCOL, "project_id": self.project_id, "runner_id": self.runner_id, "pid": os.getpid(), "state": state, "heartbeat_at": now(), **values}
        atomic_json(self.metadata_path, data)

    def request_stop(self, runner_id: str) -> None:
        atomic_json(self.control_path, {"protocol": METADATA_PROTOCOL, "project_id": self.project_id, "runner_id": runner_id, "action": "stop", "requested_at": now()})

    def stop_requested(self) -> bool:
        try:
            value = json.loads(self.control_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return value.get("protocol") == METADATA_PROTOCOL and value.get("project_id") == self.project_id and value.get("runner_id") == self.runner_id and value.get("action") == "stop"

    def clear_control(self) -> None:
        try: self.control_path.unlink()
        except FileNotFoundError: pass

    def release(self, state: str = "STOPPED", **values) -> None:
        if self.handle is None:
            return
        self.update(state=state, **values)
        self.clear_control()
        _unlock_file(self.handle)
        self.handle = None


def _pid_alive(pid: int) -> bool:
    if pid <= 0: return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def runner_status(config: RelayConfig) -> dict[str, object]:
    storage = config.local_project_storage
    metadata_path = storage / "runner.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        metadata = {"protocol": METADATA_PROTOCOL, "project_id": config.project_id, "state": "STOPPED"}
    if metadata.get("protocol") != METADATA_PROTOCOL or metadata.get("project_id") != config.project_id:
        metadata["runner_state"] = "AMBIGUOUS_OWNERSHIP"
        return metadata
    heartbeat = metadata.get("heartbeat_at", "")
    try:
        age = (datetime.now(UTC) - datetime.fromisoformat(str(heartbeat))).total_seconds()
    except (TypeError, ValueError):
        age = HEARTBEAT_STALE_SECONDS + 1
    pid_alive = _pid_alive(int(metadata.get("pid", 0)))
    active_state = metadata.get("state") not in {None, "STOPPED"}
    if pid_alive and active_state:
        # A Windows byte-range lock is process-scoped: probing from the same
        # process can succeed even while this process owns the lock. The
        # heartbeat/PID pair is therefore authoritative for a live owner.
        metadata["runner_state"] = "RUNNING_HEALTHY" if age <= HEARTBEAT_STALE_SECONDS else "RUNNING_STALE_OR_UNRESPONSIVE"
        return metadata
    # No live owner remains. A missing lock is the normal stopped state; a
    # stale lock is recoverable by the next start attempt after PID validation.
    metadata["runner_state"] = "STOPPED" if not (storage / "runner.lock").exists() else "RUNNING_STALE_OR_UNRESPONSIVE"
    return metadata


def build_adapter(config: RelayConfig):
    target = CodexTarget(config.target_type, config.target_id, config.target_label, config.repo_path)
    if config.target_type == "mock": return MockWakeAdapter()
    if config.target_type == "codex-app-server": return CodexAppServerWakeAdapter(target, config.local_project_storage / "logs", config.codex_command, config.local_project_storage, config.dev_session_id)
    return CodexCliWakeAdapter(target, config.local_project_storage / "logs", config.codex_command)


class BackgroundRunner:
    def __init__(self, config: RelayConfig, *, gateway=None, adapter=None, heartbeat_interval: float = 2.0):
        self.config = config
        self.gateway = gateway or GoogleGmailGateway(config.gmail_auth_home)
        self.adapter = adapter or build_adapter(config)
        self.heartbeat_interval = heartbeat_interval
        self.stop_event = Event()
        self.owner = RunnerOwnership(config.local_project_storage, config.project_id)
        self.relay: Supervisor | None = None

    def run(self) -> int:
        if not self.owner.acquire():
            return 2
        try:
            self.relay = Supervisor(self.config, self.gateway, self.adapter)
            self.relay.start()
            self.owner.update(state="RUNNING", supervisor_state=self.relay.snapshot().get("state"), backend_pid=getattr(getattr(self.adapter, "controller", None), "pid", None), worker_id=getattr(self.adapter, "worker_id", ""), worker_status=getattr(self.adapter, "worker_status", "unknown"))
            last_poll = 0.0
            poll_thread: Thread | None = None
            while not self.stop_event.is_set():
                if self.owner.stop_requested():
                    if self.relay.snapshot().get("active_lease"):
                        self.owner.update(state="ACTIVE_LEASE", supervisor_state=self.relay.snapshot().get("state"), worker_id=getattr(self.adapter, "worker_id", ""), worker_status=getattr(self.adapter, "worker_status", "unknown"))
                        # Keep the request durable. Once the lease completes,
                        # the next boundary will observe no active lease and
                        # perform the requested stop safely.
                    else:
                        self.relay.stop()
                        break
                current_time = time.monotonic()
                if current_time - last_poll >= self.config.poll_interval and (poll_thread is None or not poll_thread.is_alive()):
                    # Gmail I/O is allowed to block independently; the
                    # supervisor heartbeat must remain observable even when a
                    # provider request is slow or temporarily unavailable.
                    poll_thread = Thread(target=self.relay.poll_once, name="agent-relay-gmail-poll", daemon=True)
                    poll_thread.start()
                    last_poll = current_time
                snap = self.relay.snapshot()
                owner_state = "ACTIVE_LEASE" if self.owner.stop_requested() and snap.get("active_lease") else "RUNNING"
                self.owner.update(state=owner_state, supervisor_state=snap.get("state"), backend_pid=getattr(getattr(self.adapter, "controller", None), "pid", None), worker_id=getattr(self.adapter, "worker_id", ""), worker_status=getattr(self.adapter, "worker_status", "unknown"), current_run=snap.get("current_run"), expected_step=snap.get("expected_step"), active_lease_id=(snap.get("active_lease") or {}).get("lease_id"))
                self.stop_event.wait(min(self.config.poll_interval, self.heartbeat_interval))
            return 0
        except Exception as exc:
            self.owner.update(state="ERROR", error=str(exc)[:500])
            return 1
        finally:
            if self.relay and self.relay.snapshot().get("state") != SupervisorState.STOPPED:
                self.relay.stop()
            self.owner.release("STOPPED")


def start_background(config: RelayConfig, timeout: float = 35.0) -> tuple[bool, str]:
    current = runner_status(config)
    if current.get("runner_state") == "RUNNING_HEALTHY":
        return True, "already-running"
    if current.get("runner_state") == "AMBIGUOUS_OWNERSHIP":
        return False, "ambiguous ownership"
    log_path = config.local_project_storage / "logs" / "background-runner.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("a", encoding="utf-8")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    startup = None
    if hasattr(subprocess, "STARTUPINFO"):
        startup = subprocess.STARTUPINFO(); startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW; startup.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
    try:
        subprocess.Popen([sys.executable, "-m", "agent_relay.cli", "run-background-worker"], cwd=config.repo_path, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, creationflags=flags, startupinfo=startup, close_fds=True)
    finally:
        log.close()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = runner_status(config)
        if status.get("runner_state") == "RUNNING_HEALTHY": return True, "started"
        if status.get("runner_state") == "AMBIGUOUS_OWNERSHIP": return False, "ambiguous ownership"
        time.sleep(0.5)
    return False, "startup timeout"


def stop_background(config: RelayConfig, timeout: float = 15.0) -> tuple[bool, str]:
    current = runner_status(config)
    if current.get("runner_state") == "STOPPED": return True, "already-stopped"
    runner_id = str(current.get("runner_id", ""))
    if not runner_id: return False, "runner identity unavailable"
    owner = RunnerOwnership(config.local_project_storage, config.project_id)
    owner.request_stop(runner_id)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = runner_status(config)
        if status.get("runner_state") == "STOPPED": return True, "stopped"
        if status.get("state") == "ACTIVE_LEASE": return False, "ACTIVE_LEASE"
        time.sleep(0.5)
    return False, "stop timeout"
