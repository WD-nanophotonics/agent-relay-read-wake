from __future__ import annotations

from dataclasses import asdict
from enum import StrEnum
import hashlib
import logging
from pathlib import Path
from threading import Event, RLock
from typing import Any

from .config import RelayConfig
from .gmail import GmailGateway, GmailMessage
from .protocol import Disposition, ProtocolError, parse_envelope
from .storage import Ledger, StateStore, atomic_json, now, read_content_hash, stage_instruction
from .wake import CodexTarget, LeaseStatus, WakeAdapter, WakeLease, wake_instruction


class SupervisorState(StrEnum):
    STOPPED = "STOPPED"
    MONITORING = "MONITORING"
    STAGING = "STAGING"
    READY_TO_WAKE = "READY_TO_WAKE"
    WAKING = "WAKING"
    AGENT_RUNNING = "AGENT_RUNNING"
    WAITING_FOR_REPLY = "WAITING_FOR_REPLY"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
    ERROR = "ERROR"


class Supervisor:
    def __init__(self, config: RelayConfig, gateway: GmailGateway, wake_adapter: WakeAdapter):
        self.config, self.gateway, self.wake_adapter = config, gateway, wake_adapter
        self.store = StateStore(config.local_project_storage)
        self.ledger = Ledger(config.local_project_storage)
        self.stop_event = Event()
        self.lock = RLock()
        self.state = self.store.load()
        if self.state.get("active_lease"):
            self.state["state"] = SupervisorState.HUMAN_REQUIRED
            self.state["last_error"] = "RECOVERY_REQUIRED: persisted lease outcome is unknown"
        else:
            self.state["state"] = SupervisorState.STOPPED
        self.store.save(self.state)

    @property
    def target(self) -> CodexTarget:
        return CodexTarget(self.config.target_type, self.config.target_id, self.config.target_label, self.config.repo_path)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return dict(self.state)

    def _save(self) -> None:
        self.store.save(self.state)

    def _transition(self, state: SupervisorState, reason: str, **details: Any) -> None:
        before = self.state.get("state")
        self.state["state"] = state
        self.state["last"].update({key: value for key, value in details.items() if value is not None})
        self._save()
        self.ledger.append("STATE_CHANGED", project_id=self.config.project_id, state_before=before, state_after=state, reason=reason, **details)

    def start(self) -> None:
        with self.lock:
            if not self.config.enabled:
                raise RuntimeError("project is disabled")
            if self.state.get("active_lease"):
                raise RuntimeError("RECOVERY_REQUIRED: resolve the persisted active lease before monitoring")
            validation = self.wake_adapter.validate_target(self.target)
            if not validation.accepted:
                self._human_required("target-validation-failed", detail=validation.detail)
                raise RuntimeError(validation.detail)
            self.stop_event.clear()
            self._transition(SupervisorState.MONITORING, "user-started")
            self.ledger.append("SUPERVISOR_STARTED", project_id=self.config.project_id)

    def stop(self) -> None:
        with self.lock:
            self.stop_event.set()
            self._transition(SupervisorState.STOPPED, "user-stopped")
            self.ledger.append("SUPERVISOR_STOPPED", project_id=self.config.project_id)

    def test_gmail(self) -> None:
        self.gateway.test_connection()
        self.ledger.append("GMAIL_TEST_SUCCEEDED", project_id=self.config.project_id)

    def test_wake(self) -> bool:
        """Exercise only the configured Phase 1 mock adapter while monitoring."""
        with self.lock:
            if self.state["state"] != SupervisorState.MONITORING or self.stop_event.is_set():
                raise RuntimeError("Start monitoring before testing wake")
            if self.config.target_type != "mock" or self.state.get("active_lease"):
                raise RuntimeError("Test Wake is available only for an idle mock target")
            instruction_path = self.config.local_project_storage / "diagnostics" / "test-wake-instruction.txt"
            instruction_path.parent.mkdir(parents=True, exist_ok=True)
            instruction_path.write_text("AGENTRELAY_TEST_WAKE/1\nMock certification only.\n", encoding="utf-8")
            lease = WakeLease.create(self.config.project_id, "RUN-TEST-WAKE", 0, instruction_path)
            self.ledger.append("WAKE_ATTEMPTED", project_id=self.config.project_id, lease_id=lease.lease_id, reason="manual-mock-test")
        result = self.wake_adapter.wake(lease, wake_instruction(lease))
        self.ledger.append("WAKE_SUCCEEDED" if result.accepted else "WAKE_FAILED", project_id=self.config.project_id, lease_id=lease.lease_id, reason="manual-mock-test")
        return result.accepted

    def complete_lease(self, lease_id: str, outcome: str = "completed") -> None:
        """Deterministic local completion callback; exact active lease ID is required."""
        with self.lock:
            active = self.state.get("active_lease")
            if not active or active.get("lease_id") != lease_id or self.state["state"] != SupervisorState.AGENT_RUNNING:
                raise RuntimeError("completion does not match the active running lease")
            active["status"] = LeaseStatus.COMPLETED.value if outcome == "completed" else LeaseStatus.FAILED.value
            self.state["active_lease"] = None
            self.state["last"]["last_lease"] = active
            self.state["last"]["last_agent_completion"] = outcome
            self._save()
            self.ledger.append("LEASE_COMPLETED" if outcome == "completed" else "LEASE_FAILED", project_id=self.config.project_id, lease_id=lease_id, reason=outcome)
            if outcome == "completed":
                self._transition(SupervisorState.WAITING_FOR_REPLY, "deterministic-lease-completion", lease_id=lease_id)
            else:
                self._human_required("agent-lease-failed", lease_id=lease_id)

    def poll_once(self) -> None:
        with self.lock:
            if self.state["state"] not in (SupervisorState.MONITORING, SupervisorState.WAITING_FOR_REPLY) or self.stop_event.is_set():
                return
            self.consume_completion_record()
        try:
            ids = self.gateway.list_messages()
            self.ledger.append("GMAIL_POLLED", project_id=self.config.project_id, count=len(ids))
            self.state["last"]["last_gmail_poll"] = now(); self._save()
            for message_id in reversed(ids):
                if self.stop_event.is_set():
                    return
                self.process_message_id(message_id)
        except Exception as exc:
            self._error("gmail-poll-failed", str(exc))

    def completion_path(self, lease_id: str) -> Path:
        return self.config.local_project_storage / "completions" / f"{lease_id}.json"

    def write_completion_record(self, lease_id: str, outcome: str = "completed") -> Path:
        if outcome not in {"completed", "failed"}:
            raise ValueError("completion outcome must be completed or failed")
        target = self.completion_path(lease_id)
        atomic_json(target, {"lease_id": lease_id, "outcome": outcome, "recorded_at": now()})
        return target

    def consume_completion_record(self) -> bool:
        active = self.state.get("active_lease")
        if not active or self.state["state"] != SupervisorState.AGENT_RUNNING:
            return False
        path = self.completion_path(active["lease_id"])
        if not path.exists():
            return False
        try:
            import json
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("lease_id") != active["lease_id"]:
                raise ValueError("lease ID mismatch")
            self.complete_lease(active["lease_id"], record.get("outcome", "failed"))
            return True
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self._human_required("malformed-completion-record", active["lease_id"], detail=str(exc))
            return False

    def _error(self, reason: str, detail: str) -> None:
        with self.lock:
            self.state["last_error"] = detail[:500]
            self._transition(SupervisorState.ERROR, reason)
            self.ledger.append("ERROR", project_id=self.config.project_id, reason=reason)

    @staticmethod
    def _hash(message: GmailMessage) -> str:
        return hashlib.sha256(message.body.encode() + b"\0" + b"\0".join(item.data for item in message.attachments)).hexdigest()

    def _human_required(self, reason: str, message_id: str | None = None, **values: Any) -> None:
        self.state["last_error"] = reason
        self._transition(SupervisorState.HUMAN_REQUIRED, reason, gmail_message_id=message_id, **values)
        self.ledger.append("HUMAN_REQUIRED", project_id=self.config.project_id, gmail_message_id=message_id, reason=reason, **values)

    @staticmethod
    def _lease_record(lease: WakeLease, status: LeaseStatus | None = None) -> dict[str, Any]:
        record = asdict(lease)
        record["staged_instruction_path"] = str(lease.staged_instruction_path)
        if status is not None:
            record["status"] = status.value
        return record

    def process_message_id(self, message_id: str) -> str:
        with self.lock:
            if self.stop_event.is_set() or self.state["state"] not in (SupervisorState.MONITORING, SupervisorState.WAITING_FOR_REPLY):
                return "stopped"
            if message_id in self.state["consumed_message_ids"]:
                self.ledger.append("DUPLICATE_IGNORED", project_id=self.config.project_id, gmail_message_id=message_id, reason="already-consumed")
                return "duplicate"
        message = self.gateway.fetch_message(message_id)
        try:
            envelope = parse_envelope(message.body)
        except ProtocolError:
            self.ledger.append("MESSAGE_IGNORED", project_id=self.config.project_id, gmail_message_id=message_id, reason="not-valid-agentrelay-envelope")
            return "ignored"
        with self.lock:
            if envelope.channel_id != self.config.channel_id or envelope.project_id != self.config.project_id:
                self.ledger.append("MESSAGE_IGNORED", project_id=self.config.project_id, gmail_message_id=message_id, reason="wrong-project-or-channel")
                return "ignored"
            self.ledger.append("MESSAGE_MATCHED", project_id=self.config.project_id, gmail_message_id=message_id, run_id=envelope.run_id, step_id=envelope.step)
            if self.stop_event.is_set():
                return "stopped"
            expected = self.state["expected_step"]
            if self.state["current_run"] not in (None, envelope.run_id):
                self._human_required("conflicting-run", message_id, run_id=envelope.run_id, step_id=envelope.step); return "human-required"
            logical_key = f"{envelope.run_id}:{envelope.step}"
            prior_hash = self.state["logical_steps"].get(logical_key)
            content_hash = self._hash(message)
            if prior_hash and prior_hash != content_hash:
                self._human_required("conflicting-logical-step", message_id, run_id=envelope.run_id, step_id=envelope.step); return "human-required"
            if envelope.step < expected:
                self.ledger.append("MESSAGE_IGNORED", project_id=self.config.project_id, gmail_message_id=message_id, reason="old-step", run_id=envelope.run_id, step_id=envelope.step); return "old"
            if envelope.step > expected or envelope.parent != self.state["expected_parent"]:
                self._human_required("out-of-order-step-or-parent", message_id, run_id=envelope.run_id, step_id=envelope.step); return "human-required"
            if self.state.get("active_lease"):
                self._human_required("active-lease-exists", message_id, run_id=envelope.run_id, step_id=envelope.step); return "human-required"
            self._transition(SupervisorState.STAGING, "validated-envelope", gmail_message_id=message_id)
        if self.stop_event.is_set():
            return "stopped"
        try:
            staged = stage_instruction(self.config.local_project_storage, message, envelope)
        except Exception as exc:
            self._error("staging-failed", str(exc)); return "error"
        with self.lock:
            if self.stop_event.is_set():
                return "stopped"
            self.state["consumed_message_ids"].append(message_id)
            self.state["logical_steps"][f"{envelope.run_id}:{envelope.step}"] = read_content_hash(staged)
            self.state["current_run"] = envelope.run_id
            self.state["expected_step"] = envelope.step + 1
            self.state["expected_parent"] = envelope.step
            self.state["last"]["last_staged_instruction"] = str(staged)
            self._save()
            self.ledger.append("MESSAGE_STAGED", project_id=self.config.project_id, gmail_message_id=message_id, run_id=envelope.run_id, step_id=envelope.step, staged_instruction_path=str(staged))
            if envelope.disposition is not Disposition.WAKE:
                if envelope.disposition is Disposition.HUMAN_REQUIRED:
                    self._human_required("sender-requested-human", message_id, run_id=envelope.run_id, step_id=envelope.step)
                    return "human-required"
                self._transition(SupervisorState.WAITING_FOR_REPLY, "no-action", gmail_message_id=message_id)
                return "no-action"
            self._transition(SupervisorState.READY_TO_WAKE, "wake-authorized", gmail_message_id=message_id)
            lease = WakeLease.create(self.config.project_id, envelope.run_id, envelope.step, staged)
            self.state["active_lease"] = self._lease_record(lease); self._save()
            self.ledger.append("WAKE_AUTHORIZED", project_id=self.config.project_id, gmail_message_id=message_id, run_id=envelope.run_id, step_id=envelope.step, lease_id=lease.lease_id)
            self._transition(SupervisorState.WAKING, "adapter-invocation", lease_id=lease.lease_id)
        if self.stop_event.is_set():
            return "stopped"
        self.ledger.append("WAKE_ATTEMPTED", project_id=self.config.project_id, lease_id=lease.lease_id)
        result = self.wake_adapter.wake(lease, wake_instruction(lease))
        with self.lock:
            if result.accepted:
                active = self._lease_record(lease, LeaseStatus.ACTIVE)
                active["process_id"] = result.process_id
                self.state["active_lease"] = active; self._save()
                self.ledger.append("WAKE_SUCCEEDED", project_id=self.config.project_id, lease_id=lease.lease_id, process_id=result.process_id)
                self._transition(SupervisorState.AGENT_RUNNING, "wake-accepted", lease_id=lease.lease_id, process_id=result.process_id)
                if result.completed:
                    self.complete_lease(lease.lease_id)
                    return "woken"
                return "wake-accepted"
            self.state["active_lease"] = None; self._save()
            self.ledger.append("WAKE_FAILED", project_id=self.config.project_id, lease_id=lease.lease_id, reason=result.detail[:500])
            self._human_required("wake-failed", message_id, lease_id=lease.lease_id)
            return "wake-failed"
