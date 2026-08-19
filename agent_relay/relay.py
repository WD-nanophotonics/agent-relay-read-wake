from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Protocol
from uuid import uuid4
from contextlib import contextmanager

from .gmail import GmailGateway, GmailMessage
from .protocol import (AuditAction, AuditDecision, Disposition, MessageKind,
                       ProtocolEnvelope, ProtocolError, parse_envelope,
                       parse_json_attachment, validate_decision_document)
from .storage import Ledger, StateStore, stage_instruction, now
from .ownership import exact_owner_live
from .obligations import recover_pending_handoffs_once


class WorkerLauncher(Protocol):
    def launch(self, *, staged_path: Path, envelope: ProtocolEnvelope, content_hash: str, message_id: str, worker_id: str | None = None) -> dict: ...


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
                os.write(fd, f"pid={os.getpid()}\nexe={Path(os.sys.executable).name}\n".encode())
                os.close(fd)
                acquired = True
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
    decision_id: str | None = None
    work_order_id: str | None = None


@dataclass(frozen=True)
class Candidate:
    step: int
    message_id: str
    message: GmailMessage
    envelope: ProtocolEnvelope
    content_hash: str
    decision: AuditDecision | None = None


def _attachments(message: GmailMessage, filename: str) -> list[bytes]:
    return [item.data for item in message.attachments if Path(item.filename.replace("\\", "/")).name == filename]


def _parse_candidate(message: GmailMessage, envelope: ProtocolEnvelope) -> AuditDecision | None:
    if not envelope.is_v2:
        return None
    if envelope.message_kind is not MessageKind.AUDIT_DECISION:
        return None
    decision_files = _attachments(message, "decision.json")
    if len(decision_files) != 1:
        raise ProtocolError("AUDIT_DECISION requires exactly one decision.json attachment")
    work_order_files = _attachments(message, "work_order.md")
    document = parse_json_attachment(decision_files[0])
    decision = validate_decision_document(document, envelope)
    if decision.action is AuditAction.EXECUTE and len(work_order_files) != 1:
        raise ProtocolError("EXECUTE AUDIT_DECISION requires exactly one work_order.md attachment")
    if decision.action is not AuditAction.EXECUTE and work_order_files:
        raise ProtocolError("non-execute AUDIT_DECISION must not include work_order.md")
    if envelope.work_order_id != decision.work_order_id:
        raise ProtocolError("envelope work_order_id does not match decision.json")
    return decision


