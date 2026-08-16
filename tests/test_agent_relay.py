from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile

import pytest

from agent_relay.config import RelayConfig
from agent_relay.gmail import Attachment, GmailMessage
from agent_relay.protocol import Disposition, ProtocolError, parse_envelope
from agent_relay.supervisor import Supervisor, SupervisorState, write_completion_receipt
from agent_relay.wake import LeaseKind, MockWakeAdapter, WakeResult


def envelope(*, channel="AR-GMAILCOURIER-A1R7P", run="RUN-20260816-001", step=1, parent=0, disposition="WAKE", project="gmail-courier"):
    return f"AGENTRELAY/1\n\nCHANNEL: {channel}\nRUN: {run}\nSTEP: {step:04d}\nPARENT: {parent:04d}\nDISPOSITION: {disposition}\nPROJECT: {project}\n\nDo exactly one test lease."


class FakeGmail:
    def __init__(self, messages=()): self.messages = {message.message_id: message for message in messages}; self.tests = 0
    def test_connection(self): self.tests += 1
    def list_messages(self): return list(self.messages)
    def fetch_message(self, message_id): return self.messages[message_id]


def message(mid="m1", **kwargs):
    return GmailMessage(mid, "thread", "received", envelope(**kwargs), (Attachment("task.txt", b"payload"),))


def config(root: Path):
    return RelayConfig("gmail-courier", "Gmail Courier", "AR-GMAILCOURIER-A1R7P", root, root / "data" / "projects" / "gmail-courier", "mock", "", "Mock", "", 20, True, root / "gmail")


def supervisor(root: Path, messages=(), succeed=True):
    adapter = MockWakeAdapter(succeed); relay = Supervisor(config(root), FakeGmail(messages), adapter); relay.start(); return relay, adapter


def test_protocol_success_and_invalid_fields():
    parsed = parse_envelope(envelope())
    assert parsed.disposition is Disposition.WAKE and parsed.step == 1
    for text in ("hello", envelope(channel="wrong"), envelope(disposition="MAYBE")):
        with pytest.raises(ProtocolError): parse_envelope(text)


def test_valid_wake_stages_once_and_wakes(tmp_path):
    relay, adapter = supervisor(tmp_path, [message()])
    assert relay.poll_once() is None
    assert len(adapter.calls) == 1
    staged = tmp_path / "data" / "projects" / "gmail-courier" / "inbox" / "RUN-20260816-001" / "STEP-0001"
    assert (staged / "message.txt").exists() and (staged / "attachments" / "task.txt").read_bytes() == b"payload"
    assert relay.snapshot()["state"] == SupervisorState.WAITING_FOR_REPLY
    assert relay.process_message_id("m1") == "duplicate" and len(adapter.calls) == 1


def test_wrong_channel_and_human_required_never_wake(tmp_path):
    relay, adapter = supervisor(tmp_path, [message(channel="AR-OTHER-12345"), message("m2", disposition="HUMAN_REQUIRED")])
    assert relay.process_message_id("m1") == "ignored"
    assert relay.process_message_id("m2") == "human-required"
    assert not adapter.calls and relay.snapshot()["state"] == SupervisorState.HUMAN_REQUIRED


def test_ordering_conflict_and_stop_gate(tmp_path):
    relay, adapter = supervisor(tmp_path, [message("future", step=2, parent=1)])
    assert relay.process_message_id("future") == "human-required" and not adapter.calls
    relay, adapter = supervisor(tmp_path, [message("old")])
    assert relay.process_message_id("old") == "woken"
    relay.start()
    assert relay.process_message_id("old") == "duplicate"
    relay.stop()
    assert relay.process_message_id("old") == "stopped"


def test_conflicting_same_step_is_human_required(tmp_path):
    first = message("one")
    second = GmailMessage("two", "t", "r", envelope() + " changed", (Attachment("task.txt", b"different"),))
    relay, _ = supervisor(tmp_path, [first, second])
    assert relay.process_message_id("one") == "woken"
    relay.start()
    assert relay.process_message_id("two") == "human-required"


def test_mock_failure_and_restart_persistence(tmp_path):
    relay, adapter = supervisor(tmp_path, [message()], succeed=False)
    assert relay.process_message_id("m1") == "wake-failed" and len(adapter.calls) == 1
    fresh = Supervisor(config(tmp_path), FakeGmail([message()]), MockWakeAdapter())
    assert "m1" in fresh.snapshot()["consumed_message_ids"]


