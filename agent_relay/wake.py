from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from uuid import uuid4


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


class WakeAdapter(Protocol):
    def wake(self, lease: WakeLease, wake_instruction: str) -> WakeResult: ...


def wake_instruction(lease: WakeLease) -> str:
    return f"""AGENTRELAY_WAKE/1\n\nProject: {lease.project_id}\nRun: {lease.run_id}\nStep: {lease.step:04d}\nLease: {lease.lease_id}\n\nA validated instruction is staged at:\n{lease.staged_instruction_path}\n\nRead the authoritative staged instruction and execute exactly one work lease. Do not poll Gmail or begin another workflow step without a new supervisor wake.\n"""


class MockWakeAdapter:
    """Certification-only adapter: it never invokes Codex or external UI automation."""
    def __init__(self, succeed: bool = True):
        self.succeed = succeed
        self.calls: list[tuple[WakeLease, str]] = []

    def wake(self, lease: WakeLease, wake_instruction: str) -> WakeResult:
        self.calls.append((lease, wake_instruction))
        return WakeResult(self.succeed, "mock accepted" if self.succeed else "mock configured to fail")
