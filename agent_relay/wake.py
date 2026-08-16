from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
import subprocess
from typing import Protocol
from uuid import UUID, uuid4


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


class LeaseStatus(StrEnum):
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


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
    status: LeaseStatus = LeaseStatus.ACTIVE

    @classmethod
    def create(cls, project_id: str, run_id: str, step: int, path: Path) -> "WakeLease":
        return cls(str(uuid4()), project_id, run_id, step, path.resolve(), utcnow())


@dataclass(frozen=True)
class WakeResult:
    accepted: bool
    detail: str = ""
    completed: bool = False
    process_id: int | None = None


class WakeAdapter(Protocol):
    def wake(self, lease: WakeLease, wake_instruction: str) -> WakeResult: ...
    def validate_target(self, target: CodexTarget) -> WakeResult: ...


def wake_instruction(lease: WakeLease) -> str:
    return f"""AGENTRELAY_WAKE/1\n\nProject: {lease.project_id}\nRun: {lease.run_id}\nStep: {lease.step:04d}\nLease: {lease.lease_id}\nStaged instruction: {lease.staged_instruction_path}\n\nRead the authoritative staged instruction and execute exactly one work lease. Test, inspect the diff, commit and push applicable changes, then provide a completion report and deterministic completion signal before yielding. Do not poll Gmail, wait for ChatGPT, retry autonomously, or begin another workflow step without a new supervisor wake.\n"""


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
            process = subprocess.Popen([self.command, "exec", "resume", self.target.target_id, instruction], cwd=self.target.repo_path, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, shell=False, creationflags=flags, startupinfo=startup)
        except OSError as exc:
            log.close()
            return WakeResult(False, f"Codex CLI launch failed: {exc}")
        log.close()
        return WakeResult(True, "Codex CLI process launched", completed=False, process_id=process.pid)
