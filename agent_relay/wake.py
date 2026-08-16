from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
import secrets
import subprocess
import time
from typing import Protocol
from uuid import UUID, uuid4

from .app_server import AppServerController, AppServerError


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


class LeaseStatus(StrEnum):
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class LeaseKind(StrEnum):
    DIAGNOSTIC = "DIAGNOSTIC"
    WORK = "WORK"


@dataclass(frozen=True)
class CodexTarget:
    target_type: str
    target_id: str
    label: str
    repo_path: Path
    app_metadata: dict[str, str] | None = None


@dataclass(frozen=True)
class WakeLease:
    lease_id: str
    project_id: str
    run_id: str
    step: int
    staged_instruction_path: Path
    created_at: str
    lease_kind: LeaseKind = LeaseKind.WORK
    completion_token: str = ""
    worker_id: str = ""
    status: LeaseStatus = LeaseStatus.ACTIVE
    handoff_token: str = ""
    turn_id: str = ""

    @classmethod
    def create(cls, project_id: str, run_id: str, step: int, path: Path, lease_kind: LeaseKind = LeaseKind.WORK, worker_id: str = "") -> "WakeLease":
        return cls(str(uuid4()), project_id, run_id, step, path.resolve(), utcnow(), lease_kind, secrets.token_urlsafe(32), worker_id, LeaseStatus.ACTIVE, secrets.token_urlsafe(32) if lease_kind is LeaseKind.WORK else "")


@dataclass(frozen=True)
class WakeResult:
    accepted: bool
    detail: str = ""
    completed: bool = False
    process_id: int | None = None
    turn_id: str | None = None


class WakeAdapter(Protocol):
    def wake(self, lease: WakeLease, wake_instruction: str) -> WakeResult: ...
    def validate_target(self, target: CodexTarget) -> WakeResult: ...


def wake_instruction(lease: WakeLease) -> str:
    if lease.lease_kind is LeaseKind.DIAGNOSTIC:
        return f"""AGENTRELAY_DIAGNOSTIC_WAKE/1\n\nProject: {lease.project_id}\nLease: {lease.lease_id}\nWorker: {lease.worker_id}\nStaged instruction: {lease.staged_instruction_path}\n\nThis is a no-side-effect diagnostic lease. Do not modify repository files, access Gmail or Chrome, commit, push, create another turn, wait, or retry. Verify the identities above, then execute exactly once:\n{diagnostic_completion_command(lease)}\nAfter the command succeeds, terminate this Codex turn.\n"""
    return f"""AGENTRELAY_WAKE/1\n\nProject: {lease.project_id}\nRun: {lease.run_id}\nStep: {lease.step:04d}\nLease: {lease.lease_id}\nWorker: {lease.worker_id}\nStaged instruction: {lease.staged_instruction_path}\nHandoff token: {lease.handoff_token}\n\nRead the authoritative staged instruction and execute exactly one bounded work lease. Do not modify source, access Gmail, start another turn, wait for ChatGPT, or retry. Prepare this exact handoff report for the Supervisor-owned supported browser integration:\nAGENTRELAY PHASE2H HANDOFF CERTIFICATION\nRUN: {lease.run_id}\nSTEP: {lease.step:04d}\nLEASE: {lease.lease_id}\nWORKER: {lease.worker_id}\nHANDOFF_TOKEN: {lease.handoff_token}\nRESULT: WORK_HANDOFF_DIAGNOSTIC\nDo not use unsupported GUI automation. The Supervisor host performs the single fixed ChatGPT URL browser submission and writes evidence after bounded verification; terminate this turn after preparing the report.\n"""


def diagnostic_completion_command(lease: WakeLease) -> str:
    if lease.lease_kind is not LeaseKind.DIAGNOSTIC:
        raise ValueError("completion command requires a diagnostic lease")
    return f"python -m agent_relay.cli complete-diagnostic --lease-id {lease.lease_id} --completion-token {lease.completion_token}"


def work_completion_command(lease: WakeLease) -> str:
    if lease.lease_kind is not LeaseKind.WORK:
        raise ValueError("completion command requires a work lease")
    return f"python -m agent_relay.cli complete-work --lease-id {lease.lease_id} --completion-token {lease.completion_token} --handoff-token {lease.handoff_token}"


class MockWakeAdapter:
    """Certification-only adapter: it never invokes Codex or external UI automation."""
    def __init__(self, succeed: bool = True):
        self.succeed = succeed
        self.calls: list[tuple[WakeLease, str]] = []

    def wake(self, lease: WakeLease, wake_instruction: str) -> WakeResult:
        self.calls.append((lease, wake_instruction))
        return WakeResult(self.succeed, "mock accepted" if self.succeed else "mock configured to fail", completed=self.succeed)

    def validate_target(self, target: CodexTarget) -> WakeResult:
        return WakeResult(target.target_type == "mock", "mock target ready" if target.target_type == "mock" else "not a mock target")