class Relay:
    """One deterministic Gmail fetch cycle with explicit control-plane checks."""

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
        if state.get("stop_requested") or state.get("mode") == "STOPPED":
            self.ledger.append("poll_skipped_stop")
            return PollResult("stopped")
        if state.get("dispatch_intent") and not state.get("pending_worker") and not state.get("active_worker"):
            state.update({"mode": "STOPPED", "last_error": "dispatch outcome is uncertain; human recovery required"})
            self.store.save(state)
            self.ledger.append("dispatch_uncertain_fail_closed", intent=state["dispatch_intent"])
            return PollResult("dispatch_uncertain")
        owner = state.get("active_worker") or state.get("pending_worker")
        if owner and _owner_live(owner):
            self.ledger.append("poll_skipped_owned_worker", worker_id=owner.get("worker_id"), claimed=bool(state.get("active_worker")))
            return PollResult("busy")
        if owner:
            state["active_worker"] = None
            state["pending_worker"] = None
            state["mode"] = "AWAITING_AUDIT" if owner.get("work_order_id") else "IDLE"
            state["last_error"] = "stale worker owner cleared"
            self.store.save(state)
            self.ledger.append("stale_pending_owner_cleared", worker_id=owner.get("worker_id"), pid=owner.get("pid"))

        ids = list(self.gmail.list_messages())
        state = self.store.load()
        consumed = set(state["consumed_message_ids"])
        expected = int(state.get("expected_step", 1))
        current_run = state.get("current_run")
        expected_parent = int(state.get("expected_parent", expected - 1))
        candidates: list[Candidate] = []
        for message_id in sorted(set(ids)):
            if message_id in consumed:
                continue
            try:
                msg = self.gmail.fetch_message(message_id)
                env = parse_envelope(msg.body)
                decision = _parse_candidate(msg, env)
            except (ProtocolError, OSError, ValueError) as exc:
                self.ledger.append("message_rejected", message_id=message_id, reason=type(exc).__name__, detail=str(exc))
                continue
            if env.channel_id != self.config.channel_id or env.project_id != self.config.project_id:
                self.ledger.append("message_ignored", message_id=message_id, reason="binding_mismatch")
                continue
            if current_run and env.run_id != current_run:
                self.ledger.append("message_ignored", message_id=message_id, reason="run_mismatch")
                continue
            if env.is_v2 and env.message_kind is not MessageKind.AUDIT_DECISION:
                self.ledger.append("message_ignored", message_id=message_id, reason="non_authoritative_v2_message")
                continue
            candidates.append(Candidate(env.step, message_id, msg, env, message_hash(msg), decision))
        candidates.sort(key=lambda item: (item.step, item.message_id))
        grouped: dict[tuple[str, int, int, str, str], list[Candidate]] = {}
        for candidate in candidates:
            env = candidate.envelope
            grouped.setdefault((env.channel_id, env.step, env.parent, env.project_id, env.decision_id), []).append(candidate)
        for logical_identity, group in grouped.items():
            hashes = {item.content_hash for item in group}
            if len(hashes) > 1:
                state["last_error"] = f"conflicting logical-step content: {logical_identity}"
                self.store.save(state)
                self.ledger.append("conflict_fail_closed", logical_identity=logical_identity, candidates=[{"message_id": item.message_id, "body_hash": item.content_hash} for item in group])
                return PollResult("conflict", group[0].message_id, decision_id=group[0].envelope.decision_id or None)
        for candidate in candidates:
            env = candidate.envelope
            if env.is_v2:
                duplicate = self._check_duplicate(state, candidate)
                if duplicate is not None:
                    return duplicate
            if candidate.step < expected:
                self.ledger.append("old_message_ignored", message_id=candidate.message_id, step=candidate.step)
                continue
            if candidate.step > expected:
                self.ledger.append("future_message_deferred", message_id=candidate.message_id, step=candidate.step, expected_step=expected)
                continue
            if env.parent != expected_parent:
                self.ledger.append("ordering_rejected", message_id=candidate.message_id, step=candidate.step, parent=env.parent, expected_parent=expected_parent)
                continue
            logical_key = f"{env.run_id}:{env.step:04d}"
            prior_hash = state["logical_hashes"].get(logical_key)
            if prior_hash and prior_hash != candidate.content_hash:
                state["last_error"] = "conflicting logical-step content"
                self.store.save(state)
                self.ledger.append("conflict_fail_closed", message_id=candidate.message_id, logical_step=logical_key)
                return PollResult("conflict", candidate.message_id)
            if env.is_v2:
                if candidate.decision is None:
                    self.ledger.append("decision_rejected", message_id=candidate.message_id, reason="missing_decision")
                    continue
                if candidate.decision.action is AuditAction.HUMAN_REQUIRED:
                    return self._consume_terminal_decision(state, candidate, "human_required")
                if candidate.decision.action is AuditAction.NO_ACTION:
                    return self._consume_terminal_decision(state, candidate, "advanced")
                return self._dispatch_candidate(state, candidate)
            if env.disposition is Disposition.HUMAN_REQUIRED:
                consumed.add(candidate.message_id)
                state["consumed_message_ids"] = sorted(consumed)
                state["last_error"] = "human-required instruction"
                state["mode"] = "STOPPED"
                self.store.save(state)
                self.ledger.append("human_required_rejected", message_id=candidate.message_id)
                return PollResult("human_required", candidate.message_id)
            if env.disposition is Disposition.NO_ACTION:
                consumed.add(candidate.message_id)
                state.update({"current_run": env.run_id, "expected_step": candidate.step + 1, "expected_parent": candidate.step, "mode": "IDLE", "last_error": None})
                state["consumed_message_ids"] = sorted(consumed)
                state["logical_hashes"][logical_key] = candidate.content_hash
                self.store.save(state)
                self.ledger.append("no_action_advanced", message_id=candidate.message_id, step=candidate.step)
                return PollResult("advanced", candidate.message_id)
            return self._dispatch_legacy(state, candidate)
        self.ledger.append("poll_complete", fetched=len(ids), state=state.get("mode"))
        return PollResult("idle")

    def _check_duplicate(self, state: dict, candidate: Candidate) -> PollResult | None:
        decision_id = candidate.envelope.decision_id
        record = state.get("decisions", {}).get(decision_id)
        if record:
            if record.get("decision_hash") != candidate.content_hash:
                state["mode"] = "STOPPED"
                state["last_error"] = "decision_id reused with different content"
                self.store.save(state)
                self.ledger.append("decision_conflict_fail_closed", decision_id=decision_id)
                return PollResult("conflict", candidate.message_id, decision_id=decision_id)
            self.ledger.append("decision_duplicate", decision_id=decision_id, message_id=candidate.message_id)
            return PollResult("duplicate", candidate.message_id, decision_id=decision_id, work_order_id=candidate.envelope.work_order_id or None)
        work_order_id = candidate.envelope.work_order_id
        if work_order_id and work_order_id in state.get("work_orders", {}):
            existing = state["work_orders"][work_order_id]
            if existing.get("decision_hash") != candidate.content_hash:
                state["mode"] = "STOPPED"
                state["last_error"] = "work_order_id reused with different content"
                self.store.save(state)
                self.ledger.append("work_order_conflict_fail_closed", work_order_id=work_order_id)
                return PollResult("conflict", candidate.message_id, work_order_id=work_order_id)
            self.ledger.append("work_order_duplicate", work_order_id=work_order_id, message_id=candidate.message_id)
            return PollResult("duplicate", candidate.message_id, decision_id=candidate.envelope.decision_id, work_order_id=work_order_id)
        return None

    def _consume_terminal_decision(self, state: dict, candidate: Candidate, action: str) -> PollResult:
        env = candidate.envelope
        decision = candidate.decision
        assert decision is not None
        state["consumed_message_ids"] = sorted(set(state["consumed_message_ids"]) | {candidate.message_id})
        state["logical_hashes"][f"{env.run_id}:{env.step:04d}"] = candidate.content_hash
        state["decisions"][decision.decision_id] = {"decision_hash": candidate.content_hash, "state": "CONSUMED", "work_order_id": decision.work_order_id, "message_id": candidate.message_id}
        state["current_run"] = env.run_id
        state["expected_step"] = env.step + 1
        state["expected_parent"] = env.step
        state["mode"] = "STOPPED" if action == "human_required" else "AWAITING_AUDIT"
        state["last_error"] = "human-required decision" if action == "human_required" else None
        self.store.save(state)
        self.ledger.append("audit_decision_consumed", decision_id=decision.decision_id, action=action, message_id=candidate.message_id)
        return PollResult(action, candidate.message_id, decision_id=decision.decision_id)

    def _dispatch_candidate(self, state: dict, candidate: Candidate) -> PollResult:
        env = candidate.envelope
        decision = candidate.decision
        assert decision is not None and decision.action is AuditAction.EXECUTE
        worker_id = str(uuid4())
        work_order_bytes = _attachments(candidate.message, "work_order.md")[0]
        work_order_hash = hashlib.sha256(work_order_bytes).hexdigest()
        intent = {"worker_id": worker_id, "decision_id": decision.decision_id, "work_order_id": decision.work_order_id, "work_order_hash": work_order_hash, "message_id": candidate.message_id, "content_hash": candidate.content_hash, "run_id": env.run_id, "step": env.step, "parent": env.parent}
        state["mode"] = "READY_TO_DISPATCH"
        state["dispatch_intent"] = intent
        state["decisions"][decision.decision_id] = {"decision_hash": candidate.content_hash, "state": "AUTHORIZED", "work_order_id": decision.work_order_id, "message_id": candidate.message_id}
        state["work_orders"][decision.work_order_id] = {"decision_id": decision.decision_id, "decision_hash": candidate.content_hash, "work_order_hash": work_order_hash, "state": "AUTHORIZED", "post_completion": decision.post_completion.value, "further_work_requires_new_decision": True}
        self.store.save(state)
        state["mode"] = "DISPATCHING"
        self.store.save(state)
        try:
            staged = stage_instruction(self.config.local_project_storage, candidate.message, env, decision=decision)
            worker = self.launcher.launch(staged_path=staged, envelope=env, content_hash=candidate.content_hash, message_id=candidate.message_id, worker_id=worker_id)
        except Exception as exc:
            state = self.store.load()
            state["mode"] = "STOPPED"
            state["last_error"] = f"dispatch failed: {type(exc).__name__}"
            self.store.save(state)
            self.ledger.append("launch_failed", message_id=candidate.message_id, decision_id=decision.decision_id, reason=type(exc).__name__)
            return PollResult("launch_failed", candidate.message_id, decision_id=decision.decision_id, work_order_id=decision.work_order_id)
        worker.update({"worker_id": worker_id, "message_id": candidate.message_id, "content_hash": candidate.content_hash, "staged_path": str(staged), "decision_id": decision.decision_id, "work_order_id": decision.work_order_id, "work_order_hash": work_order_hash, "post_completion": decision.post_completion.value, "parent": env.parent})
        state = self.store.load()
        state.update({"mode": "BUSY", "pending_worker": worker, "dispatch_intent": None, "last_error": None})
        state["decisions"][decision.decision_id].update({"state": "DISPATCHED", "worker_id": worker_id})
        state["work_orders"][decision.work_order_id].update({"state": "DISPATCHED", "worker_id": worker_id})
        self.store.save(state)
        self.ledger.append("worker_process_created", message_id=candidate.message_id, step=env.step, worker_id=worker_id, decision_id=decision.decision_id, work_order_id=decision.work_order_id)
        self.ledger.append("worker_launch_pending_claim", message_id=candidate.message_id, step=env.step, worker_id=worker_id)
        return PollResult("worker_process_created", candidate.message_id, staged, worker, decision.decision_id, decision.work_order_id)

    def _dispatch_legacy(self, state: dict, candidate: Candidate) -> PollResult:
        env = candidate.envelope
        try:
            staged = stage_instruction(self.config.local_project_storage, candidate.message, env)
            worker = self.launcher.launch(staged_path=staged, envelope=env, content_hash=candidate.content_hash, message_id=candidate.message_id)
        except Exception as exc:
            state["last_error"] = f"launch failed: {type(exc).__name__}"
            self.store.save(state)
            self.ledger.append("launch_failed", message_id=candidate.message_id, reason=type(exc).__name__)
            return PollResult("launch_failed", candidate.message_id)
        worker.update({"message_id": candidate.message_id, "content_hash": candidate.content_hash, "staged_path": str(staged)})
        state.update({"mode": "BUSY", "pending_worker": worker, "last_error": None})
        self.store.save(state)
        self.ledger.append("worker_process_created", message_id=candidate.message_id, step=env.step, worker_id=worker.get("worker_id"), pid=worker.get("pid"))
        self.ledger.append("worker_launch_pending_claim", message_id=candidate.message_id, step=env.step, worker_id=worker.get("worker_id"))
        return PollResult("worker_process_created", candidate.message_id, staged, worker)


class NoopWorkerLauncher:
    """Small launcher used by diagnostics and deterministic tests."""

    def __init__(self, pid: int | None = None):
        self.pid = os.getpid() if pid is None else pid
        self.calls: list[dict] = []

    def launch(self, *, staged_path: Path, envelope: ProtocolEnvelope, content_hash: str, message_id: str, worker_id: str | None = None) -> dict:
        worker_id = worker_id or str(uuid4())
        owner = {"worker_id": worker_id, "pid": self.pid, "project_id": envelope.project_id, "run_id": envelope.run_id, "step": envelope.step, "parent": envelope.parent, "started_at": now(), "exe": Path(os.sys.executable).name}
        self.calls.append({"staged_path": staged_path, "envelope": envelope, "content_hash": content_hash, "message_id": message_id, "owner": owner})
        return owner
