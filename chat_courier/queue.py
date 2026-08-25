"""Durable FIFO arbitration for the single Courier-owned ChatGPT profile."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import secrets
import time
from typing import Any, Callable

from .liveness import process_alive
from .locking import RuntimeLock
from .model import Request, atomic_json, runtime_root


class QueueIntegrityError(RuntimeError):
    pass


def _now() -> float:
    return time.time()


def _alive(pid: int) -> bool:
    return process_alive(pid)


@dataclass(frozen=True)
class QueueStatus:
    state: str
    ticket: str | None = None
    position: int | None = None
    ahead: int = 0
    waited_seconds: int = 0
    estimated_wait_upper_bound_seconds: int = 0
    current_owner: dict[str, Any] | None = None
    detail: str | None = None


class CourierQueue:
    """FIFO state; only an active queue entry may open the shared browser."""
    def __init__(self, request: Request, *, root: Path | None = None,
                 now: Callable[[], float] = _now, alive: Callable[[int], bool] = _alive,
                 pid: int | None = None):
        self.request = request
        self.root = root or runtime_root()
        self.now, self.alive, self.pid = now, alive, os.getpid() if pid is None else pid
        self.ticket: str | None = None

    @property
    def path(self) -> Path:
        return self.root / "queue.json"

    def _lock(self) -> RuntimeLock:
        return RuntimeLock("ChatCourier-QueueState", self.root)

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "next_sequence": 1, "entries": []}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise QueueIntegrityError(f"invalid durable Courier queue: {self.path}") from exc
        if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("entries"), list):
            raise QueueIntegrityError(f"invalid durable Courier queue schema: {self.path}")
        return value

    def _save(self, value: dict[str, Any]) -> None:
        atomic_json(self.path, value)

    @staticmethod
    def _ordered(value: dict[str, Any]) -> list[dict[str, Any]]:
        return sorted(value["entries"], key=lambda entry: int(entry.get("sequence", 0)))

    def _prune_dead_queued(self, value: dict[str, Any]) -> bool:
        # Only a process which has never acquired the browser can be removed
        # automatically.  Active work is a recovery boundary, not stale junk.
        before = len(value["entries"])
        value["entries"] = [entry for entry in value["entries"] if not (
            entry.get("state") == "queued" and not self.alive(int(entry.get("pid", 0)))
        )]
        return len(value["entries"]) != before

    def _queue_waited_seconds(self, entry: dict[str, Any]) -> float:
        """Return only time spent waiting for the browser, never active age."""
        waited = float(entry.get("queue_wait_accumulated_seconds", 0))
        if entry.get("state") == "queued":
            started = float(entry.get("queue_wait_started_at", entry.get("enqueued_at", self.now())))
            waited += max(0.0, self.now() - started)
        return waited

    def _stop_waiting(self, entry: dict[str, Any]) -> None:
        entry["queue_wait_accumulated_seconds"] = self._queue_waited_seconds(entry)
        entry.pop("queue_wait_started_at", None)

    def join(self, *, allow_active_recovery: bool = False) -> QueueStatus:
        with self._lock():
            value = self._load(); self._prune_dead_queued(value)
            matching = [entry for entry in value["entries"] if entry.get("project_id") == self.request.project_id and entry.get("request_id") == self.request.request_id]
            if matching:
                entry = matching[0]
                if entry.get("fingerprint") != self.request.fingerprint:
                    raise QueueIntegrityError("a live queue entry has this project/request ID with different content")
                self.ticket = str(entry["ticket"])
                if self.alive(int(entry.get("pid", 0))) and int(entry.get("pid", 0)) != self.pid:
                    self._save(value)
                    return self._status(value, entry, "duplicate_runner")
                if entry.get("state") == "active" and not self.alive(int(entry.get("pid", 0))):
                    # The original request may recover only if it is re-run by
                    # the same immutable request identity.
                    if not allow_active_recovery:
                        self._save(value)
                        return self._status(value, entry, "recovery_required", current_owner=entry)
                    # The request already owned the browser turn before it
                    # died.  Its historic active age is not FIFO wait time.
                    entry["pid"] = self.pid; entry["heartbeat_at"] = self.now(); entry["state"] = "queued"
                    entry["queue_wait_started_at"] = self.now()
                    self._save(value)
                    return self._status(value, entry, "recovery_rejoined")
                else:
                    entry["pid"] = self.pid; entry["heartbeat_at"] = self.now()
                self._save(value)
                return self._status(value, entry, "joined")
            sequence = int(value.get("next_sequence", 1)); value["next_sequence"] = sequence + 1
            entry = {
                "ticket": secrets.token_urlsafe(12), "sequence": sequence, "state": "queued",
                "project_id": self.request.project_id, "request_id": self.request.request_id,
                "fingerprint": self.request.fingerprint, "request_directory": str(self.request.directory),
                "pid": self.pid, "enqueued_at": self.now(), "heartbeat_at": self.now(),
                "queue_wait_started_at": self.now(), "queue_wait_accumulated_seconds": 0,
                "queue_wait_seconds": self.request.queue_wait_seconds,
                "workflow_window_seconds": self.request.workflow_window_seconds,
            }
            value["entries"].append(entry); self.ticket = str(entry["ticket"]); self._save(value)
            return self._status(value, entry, "joined")

    def observe(self) -> QueueStatus:
        """Report queue occupancy without creating a ticket or opening Chrome."""
        with self._lock():
            value = self._load(); changed = self._prune_dead_queued(value)
            if changed:
                self._save(value)
            ordered = self._ordered(value)
            if not ordered:
                return QueueStatus("empty")
            head = ordered[0]
            return QueueStatus(
                "waiting", position=1, ahead=0,
                estimated_wait_upper_bound_seconds=sum(int(item.get("workflow_window_seconds", 0)) for item in ordered),
                current_owner={
                    "project_id": head.get("project_id"), "request_id": head.get("request_id"),
                    "state": head.get("state"), "workflow_window_seconds": head.get("workflow_window_seconds"),
                },
            )

    def poll(self) -> QueueStatus:
        if not self.ticket:
            raise QueueIntegrityError("queue ticket was not created")
        with self._lock():
            value = self._load(); self._prune_dead_queued(value)
            entry = next((item for item in value["entries"] if item.get("ticket") == self.ticket), None)
            if entry is None:
                raise QueueIntegrityError("Courier queue ticket disappeared")
            if entry.get("pid") != self.pid:
                return self._status(value, entry, "duplicate_runner")
            entry["heartbeat_at"] = self.now()
            waited = self._queue_waited_seconds(entry)
            if entry.get("state") == "queued" and waited >= int(entry["queue_wait_seconds"]):
                value["entries"].remove(entry); self._save(value)
                return self._status(value, entry, "timeout")
            ordered = self._ordered(value)
            head = ordered[0] if ordered else None
            if head is entry and entry.get("state") == "queued":
                self._stop_waiting(entry)
                entry["state"] = "active"; entry["execution_started_at"] = self.now(); self._save(value)
                return self._status(value, entry, "turn_acquired")
            if head is not None and head.get("state") == "active" and not self.alive(int(head.get("pid", 0))):
                self._save(value)
                return self._status(value, entry, "recovery_required", current_owner=head)
            self._save(value)
            return self._status(value, entry, "waiting", current_owner=head if head and head is not entry else None)

    def complete(self) -> None:
        if not self.ticket:
            return
        with self._lock():
            value = self._load()
            value["entries"] = [entry for entry in value["entries"] if entry.get("ticket") != self.ticket]
            self._save(value)

    def mark_recovery_required(self, detail: str) -> None:
        if not self.ticket:
            return
        with self._lock():
            value = self._load()
            entry = next((item for item in value["entries"] if item.get("ticket") == self.ticket), None)
            if entry is not None:
                entry["state"] = "active"; entry["heartbeat_at"] = self.now(); entry["recovery_detail"] = detail
                self._save(value)

    def _status(self, value: dict[str, Any], entry: dict[str, Any], state: str,
                current_owner: dict[str, Any] | None = None) -> QueueStatus:
        ordered = self._ordered(value); position = next((index + 1 for index, item in enumerate(ordered) if item is entry), None)
        now = self.now(); waited = max(0, int(self._queue_waited_seconds(entry)))
        ahead_entries = ordered[:max(0, (position or 1) - 1)]
        estimate = sum(int(item.get("workflow_window_seconds", 0)) for item in ahead_entries)
        if current_owner is not None and current_owner.get("execution_started_at"):
            elapsed = max(0, int(now - float(current_owner["execution_started_at"])))
            estimate = max(0, int(current_owner.get("workflow_window_seconds", 0)) - elapsed) + sum(
                int(item.get("workflow_window_seconds", 0)) for item in ahead_entries if item is not current_owner
            )
        owner = None if current_owner is None else {
            "project_id": current_owner.get("project_id"), "request_id": current_owner.get("request_id"),
            "state": current_owner.get("state"), "workflow_window_seconds": current_owner.get("workflow_window_seconds"),
        }
        return QueueStatus(state, str(entry.get("ticket")), position, len(ahead_entries), waited, estimate, owner)