class CodexCliWakeAdapter:
    """Real wake fallback using official `codex exec resume`, always without a console."""
    def __init__(self, target: CodexTarget, log_dir: Path, command: str = "codex.cmd"):
        self.target, self.log_dir, self.command = target, log_dir, command

    def validate_target(self, target: CodexTarget) -> WakeResult:
        try:
            UUID(target.target_id)
        except ValueError:
            return WakeResult(False, "Codex target ID must be a UUID")
        if target != self.target or target.target_type != "codex-cli" or not target.repo_path.is_dir():
            return WakeResult(False, "bound Codex target is unavailable or mismatched")
        return WakeResult(True, "explicit Codex CLI target validated")

    def wake(self, lease: WakeLease, instruction: str) -> WakeResult:
        valid = self.validate_target(self.target)
        if not valid.accepted:
            return valid
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log = (self.log_dir / f"codex-{lease.lease_id}.log").open("a", encoding="utf-8")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        startup = None
        if hasattr(subprocess, "STARTUPINFO"):
            startup = subprocess.STARTUPINFO(); startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW; startup.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
        try:
            process = subprocess.Popen([self.command, "exec", "--approve-for-me", "resume", self.target.target_id, instruction], cwd=self.target.repo_path, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, shell=False, creationflags=flags, startupinfo=startup)
        except OSError as exc:
            log.close()
            return WakeResult(False, f"Codex CLI launch failed: {exc}")
        log.close()
        return WakeResult(True, "Codex CLI process launched", completed=False, process_id=process.pid)


