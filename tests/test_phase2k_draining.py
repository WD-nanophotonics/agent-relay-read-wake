from __future__ import annotations

from pathlib import Path

from agent_relay.config import EXPECTED_CHAT_URL, RelayConfig
from agent_relay.gmail import Attachment, GmailMessage
from agent_relay.handoff import write_evidence
from agent_relay.supervisor import Supervisor, SupervisorState, write_work_completion_receipt
from agent_relay.wake import WakeResult


def cfg(root: Path) -> RelayConfig:
    return RelayConfig(
        "gmail-courier", "Gmail Courier", "AR-GMAILCOURIER-A1R7P", root,
        root / "storage", "mock", "", "Mock", EXPECTED_CHAT_URL, 1, True,
        root / "gmail",
    )


def body(step: int) -> str:
    return f"""AGENTRELAY/1

CHANNEL: AR-GMAILCOURIER-A1R7P
RUN: RUN-20260816-PHASE2
STEP: {step:04d}
PARENT: {step - 1:04d}
DISPOSITION: WAKE
PROJECT: gmail-courier

draining lifecycle task {step}
"""


class Gateway:
    def __init__(self):
        self.items = {
            "m1": GmailMessage("m1", "thread", "received", body(1), (Attachment("one.txt", b"one"),)),
            "m2": GmailMessage("m2", "thread", "received", body(2), (Attachment("two.txt", b"two"),)),
        }
        self.list_calls = 0

    def list_messages(self):
        self.list_calls += 1
        return list(self.items)

    def fetch_message(self, message_id):
        return self.items[message_id]

    def test_connection(self):
        return None


class DelayedTransport:
    worker_id = "worker-1"

    def __init__(self):
        self.terminal = False
        self.quiet = False
        self.interrupt_calls = []
        self.wakes = []

    def validate_target(self, _target):
        return WakeResult(True, "ready")

    def wake(self, lease, _instruction):
        self.wakes.append(lease.step)
        return WakeResult(True, "started", completed=False, process_id=7, turn_id=f"turn-{lease.step}")

    def turn_completed(self, _lease):
        return self.terminal

    def interrupt_turn_once(self, lease):
        if lease.lease_id not in self.interrupt_calls:
            self.interrupt_calls.append(lease.lease_id)
            return True
        return False

    def transport_quiescent(self, _lease):
        return self.quiet


def _start(root: Path):
    gateway = Gateway()
    adapter = DelayedTransport()
    relay = Supervisor(cfg(root), gateway, adapter)
    relay.start()
    assert relay.process_message_id("m1") == "wake-accepted"
    return relay, gateway, adapter


def _write_receipt(relay: Supervisor):
    active = relay.snapshot()["active_lease"]
    write_evidence(
        relay.config.local_project_storage,
        lease_id=active["lease_id"],
        worker_id=active["worker_id"],
        handoff_token=active["handoff_token"],
        chat_url=EXPECTED_CHAT_URL,
    )
    write_work_completion_receipt(
        relay.config.local_project_storage,
        active["lease_id"],
        active["completion_token"],
        active["handoff_token"],
    )


def test_completion_drains_before_next_gmail_and_continues_automatically(tmp_path):
    relay, gateway, adapter = _start(tmp_path)
    _write_receipt(relay)

    relay.poll_once()
    assert relay.snapshot()["state"] == SupervisorState.DRAINING
    assert relay.snapshot()["active_lease"] is not None
    assert gateway.list_calls == 0
    assert relay.process_message_id("m2") == "deferred"
    assert adapter.interrupt_calls

    relay.poll_once()
    assert relay.snapshot()["state"] == SupervisorState.DRAINING
    assert gateway.list_calls == 0
    assert relay.snapshot()["state"] != SupervisorState.HUMAN_REQUIRED

    adapter.terminal = True
    adapter.quiet = True
    relay.poll_once()
    assert gateway.list_calls == 1
    assert adapter.wakes == [1, 2]
    assert relay.snapshot()["state"] == SupervisorState.AGENT_RUNNING
    assert relay.snapshot()["active_lease"]["step"] == 2
    assert "m2" in relay.snapshot()["consumed_message_ids"]


def test_deferred_wake_is_not_consumed_or_lost_during_drain(tmp_path):
    relay, gateway, adapter = _start(tmp_path)
    _write_receipt(relay)
    relay.poll_once()
    assert relay.snapshot()["state"] == SupervisorState.DRAINING
    assert "m2" not in relay.snapshot()["consumed_message_ids"]
    assert gateway.list_calls == 0
    assert not adapter.wakes[1:]
