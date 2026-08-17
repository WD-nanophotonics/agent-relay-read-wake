from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
from typing import Callable, Protocol
from uuid import uuid4
from contextlib import contextmanager

from .gmail import GmailGateway, GmailMessage
from .protocol import Disposition, ProtocolEnvelope, ProtocolError, parse_envelope
from .storage import Ledger, StateStore, stage_instruction, now
from .ownership import exact_owner_live
from .obligations import recover_pending_handoffs_once


class WorkerLauncher(Protocol):
    def launch(self, *, staged_path: Path, envelope: ProtocolEnvelope, content_hash: str, message_id: str) -> dict: ...


def message_hash(message: GmailMessage) -> str:
    data = message.body.encode("utf-8") + b"\0" + b"\0".join(a.data for a in message.attachments)
    return hashlib.sha256(data).hexdigest()


_owner_live = exact_owner_live


@contextmanager
def poll_transaction_lock(root: Path):
    """Short-lived cross-process mutex for one poll-once transaction."""
    path = root / "poll-once.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    acquired = False
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"pid={os.getpid()}\nexe={Path(os.sys.executable).name}\n".encode())
        os.close(fd)
        acquired = True
    except FileExistsError:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            owner = {"pid": int(next(item.split("=", 1)[1] for item in lines if item.startswith("pid="))), "exe": next(item.split("=", 1)[1] for item in lines if item.startswith("exe="))}
            if not exact_owner_live(owner):
                path.unlink(missing_ok=True)
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, f"pid={os.getpid()}\nexe={Path(os.sys.executable).name}\n".encode()); os.close(fd); acquired = True
        except (OSError, StopIteration, ValueError, FileExistsError):
            acquired = False
    try:
        yield acquired
    finally:
        if acquired:
            path.unlink(missing_ok=True)


@dataclass(frozen=True)
class PollResult:
    action: str
    message_id: str | None = None
    staged_path: Path | None = None
    worker: dict | None = None


