from __future__ import annotations

from pathlib import Path

import pytest

from agent_relay.config import EXPECTED_CHAT_URL, RelayConfig
from agent_relay.gmail import Attachment, GmailMessage
from agent_relay.handoff import evidence_path, validate_evidence, write_evidence
from agent_relay.supervisor import Supervisor
from agent_relay.wake import LeaseKind, MockWakeAdapter, WakeResult


def env(step=1):
    return f"AGENTRELAY/1\n\nCHANNEL: AR-GMAILCOURIER-A1R7P\nRUN: RUN-20260816-PHASE2\nSTEP: {step:04d}\nPARENT: {step-1:04d}\nDISPOSITION: WAKE\nPROJECT: gmail-courier\n\nfirst work"


class Gmail:
    def __init__(self): self.items = {"m": GmailMessage("m", "t", "r", env(), (Attachment("task.txt", b"x"),))}
    def list_messages(self): return list(self.items)
    def fetch_message(self, mid): return self.items[mid]
    def test_connection(self): pass


def config(root: Path):
    return RelayConfig("gmail-courier", "Gmail Courier", "AR-GMAILCOURIER-A1R7P", root, root / "storage", "mock", "", "Mock", "", 20, True, root / "gmail")


def realish():
    class Adapter(MockWakeAdapter):
        def wake(self, lease, instruction):
            self.calls.append((lease, instruction)); return WakeResult(True, "accepted", completed=False)
    return Adapter()


def test_work_lease_has_distinct_handoff_token(tmp_path):
    relay = Supervisor(config(tmp_path), Gmail(), realish()); relay.start(); assert relay.process_message_id("m", LeaseKind.WORK) == "wake-accepted"
    active = relay.snapshot()["active_lease"]
    assert active["handoff_token"] and active["handoff_token"] != active["completion_token"]


def test_handoff_evidence_enforces_fixed_url_and_exact_identity(tmp_path):
    relay = Supervisor(config(tmp_path), Gmail(), realish()); relay.start(); relay.process_message_id("m", LeaseKind.WORK)
    active = relay.snapshot()["active_lease"]
    with pytest.raises(ValueError, match="fixed ChatGPT"):
        write_evidence(config(tmp_path).local_project_storage, lease_id=active["lease_id"], worker_id=active["worker_id"], handoff_token=active["handoff_token"], chat_url="https://chatgpt.com/c/other")
    write_evidence(config(tmp_path).local_project_storage, lease_id=active["lease_id"], worker_id=active["worker_id"], handoff_token=active["handoff_token"], chat_url=EXPECTED_CHAT_URL)
    with pytest.raises(ValueError):
        validate_evidence(config(tmp_path).local_project_storage, active, handoff_token="wrong")


def test_work_completion_requires_matching_evidence_and_rejects_duplicate(tmp_path):
    relay = Supervisor(config(tmp_path), Gmail(), realish()); relay.start(); relay.process_message_id("m", LeaseKind.WORK)
    active = relay.snapshot()["active_lease"]
    with pytest.raises((RuntimeError, ValueError), match="handoff"):
        relay.write_completion_record(active["lease_id"], handoff_succeeded=True, completion_token=active["completion_token"], handoff_token=active["handoff_token"])
    write_evidence(config(tmp_path).local_project_storage, lease_id=active["lease_id"], worker_id=active["worker_id"], handoff_token=active["handoff_token"], chat_url=EXPECTED_CHAT_URL)
    relay.write_completion_record(active["lease_id"], handoff_succeeded=True, completion_token=active["completion_token"], handoff_token=active["handoff_token"])
    assert relay.consume_completion_record() is True
    assert relay.consume_completion_record() is False


def test_handoff_budget_is_bounded(tmp_path):
    path = write_evidence(tmp_path, lease_id="lease", worker_id="worker", handoff_token="token", chat_url=EXPECTED_CHAT_URL)
    assert evidence_path(tmp_path, "lease") == path
    with pytest.raises(ValueError, match="bounded"):
        write_evidence(tmp_path, lease_id="other", worker_id="worker", handoff_token="token", chat_url=EXPECTED_CHAT_URL, send_attempts=2)
