from __future__ import annotations

from pathlib import Path

from agent_relay.config import RelayConfig
from agent_relay.gmail import Attachment, GmailMessage
from agent_relay.handoff import build_actionable_report, write_evidence
from agent_relay.supervisor import Supervisor, SupervisorState
from agent_relay.wake import LeaseKind, MockWakeAdapter, WakeResult, wake_instruction


def body(step: int) -> str:
    return f"AGENTRELAY/1\n\nCHANNEL: AR-GMAILCOURIER-A1R7P\nRUN: RUN-20260816-PHASE2\nSTEP: {step:04d}\nPARENT: {step-1:04d}\nDISPOSITION: WAKE\nPROJECT: gmail-courier\n\nTiny return-path task."


class Gmail:
    def __init__(self):
        self.messages = {str(i): GmailMessage(str(i), "thread", "received", body(i), (Attachment("task.txt", b"x"),)) for i in (1, 2)}
    def list_messages(self): return list(self.messages)
    def fetch_message(self, mid): return self.messages[mid]
    def test_connection(self): pass


def config(root: Path):
    return RelayConfig("gmail-courier", "Gmail Courier", "AR-GMAILCOURIER-A1R7P", root, root / "storage", "mock", "", "Mock", "", 20, True, root / "gmail")


class Realish(MockWakeAdapter):
    def __init__(self): super().__init__(); self.stops = 0
    def wake(self, lease, instruction):
        self.calls.append((lease, instruction)); return WakeResult(True, "accepted", completed=False)
    def stop_backend(self): self.stops += 1


def finish_work(relay: Supervisor):
    active = relay.snapshot()["active_lease"]
    write_evidence(relay.config.local_project_storage, lease_id=active["lease_id"], worker_id=active["worker_id"], handoff_token=active["handoff_token"], chat_url="https://chatgpt.com/c/6a818a0c-5208-83ee-95cd-fd558d66ecc9")
    relay.write_completion_record(active["lease_id"], completion_token=active["completion_token"], handoff_token=active["handoff_token"], handoff_succeeded=True)
    assert relay.consume_completion_record()


def test_persistent_supervisor_waits_after_work_and_accepts_next_step(tmp_path):
    adapter = Realish(); relay = Supervisor(config(tmp_path), Gmail(), adapter); relay.start()
    assert relay.process_message_id("1", LeaseKind.WORK) == "wake-accepted"
    finish_work(relay)
    assert relay.snapshot()["state"] == SupervisorState.WAITING_FOR_REPLY and adapter.stops == 0
    assert relay.process_message_id("2", LeaseKind.WORK) == "wake-accepted"
    assert len(adapter.calls) == 2


def test_actionable_handoff_contains_exact_return_contract():
    report = build_actionable_report(run_id="RUN-20260816-PHASE2", step=9, project_id="gmail-courier", channel_id="AR-GMAILCOURIER-A1R7P", lease_id="lease", worker_id="worker", handoff_token="token", repository="WD-nanophotonics/agent-relay-read-wake", branch="main", baseline_sha="base", remote_head="head", tests="36 passed", summary="persistent runner", blockers="KNOWN_NON_BLOCKING_UX_DEFECT", next_boundary="Phase 2J")
    assert "AGENTRELAY_CHATGPT_HANDOFF/1" in report
    assert "ACTION_REQUIRED: AUDIT_REMOTE_AND_SEND_NEXT_GMAIL" in report
    assert "NEXT_STEP: 0010" in report and "NEXT_PARENT: 0009" in report
    assert "WORKER = execution" not in report


def test_work_instruction_does_not_authorize_worker_to_send_gmail(tmp_path):
    from agent_relay.wake import WakeLease
    lease = WakeLease.create("gmail-courier", "RUN-20260816-PHASE2", 9, tmp_path / "staged", LeaseKind.WORK, "worker")
    instruction = wake_instruction(lease)
    assert "access Gmail" in instruction and "fixed ChatGPT URL" in instruction
    assert "complete-work" not in instruction