class Relay:
    """One deterministic Gmail fetch cycle. It never sleeps or owns a thread."""

    def __init__(self, config, gmail: GmailGateway, launcher: WorkerLauncher):
        self.config = config
        self.gmail = gmail
        self.launcher = launcher
        self.store = StateStore(config.local_project_storage)
        self.ledger = Ledger(config.local_project_storage)

    def poll_once(self) -> PollResult:
        with poll_transaction_lock(self.config.local_project_storage) as acquired:
            if not acquired:
                self.ledger.append("poll_skipped_lock")
                return PollResult("busy")
            return self._poll_once()

    def _poll_once(self) -> PollResult:
        recovered = recover_pending_handoffs_once(self.config)
        if any(item.get("state") != "VERIFIED" for item in recovered):
            self.ledger.append("poll_blocked_terminal_handoff", count=len(recovered))
            return PollResult("handoff_pending")
        state = self.store.load()
        if state.get("stop_requested"):
            self.ledger.append("poll_skipped_stop")
            return PollResult("stopped")
        owner = state.get("active_worker") or state.get("pending_worker")
        if owner and _owner_live(owner):
            self.ledger.append("poll_skipped_owned_worker", worker_id=owner.get("worker_id"), claimed=bool(state.get("active_worker")))
            return PollResult("busy")
        if owner:
            state["pending_worker"] = None
            state["mode"] = "IDLE"
            self.store.save(state)
            self.ledger.append("stale_pending_owner_cleared", worker_id=owner.get("worker_id"), pid=owner.get("pid"))

        ids = list(self.gmail.list_messages())
        state = self.store.load()
        consumed = set(state["consumed_message_ids"])
        expected = int(state.get("expected_step", 1))
        current_run = state.get("current_run")
        expected_parent = int(state.get("expected_parent", expected - 1))
        candidates: list[tuple[int, str, GmailMessage, ProtocolEnvelope, str]] = []
        for message_id in sorted(set(ids)):
            if message_id in consumed:
                continue
            try:
                msg = self.gmail.fetch_message(message_id)
                env = parse_envelope(msg.body)
            except (ProtocolError, OSError, ValueError) as exc:
                self.ledger.append("message_ignored", message_id=message_id, reason=type(exc).__name__)
                continue
            if env.channel_id != self.config.channel_id or env.project_id != self.config.project_id:
                self.ledger.append("message_ignored", message_id=message_id, reason="binding_mismatch")
                continue
            if current_run and env.run_id != current_run:
                self.ledger.append("message_ignored", message_id=message_id, reason="run_mismatch")
                continue
            candidates.append((env.step, message_id, msg, env, message_hash(msg)))
        candidates.sort(key=lambda item: (item[0], item[1]))
        # A logical step is identified by the validated envelope identity, not
        # by Gmail arrival order.  Two different body hashes for that identity
        # are preserved as a fail-closed conflict; neither message is
        # consumed, staged, or launched.
        grouped: dict[tuple[str, int, int, str], list[tuple[int, str, GmailMessage, ProtocolEnvelope, str]]] = {}
        for candidate in candidates:
            env = candidate[3]
            grouped.setdefault((env.channel_id, env.step, env.parent, env.project_id), []).append(candidate)
        for logical_identity, group in grouped.items():
            hashes = {item[4] for item in group}
            if len(hashes) > 1:
                details = [{"message_id": item[1], "body_hash": item[4]} for item in group]
                state["last_error"] = f"conflicting logical-step content: {logical_identity}"
                self.store.save(state)
                self.ledger.append("conflict_fail_closed", logical_identity=logical_identity, candidates=details)
                return PollResult("conflict", group[0][1])
        for step, message_id, msg, env, content_hash in candidates:
            if step < expected:
                self.ledger.append("old_message_ignored", message_id=message_id, step=step)
                continue
            if step > expected:
                self.ledger.append("future_message_deferred", message_id=message_id, step=step, expected_step=expected)
                continue
            if env.parent != expected_parent:
                self.ledger.append("ordering_rejected", message_id=message_id, step=step, parent=env.parent, expected_parent=expected_parent)
                continue
            logical_key = f"{env.run_id}:{env.step:04d}"
            prior_hash = state["logical_hashes"].get(logical_key)
            if prior_hash and prior_hash != content_hash:
                state["last_error"] = "conflicting logical-step content"
                self.store.save(state)
                self.ledger.append("conflict_fail_closed", message_id=message_id, logical_step=logical_key)
                return PollResult("conflict", message_id)
            if env.disposition is Disposition.HUMAN_REQUIRED:
                consumed.add(message_id)
                state["consumed_message_ids"] = sorted(consumed)
                state["last_error"] = "human-required instruction"
                self.store.save(state)
                self.ledger.append("human_required_rejected", message_id=message_id)
                return PollResult("human_required", message_id)
            if env.disposition is Disposition.NO_ACTION:
                consumed.add(message_id)
                state.update({"current_run": env.run_id, "expected_step": step + 1, "expected_parent": step, "mode": "IDLE", "last_error": None})
                state["consumed_message_ids"] = sorted(consumed)
                state["logical_hashes"][logical_key] = content_hash
                self.store.save(state)
                self.ledger.append("no_action_advanced", message_id=message_id, step=step)
                return PollResult("advanced", message_id)
            try:
                staged = stage_instruction(self.config.local_project_storage, msg, env)
                worker = self.launcher.launch(staged_path=staged, envelope=env, content_hash=content_hash, message_id=message_id)
            except Exception as exc:
                state["last_error"] = f"launch failed: {type(exc).__name__}"
                self.store.save(state)
                self.ledger.append("launch_failed", message_id=message_id, reason=type(exc).__name__)
                return PollResult("launch_failed", message_id)
            # Launch is only a pending ownership barrier. The Worker atomically
            # acknowledges claim before this logical step is consumed.
            worker.update({"message_id": message_id, "content_hash": content_hash, "staged_path": str(staged)})
            state.update({"mode": "BUSY", "pending_worker": worker, "last_error": None})
            self.store.save(state)
            self.ledger.append("worker_process_created", message_id=message_id, step=step, worker_id=worker.get("worker_id"), pid=worker.get("pid"))
            self.ledger.append("worker_launch_pending_claim", message_id=message_id, step=step, worker_id=worker.get("worker_id"))
            return PollResult("worker_process_created", message_id, staged, worker)
        self.ledger.append("poll_complete", fetched=len(ids))
        return PollResult("idle")


class NoopWorkerLauncher:
    """Small launcher used by `Test Wake` and deterministic integration tests."""

    def __init__(self, pid: int | None = None):
        self.pid = os.getpid() if pid is None else pid
        self.calls: list[dict] = []

    def launch(self, *, staged_path: Path, envelope: ProtocolEnvelope, content_hash: str, message_id: str) -> dict:
        owner = {"worker_id": str(uuid4()), "pid": self.pid, "project_id": envelope.project_id, "run_id": envelope.run_id, "step": envelope.step, "parent": envelope.parent, "started_at": now(), "exe": Path(os.sys.executable).name}
        self.calls.append({"staged_path": staged_path, "envelope": envelope, "content_hash": content_hash, "message_id": message_id, "owner": owner})
        return owner