def test_manual_mock_wake_requires_monitoring_and_is_bounded(tmp_path):
    relay, adapter = supervisor(tmp_path)
    assert relay.test_wake() and len(adapter.calls) == 1
    relay.stop()
    with pytest.raises(RuntimeError, match="Start monitoring"):
        relay.test_wake()


def test_real_wake_acceptance_keeps_lease_running_until_completion_record(tmp_path):
    class Realish:
        def validate_target(self, _target): return WakeResult(True, "bound")
        def wake(self, _lease, _instruction): return WakeResult(True, "accepted", completed=False, process_id=1234)
    relay = Supervisor(config(tmp_path), FakeGmail([message()]), Realish())
    relay.start()
    assert relay.process_message_id("m1") == "wake-accepted"
    active = relay.snapshot()["active_lease"]
    assert relay.snapshot()["state"] == SupervisorState.AGENT_RUNNING and active["process_id"] == 1234
    relay.write_completion_record(active["lease_id"], handoff_succeeded=True)
    assert relay.consume_completion_record()
    assert relay.snapshot()["state"] == SupervisorState.WAITING_FOR_REPLY and relay.snapshot()["active_lease"] is None


def test_restart_with_inflight_lease_fails_closed(tmp_path):
    class Realish:
        def validate_target(self, _target): return WakeResult(True, "bound")
        def wake(self, _lease, _instruction): return WakeResult(True, "accepted", completed=False)
    relay = Supervisor(config(tmp_path), FakeGmail([message()]), Realish()); relay.start()
    assert relay.process_message_id("m1") == "wake-accepted"
    restored = Supervisor(config(tmp_path), FakeGmail(), Realish())
    assert restored.snapshot()["state"] == SupervisorState.HUMAN_REQUIRED
    assert restored.fail_active_lease("worker outcome unavailable")
    assert restored.snapshot()["active_lease"] is None and restored.snapshot()["state"] == SupervisorState.HUMAN_REQUIRED


def test_completion_requires_handoff_evidence(tmp_path):
    class Realish:
        def validate_target(self, _target): return WakeResult(True, "bound")
        def wake(self, _lease, _instruction): return WakeResult(True, "accepted", completed=False)
    relay = Supervisor(config(tmp_path), FakeGmail([message()]), Realish()); relay.start(); relay.process_message_id("m1")
    with pytest.raises(RuntimeError, match="handoff"):
        relay.write_completion_record(relay.snapshot()["active_lease"]["lease_id"])


def test_diagnostic_completion_requires_exact_token_and_kind(tmp_path):
    class Realish:
        def validate_target(self, _target): return WakeResult(True, "bound")
        def wake(self, _lease, _instruction): return WakeResult(True, "accepted", completed=False)
    relay = Supervisor(config(tmp_path), FakeGmail([message()]), Realish()); relay.start()
    assert relay.process_message_id("m1", LeaseKind.DIAGNOSTIC) == "wake-accepted"
    active = relay.snapshot()["active_lease"]
    assert active["lease_kind"] == LeaseKind.DIAGNOSTIC and active["completion_token"]
    write_completion_receipt(config(tmp_path).local_project_storage, active["lease_id"], active["completion_token"])
    assert relay.consume_completion_record()
    assert relay.snapshot()["state"] == SupervisorState.WAITING_FOR_REPLY


def test_self_recursion_target_is_rejected(tmp_path):
    class Realish:
        def validate_target(self, _target): return WakeResult(True, "bound")
        def wake(self, _lease, _instruction): return WakeResult(True, "accepted")
    cfg = replace(config(tmp_path), target_type="codex-cli", target_id="same", dev_session_id="same")
    relay = Supervisor(cfg, FakeGmail(), Realish())
    with pytest.raises(RuntimeError, match="development session"):
        relay.start()


def test_malformed_state_and_project_isolation(tmp_path):
    first = config(tmp_path)
    first.local_project_storage.mkdir(parents=True)
    (first.local_project_storage / "state.json").write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError, match="malformed"):
        Supervisor(first, FakeGmail(), MockWakeAdapter())
    other = replace(first, project_id="other", local_project_storage=tmp_path / "data" / "projects" / "other")
    relay = Supervisor(other, FakeGmail(), MockWakeAdapter())
    assert relay.store.path.parent.name == "other"
