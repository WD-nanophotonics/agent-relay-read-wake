from __future__ import annotations

from pathlib import Path
import time

from agent_relay.app_server import AppServerController
from agent_relay.config import EXPECTED_CHAT_URL, RelayConfig
from agent_relay.gmail import Attachment, GmailMessage
from agent_relay.handoff import write_evidence
from agent_relay.runner import RunnerOwnership, _pid_alive
from agent_relay.supervisor import Supervisor, SupervisorState, write_work_completion_receipt
from agent_relay.wake import CodexAppServerWakeAdapter, CodexTarget, WakeLease, WakeResult


def config(root: Path) -> RelayConfig:
    return RelayConfig(
        "gmail-courier", "Gmail Courier", "AR-GMAILCOURIER-A1R7P", root,
        root / "storage", "mock", "", "Mock", EXPECTED_CHAT_URL, 1, True,
        root / "gmail",
    )


def message() -> GmailMessage:
    body = """AGENTRELAY/1

CHANNEL: AR-GMAILCOURIER-A1R7P
RUN: RUN-20260816-PHASE2
STEP: 0001
PARENT: 0000
DISPOSITION: WAKE
PROJECT: gmail-courier

completion-path test
"""
    return GmailMessage("m1", "thread", "received", body, (Attachment("task.txt", b"task"),))


class Gateway:
    def __init__(self):
        self.list_calls = 0

    def list_messages(self):
        self.list_calls += 1
        return []

    def fetch_message(self, message_id):
        assert message_id == "m1"
        return message()

    def test_connection(self):
        return None


class CompletionAdapter:
    worker_id = "worker-1"

    def validate_target(self, _target):
        return WakeResult(True, "ready")

    def wake(self, _lease, _instruction):
        return WakeResult(True, "started", completed=False, process_id=7, turn_id="turn-1")

    def turn_completed(self, _lease):
        return True


def start_active(root: Path):
    gateway = Gateway()
    adapter = CompletionAdapter()
    relay = Supervisor(config(root), gateway, adapter)
    relay.start()
    assert relay.process_message_id("m1") == "wake-accepted"
    return relay, gateway


def record_handoff_and_receipt(relay: Supervisor) -> Path:
    active = relay.snapshot()["active_lease"]
    write_evidence(
        relay.config.local_project_storage,
        lease_id=active["lease_id"],
        worker_id=active["worker_id"],
        handoff_token=active["handoff_token"],
        chat_url=EXPECTED_CHAT_URL,
    )
    return write_work_completion_receipt(
        relay.config.local_project_storage,
        active["lease_id"],
        active["completion_token"],
        active["handoff_token"],
    )


def test_running_poll_consumes_receipt_before_gmail_poll(tmp_path):
    relay, gateway = start_active(tmp_path)
    record_handoff_and_receipt(relay)
    relay.poll_once()
    assert relay.snapshot()["state"] == SupervisorState.WAITING_FOR_REPLY
    assert relay.snapshot()["active_lease"] is None
    assert gateway.list_calls == 1


def test_pure_work_writer_does_not_construct_or_mutate_supervisor(tmp_path):
    relay, _ = start_active(tmp_path)
    receipt = record_handoff_and_receipt(relay)
    assert receipt.exists()
    fresh = Supervisor(config(tmp_path), Gateway(), CompletionAdapter())
    assert fresh.snapshot()["state"] == SupervisorState.AGENT_RUNNING
    assert fresh.snapshot()["active_lease"]["lease_id"] == relay.snapshot()["active_lease"]["lease_id"]


def test_constructor_recovery_policy_is_explicit(tmp_path):
    relay, _ = start_active(tmp_path)
    readonly = Supervisor(config(tmp_path), Gateway(), CompletionAdapter())
    assert readonly.snapshot()["state"] == SupervisorState.AGENT_RUNNING
    recovered = Supervisor(config(tmp_path), Gateway(), CompletionAdapter(), startup_recovery=True)
    assert recovered.snapshot()["state"] == SupervisorState.HUMAN_REQUIRED


def test_verified_completion_recovery_never_fabricates_terminal_event(tmp_path):
    relay, _ = start_active(tmp_path)
    active = relay.snapshot()["active_lease"]
    record_handoff_and_receipt(relay)
    recovered = Supervisor(config(tmp_path), Gateway(), CompletionAdapter())
    assert recovered.recover_verified_completion(active["lease_id"], "bootstrap-control-path-unavailable")
    assert recovered.snapshot()["state"] == SupervisorState.WAITING_FOR_REPLY
    assert recovered.snapshot()["active_lease"] is None


def test_terminal_event_is_retained_and_consumed_by_exact_identity(tmp_path):
    controller = AppServerController("codex.cmd", tmp_path, tmp_path / "app.log", "worker")
    controller._handle_server_message({
        "method": "turn/completed",
        "params": {"threadId": "worker", "turn": {"id": "turn-1", "status": "completed"}},
    })
    assert controller.poll_notifications() == []
    assert controller.consume_terminal_event("other", "turn-1") is None
    event = controller.consume_terminal_event("worker", "turn-1")
    assert event and event["params"]["turn"]["id"] == "turn-1"
    assert controller.consume_terminal_event("worker", "turn-1") is None


def test_interrupt_is_exactly_once_after_grace_window(tmp_path):
    class Controller:
        def __init__(self):
            self.calls = []

        def consume_terminal_event(self, _thread_id, _turn_id):
            return None

        def interrupt_turn(self, thread_id, turn_id):
            self.calls.append((thread_id, turn_id))
            return {"status": "accepted"}

    adapter = CodexAppServerWakeAdapter(
        CodexTarget("codex-app-server", "worker", "Worker", tmp_path),
        tmp_path / "logs",
    )
    adapter.controller = Controller()
    adapter.worker_id = "worker"
    adapter.interrupt_grace_seconds = 0
    lease = WakeLease.create("gmail-courier", "RUN", 1, tmp_path / "instruction", worker_id="worker")
    lease = lease.__class__(**{**lease.__dict__, "turn_id": "turn-1"})
    adapter.last_turn_started_at = time.monotonic()
    assert adapter.interrupt_turn_once(lease)
    assert not adapter.interrupt_turn_once(lease)
    assert adapter.controller.calls == [("worker", "turn-1")]


def test_runner_ownership_and_pid_probe_are_bounded(tmp_path, monkeypatch):
    owner = RunnerOwnership(tmp_path, "gmail-courier")
    assert owner.acquire()
    assert owner.acquire() is False
    owner.release()
    monkeypatch.setattr("agent_relay.runner.os.kill", lambda *_args: (_ for _ in ()).throw(SystemError("probe")))
    assert _pid_alive(999999) is False
