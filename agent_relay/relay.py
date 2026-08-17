from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
from typing import Callable, Protocol
from uuid import uuid4

from .gmail import GmailGateway, GmailMessage
from .protocol import Disposition, ProtocolEnvelope, ProtocolError, parse_envelope
from .storage import Ledger, StateStore, stage_instruction, now


class WorkerLauncher(Protocol):
    def launch(self, *, staged_path: Path, envelope: ProtocolEnvelope, content_hash: str) -> dict: ...


def message_hash(message: GmailMessage) -> str:
    data = message.body.encode("utf-8") + b"\0" + b"\0".join(a.data for a in message.attachments)
    return hashlib.sha256(data).hexdigest()


def _owner_live(owner: dict | None) -> bool:
    if not owner or not isinstance(owner.get("pid"), int):
        return False
    pid = owner["pid"]
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False
    # QueryFullProcessImageNameW avoids the Windows os.kill false positive.
    try:
        import ctypes
        from ctypes import wintypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            size = wintypes.DWORD(32768)
            buf = ctypes.create_unicode_buffer(size.value)
            ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
            if not ok:
                return False
            expected = str(owner.get("exe") or "").lower()
            return not expected or Path(buf.value).name.lower() == Path(expected).name.lower()
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except (AttributeError, OSError):
        return False


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
        state = self.store.load()
        if state.get("stop_requested"):
            self.ledger.append("poll_skipped_stop")
            return PollResult("stopped")
        owner = state.get("active_worker")
        if owner and _owner_live(owner):
            self.ledger.append("poll_skipped_active_worker", worker_id=owner.get("worker_id"))
            return PollResult("busy")
        if owner:
            state["active_worker"] = None
            state["mode"] = "IDLE"
            self.store.save(state)
            self.ledger.append("stale_owner_cleared", worker_id=owner.get("worker_id"), pid=owner.get("pid"))

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
                worker = self.launcher.launch(staged_path=staged, envelope=env, content_hash=content_hash)
            except Exception as exc:
                state["last_error"] = f"launch failed: {type(exc).__name__}"
                self.store.save(state)
                self.ledger.append("launch_failed", message_id=message_id, reason=type(exc).__name__)
                return PollResult("launch_failed", message_id)
            consumed.add(message_id)
            state.update({"current_run": env.run_id, "expected_step": step + 1, "expected_parent": step, "mode": "BUSY", "active_worker": worker, "last_error": None})
            state["consumed_message_ids"] = sorted(consumed)
            state["logical_hashes"][logical_key] = content_hash
            self.store.save(state)
            self.ledger.append("worker_launched", message_id=message_id, step=step, worker_id=worker.get("worker_id"))
            return PollResult("launched", message_id, staged, worker)
        self.ledger.append("poll_complete", fetched=len(ids))
        return PollResult("idle")


class NoopWorkerLauncher:
    """Small launcher used by `Test Wake` and deterministic integration tests."""

    def __init__(self, pid: int | None = None):
        self.pid = os.getpid() if pid is None else pid
        self.calls: list[dict] = []

    def launch(self, *, staged_path: Path, envelope: ProtocolEnvelope, content_hash: str) -> dict:
        owner = {"worker_id": str(uuid4()), "pid": self.pid, "project_id": envelope.project_id, "run_id": envelope.run_id, "step": envelope.step, "started_at": now(), "exe": Path(os.sys.executable).name}
        self.calls.append({"staged_path": staged_path, "envelope": envelope, "content_hash": content_hash, "owner": owner})
        return owner
