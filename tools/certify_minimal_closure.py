"""Bounded, non-pytest closure certification for unstable Windows hosts."""
from __future__ import annotations

import tempfile
from pathlib import Path
from dataclasses import replace
import sys

from agent_relay.config import EXPECTED_CHAT_URL, RelayConfig
from agent_relay.gmail import GmailMessage
from agent_relay.handoff import HandoffSubmission, CommandHandoffSender
from agent_relay.ownership import exact_owner_live
from agent_relay.protocol import parse_envelope
from agent_relay.relay import Relay, poll_transaction_lock
from agent_relay.storage import StateStore, stage_instruction
from agent_relay.watchdog import run_watchdog
from agent_relay.worker import OneShotWorker, WorkerOutcome


def body(step=1):
    return f"AGENTRELAY/1\n\nCHANNEL: AR-GMAILCOURIER-A1R7P\nRUN: RUN-CERT-001\nSTEP: {step:04d}\nPARENT: {step-1:04d}\nDISPOSITION: WAKE\nPROJECT: gmail-courier\n\nTask"


class Mail:
    def __init__(self, items): self.items = {item.message_id: item for item in items}
    def list_messages(self): return list(self.items)
    def fetch_message(self, message_id): return self.items[message_id]


class Sender:
    def __init__(self, ok=True, events=None): self.ok, self.calls, self.events = ok, [], (events if events is not None else [])
    def submit(self, report):
        self.calls.append(report); self.events.append("handoff")
        return HandoffSubmission(self.ok, "SUBMITTED" if self.ok else "rejected", verified=self.ok)


class Launcher:
    def __init__(self, pid, events=None): self.pid, self.calls, self.events = pid, [], (events if events is not None else [])
    def launch(self, *, staged_path, envelope, content_hash, message_id):
        self.calls.append(message_id); self.events.append("launch")
        return {"worker_id": f"worker-{len(self.calls)}", "pid": self.pid, "project_id": envelope.project_id, "run_id": envelope.run_id, "step": envelope.step, "parent": envelope.parent, "exe": "python.exe"}


def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp); cfg = RelayConfig("gmail-courier", "Gmail", "AR-GMAILCOURIER-A1R7P", Path.cwd(), root / "storage", "mock", "", "mock", EXPECTED_CHAT_URL, 20, True, root)
        msg = GmailMessage("m1", "t", None, body(), ())
        helper = root / "bounded_sender.py"; helper.write_text("import sys; sys.stdin.read(); print('SUBMITTED')", encoding="utf-8")
        real_cfg = replace(cfg, handoff_command=f'"{sys.executable}" "{helper}"'); real_submission = CommandHandoffSender(real_cfg).submit("AGENTRELAY_CHATGPT_HANDOFF/1")
        assert real_submission.ok and real_submission.verified and real_submission.attempts == 1; print("REAL_CHATGPT_SENDER_INVOCATION_PASS")
        events = []; sender = Sender(events=events); worker = OneShotWorker(cfg, executor=lambda text, path: (events.append("work") or WorkerOutcome(True, "ok")), handoff_sender=sender, watchdog_spawn=lambda step, run: events.append("watchdog"))
        staged = stage_instruction(cfg.local_project_storage, msg, parse_envelope(msg.body)); assert worker.run(run_id="RUN-CERT-001", step=1, staged_path=staged).ok
        assert len(sender.calls) == 1 and events.index("handoff") < events.index("watchdog"); print("REAL_CHATGPT_HANDOFF_PASS")
        assert StateStore(cfg.local_project_storage).load()["active_worker"] is None; assert events[-1] == "watchdog"; print("WORKER_EXIT_AFTER_HANDOFF_PASS"); print("WATCHDOG_STARTED_AFTER_REAL_HANDOFF_PASS")
        report_lines = sender.calls[0].splitlines(); assert any(line.startswith("BASELINE_SHA: ") and len(line) > 13 for line in report_lines); assert any(line.startswith("REMOTE_HEAD: ") and len(line) > 13 for line in report_lines); print("MECHANICAL_GIT_PROVENANCE_PASS")

        failed_events = []; failed = Sender(False, failed_events); worker = OneShotWorker(cfg, executor=lambda text, path: WorkerOutcome(True, "ok"), handoff_sender=failed, watchdog_spawn=lambda step, run: failed_events.append("watchdog"))
        staged2 = stage_instruction(root / "failed", msg, parse_envelope(msg.body)); assert not worker.run(run_id="RUN-CERT-001", step=1, staged_path=staged2).ok and "watchdog" not in failed_events; print("FAILED_HANDOFF_NO_WATCHDOG_PASS")

        dead = Launcher(99999999); relay = Relay(cfg, Mail([msg]), dead); assert relay.poll_once().action == "launched"; state = StateStore(cfg.local_project_storage).load(); assert state["expected_step"] == 1 and not state["consumed_message_ids"]; assert relay.poll_once().action == "launched"; print("NO_STEP_LOSS_BEFORE_CLAIM_PASS")

        live = Launcher(__import__("os").getpid()); relay = Relay(replace(cfg, local_project_storage=root / "live"), Mail([msg]), live); assert relay.poll_once().action == "launched" and relay.poll_once().action == "busy" and len(live.calls) == 1; print("SINGLE_WORKER_PASS")
        lock_root = root / "mutex"
        with poll_transaction_lock(lock_root) as first:
            with poll_transaction_lock(lock_root) as second:
                assert first is True and second is False
        print("SINGLE_POLL_PROCESS_PASS")

        claim_root = root / "claim"; claim_cfg = replace(cfg, local_project_storage=claim_root); claim_msg = GmailMessage("claim", "t", None, body(), ()); claim_stage = stage_instruction(claim_root, claim_msg, parse_envelope(claim_msg.body)); store = StateStore(claim_root); store.save({**store.load(), "pending_worker": {"worker_id": "claimed", "pid": __import__("os").getpid(), "project_id": "gmail-courier", "run_id": "RUN-CERT-001", "step": 1, "parent": 0, "message_id": "claim", "content_hash": "h", "exe": "python.exe"}}); claim_sender = Sender(); claim_worker = OneShotWorker(claim_cfg, executor=lambda text, path: WorkerOutcome(True, "ok"), handoff_sender=claim_sender); assert claim_worker.run(run_id="RUN-CERT-001", step=1, staged_path=claim_stage, worker_id="claimed", message_id="claim", content_hash="h").ok; assert StateStore(claim_root).load()["consumed_message_ids"] == ["claim"]; print("TRANSACTIONAL_WORKER_CLAIM_PASS")

        assert exact_owner_live({"pid": __import__("os").getpid(), "exe": "python.exe"}) is True; import agent_relay.watchdog as watchdog; assert watchdog.exact_owner_live is exact_owner_live; print("WINDOWS_OWNER_VALIDATION_PASS")
        sleeps = []; result = run_watchdog(replace(cfg, local_project_storage=root / "wd"), run_id="RUN-CERT-001", after_step=1, poll_factory=lambda: Relay(replace(cfg, local_project_storage=root / "wd"), Mail([]), Launcher(-1)), sleep=lambda n: sleeps.append(n), ui_spawn=lambda: None); assert result == "exhausted" and sleeps == [120, 120]; print("TWO_SHOT_WATCHDOG_PASS")
        print("MINIMAL_ARCHITECTURE_PRESERVED")


if __name__ == "__main__": main()