class CodexAppServerWakeAdapter:
    """Primary Phase 2F backend: one Supervisor-owned App Server connection."""

    def __init__(self, target: CodexTarget, log_dir: Path, command: str = "codex.cmd", local_project_storage: Path | None = None, dev_session_id: str = ""):
        self.target = target
        self.log_dir = log_dir
        self.command = command
        self.local_project_storage = local_project_storage or target.repo_path
        self.dev_session_id = dev_session_id
        self.controller: AppServerController | None = None
        self.worker_id = target.target_id
        self.worker_status = "unknown"
        self.superseded_worker_id: str | None = None
        self.last_turn_id: str | None = None
        self.last_turn_status: str | None = None
        self.last_error: str | None = None
        self.last_turn_started_at: float | None = None
        self.interrupt_grace_seconds = 5.0
        self.interrupt_attempts: set[tuple[str, str]] = set()

    def start_backend(self) -> WakeResult:
        if self.target.target_type != "codex-app-server":
            return WakeResult(False, "App Server adapter requires codex-app-server target")
        if not self.worker_id or self.worker_id == self.dev_session_id:
            return WakeResult(False, "bound App Server worker is missing or equals DEV")
        self.controller = AppServerController(self.command, self.target.repo_path, self.log_dir / "app-server.log", self.worker_id, self.dev_session_id)
        try:
            self.controller.start()
            observed = self.controller.find_worker(self.worker_id)
            if observed is None:
                try:
                    observed = self.controller.read_worker(self.worker_id)
                except AppServerError:
                    observed = None
            if observed and observed.source_kind != "appServer":
                self.superseded_worker_id = self.worker_id
                created = self.controller.start_worker()
                observed = created
                self.worker_id = created.thread_id
                self.worker_status = created.status
            elif observed and observed.status in {"idle"}:
                self.worker_status = observed.status
            elif observed and observed.status == "notLoaded":
                try:
                    resumed = self.controller.resume_worker(self.worker_id)
                    self.worker_status = resumed.status
                except AppServerError as exc:
                    if "active writer" not in str(exc):
                        raise
                    self.superseded_worker_id = self.worker_id
                    created = self.controller.start_worker()
                    observed = created
                    self.worker_id = created.thread_id
                    self.worker_status = created.status
            elif observed and observed.status in {"active", "systemError"}:
                self.superseded_worker_id = self.worker_id
                created = self.controller.start_worker()
                observed = created
                self.worker_id = created.thread_id
                self.worker_status = created.status
            else:
                created = self.controller.start_worker()
                observed = created
                self.worker_id = created.thread_id
                self.worker_status = created.status
            if self.worker_id == self.dev_session_id or self.worker_status not in {"idle", "notLoaded"}:
                raise AppServerError(f"unsafe worker status: {self.worker_status}")
            if observed and observed.source_kind and observed.source_kind != "appServer":
                raise AppServerError(f"worker source is not App Server-owned: {observed.source_kind}")
            if self.worker_id != self.target.target_id:
                from .config import EXPECTED_CHAT_URL, app_home, save_binding
                save_binding(app_home(), target_id=self.worker_id, target_type="codex-app-server", chat_url=EXPECTED_CHAT_URL, dev_session_id=self.dev_session_id)
            return WakeResult(True, "App Server initialized", process_id=self.controller.pid)
        except Exception as exc:
            self.last_error = str(exc)
            self.stop_backend()
            return WakeResult(False, f"App Server startup failed: {exc}")

    def stop_backend(self) -> None:
        if self.controller:
            self.controller.stop()
        self.controller = None
        self.worker_status = "stopped"

    def validate_target(self, target: CodexTarget) -> WakeResult:
        if target.target_type != "codex-app-server" or target.repo_path != self.target.repo_path:
            return WakeResult(False, "codex-app-server target mismatch")
        if self.worker_id == self.dev_session_id or not self.worker_id:
            return WakeResult(False, "App Server worker equals DEV or is missing")
        if not self.controller or not self.controller.alive:
            return WakeResult(False, "owned App Server is not healthy")
        if self.worker_status not in {"idle", "notLoaded"}:
            return WakeResult(False, f"worker is not safely wakeable: {self.worker_status}")
        return WakeResult(True, "owned App Server and worker validated")

    def wake(self, lease: WakeLease, instruction: str) -> WakeResult:
        valid = self.validate_target(self.target)
        if not valid.accepted or not self.controller:
            return valid
        if lease.worker_id != self.worker_id:
            return WakeResult(False, "lease worker does not match App Server binding")
        try:
            observed = self.controller.find_worker(self.worker_id)
            if observed is None:
                try:
                    observed = self.controller.read_worker(self.worker_id)
                except AppServerError:
                    # A freshly provisioned thread/start response is authoritative
                    # on this exact owned connection when read/list are delayed.
                    if self.controller.worker_id != self.worker_id or self.worker_status not in {"idle", "notLoaded"}:
                        return WakeResult(False, "worker status is not safe: missing")
            elif observed.status not in {"idle", "notLoaded"}:
                return WakeResult(False, f"worker status is not safe: {observed.status if observed else 'missing'}")
            if observed and observed.source_kind and observed.source_kind != "appServer":
                return WakeResult(False, f"worker source is not App Server-owned: {observed.source_kind}")
            turn = self.controller.start_turn(self.worker_id, instruction, [self.target.repo_path, self.local_project_storage])
            self.last_turn_id = turn.turn_id
            self.last_turn_status = turn.status
            self.last_turn_started_at = time.monotonic()
            self.worker_status = "active"
            return WakeResult(True, "App Server turn started", process_id=self.controller.pid, turn_id=turn.turn_id)
        except Exception as exc:
            self.last_error = str(exc)
            return WakeResult(False, f"App Server turn start failed: {exc}")

    def turn_completed(self, lease: WakeLease) -> bool:
        turn_id = str(lease.turn_id or self.last_turn_id or "")
        if not self.controller or not turn_id or lease.worker_id != self.worker_id:
            return False
        try:
            event = self.controller.consume_terminal_event(self.worker_id, turn_id) if hasattr(self.controller, "consume_terminal_event") else None
            if event is None:
                for candidate in self.controller.poll_notifications():
                    if candidate.get("method") == "turn/completed":
                        params = candidate.get("params", {})
                        turn = params.get("turn", {})
                        if params.get("threadId") == self.worker_id and turn.get("id") == turn_id:
                            event = candidate
                            break
            if event is not None:
                params = event.get("params", {})
                turn = params.get("turn", {})
                if params.get("threadId") == self.worker_id and turn.get("id") == turn_id:
                    self.last_turn_id = turn_id
                    self.last_turn_status = str(turn.get("status", "completed"))
                    self.worker_status = "idle"
            return self.last_turn_status in {"completed", "interrupted", "failed"} and self.worker_status == "idle"
        except AppServerError as exc:
            self.last_error = str(exc)
            return False

    def transport_quiescent(self, lease: WakeLease) -> bool:
        """Confirm the exact owned turn is terminal and the worker is idle."""
        if not self.controller or lease.worker_id != self.worker_id:
            return False
        return bool(
            self.controller.alive
            and self.worker_status == "idle"
            and self.last_turn_status in {"completed", "interrupted", "failed"}
            and str(lease.turn_id or self.last_turn_id or "") == str(self.last_turn_id or "")
        )

    def interrupt_turn_once(self, lease: WakeLease) -> bool:
        """Request bounded cleanup for one exact post-handoff turn at most once."""
        if not self.controller or lease.worker_id != self.worker_id or not lease.turn_id:
            return False
        key = (self.worker_id, str(lease.turn_id))
        if key in self.interrupt_attempts:
            return False
        started = self.last_turn_started_at
        if started is None:
            started = time.monotonic() - self.interrupt_grace_seconds
        if time.monotonic() - started < self.interrupt_grace_seconds:
            return False
        try:
            if hasattr(self.controller, "has_terminal_event") and self.controller.has_terminal_event(self.worker_id, str(lease.turn_id)):
                return False
            self.controller.interrupt_turn(self.worker_id, str(lease.turn_id))
            self.interrupt_attempts.add(key)
            self.last_error = "post-handoff turn interrupt requested"
            return True
        except AppServerError as exc:
            self.last_error = str(exc)
            return False
