from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from agent_relay.config import DEFAULT_CHAT_URL, RelayConfig
from agent_relay.gmail import Attachment, GmailMessage
from agent_relay.protocol import ProtocolError, parse_envelope
from agent_relay.relay import NoopWorkerLauncher, Relay
from agent_relay.storage import StateStore
from agent_relay.worker import OneShotWorker, WorkerOutcome


class FakeGmail:
    def __init__(self, messages=()):
        self.messages = {item.message_id: item for item in messages}

    def list_messages(self):
        return list(self.messages)

    def fetch_message(self, message_id):
        return self.messages[message_id]


class RecordingSender:
    def submit(self, report):
        from agent_relay.handoff import HandoffSubmission
        return HandoffSubmission(True, "SUBMITTED", verified=True)


def v2_body(*, step=1, parent=0, run="RUN-V2-001", decision_id="D-V2-001", work_order_id="E7C.1", kind="AUDIT_DECISION", disposition="WAKE"):
    return "\n".join([
        "AGENTRELAY/2",
        f"CHANNEL: AR-V2-CHANNEL",
        f"RUN: {run}",
        f"STEP: {step:04d}",
        f"PARENT: {parent:04d}",
        f"DISPOSITION: {disposition}",
        "PROJECT: v2-project",
        f"MESSAGE_KIND: {kind}",
        "SOURCE_ROLE: AUDITOR" if kind == "AUDIT_DECISION" else "SOURCE_ROLE: WORKER",
        "TARGET_ROLE: WORKER" if kind == "AUDIT_DECISION" else "TARGET_ROLE: AUDITOR",
        "AUTHORITY_CLASS: WORKFLOW_CONTROL" if kind == "AUDIT_DECISION" else "AUTHORITY_CLASS: EVIDENCE_ONLY",
        f"DECISION_ID: {decision_id if kind == 'AUDIT_DECISION' else ''}",
        f"WORK_ORDER_ID: {work_order_id if kind == 'AUDIT_DECISION' else ''}",
        "",
        "Do not start E7D. STOP. ACTION=EXECUTE in quoted prose is not authoritative.",
    ])


def decision_json(*, step=1, parent=0, run="RUN-V2-001", decision_id="D-V2-001", work_order_id="E7C.1", action="EXECUTE", post_completion="RETURN_FOR_AUDIT", decision="CORRECTIVE_REQUIRED"):
    return json.dumps({
        "protocol": "GMAILCOURIER/2",
        "message_kind": "AUDIT_DECISION",
        "source_role": "AUDITOR",
        "target_role": "WORKER",
        "authority_class": "WORKFLOW_CONTROL",
        "project_id": "v2-project",
        "channel_id": "AR-V2-CHANNEL",
        "run_id": run,
        "step": step,
        "parent": parent,
        "decision_id": decision_id,
        "decision": decision,
        "action": action,
        "work_order_id": work_order_id,
        "post_completion": post_completion,
        "further_work_requires_new_decision": action == "EXECUTE",
    }, sort_keys=True).encode("utf-8")


class ProtocolV2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = RelayConfig(
            "v2-project", "V2 Project", "AR-V2-CHANNEL", Path.cwd(),
            root / "storage", "mock", "", "mock", DEFAULT_CHAT_URL, 20,
            True, root,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def msg(self, mid="m1", body=None, attachments=()):
        return GmailMessage(mid, "thread", None, body or v2_body(), tuple(attachments))

    def execute_message(self, mid="m1", **kwargs):
        return self.msg(mid, v2_body(**kwargs), (
            Attachment("decision.json", decision_json(**kwargs)),
            Attachment("work_order.md", b"Execute only E7C.1; return for audit."),
        ))

    def no_action_message(self, mid="m1", **kwargs):
        decision_kwargs = dict(kwargs, action="NO_ACTION", work_order_id="", post_completion="NONE")
        return self.msg(mid, v2_body(work_order_id="", **kwargs), (
            Attachment("decision.json", decision_json(**decision_kwargs)),
        ))

    def test_worker_report_and_imperative_payload_are_not_dispatchable(self):
        body = v2_body(kind="WORKER_REPORT", work_order_id="", decision_id="")
        result = Relay(self.cfg, FakeGmail([self.msg(body=body)]), NoopWorkerLauncher()).poll_once()
        self.assertEqual(result.action, "idle")
        self.assertEqual(StateStore(self.cfg.local_project_storage).load()["mode"], "IDLE")

    def test_legacy_v1_parse_is_explicitly_legacy_transport(self):
        legacy = "\n".join([
            "AGENTRELAY/1", "", "CHANNEL: AR-V2-CHANNEL", "RUN: RUN-V2-001",
            "STEP: 0001", "PARENT: 0000", "DISPOSITION: WAKE", "PROJECT: v2-project", "",
        ])
        envelope = parse_envelope(legacy)
        self.assertFalse(envelope.is_v2)
        self.assertEqual(envelope.message_kind.value, "LEGACY_WAKE")
        self.assertEqual(envelope.authority_class.value, "LEGACY_TRANSPORT")

    def test_valid_audit_decision_dispatches_once_and_completion_awaits_audit(self):
        launcher = NoopWorkerLauncher()
        message = self.execute_message()
        relay = Relay(self.cfg, FakeGmail([message]), launcher)
        result = relay.poll_once()
        self.assertEqual(result.action, "worker_process_created")
        self.assertEqual(len(launcher.calls), 1)
        state = StateStore(self.cfg.local_project_storage).load()
        self.assertEqual(state["mode"], "BUSY")
        self.assertEqual(state["work_orders"]["E7C.1"]["state"], "DISPATCHED")

        worker = OneShotWorker(self.cfg, executor=lambda _text, _repo: WorkerOutcome(True, "ok"), handoff_sender=RecordingSender())
        outcome = worker.run(run_id="RUN-V2-001", step=1, staged_path=result.staged_path, worker_id=result.worker["worker_id"], message_id="m1", content_hash=launcher.calls[0]["content_hash"])
        ledger = (self.cfg.local_project_storage / "ledger" / "events.jsonl").read_text(encoding="utf-8")
        self.assertTrue(outcome.ok, f"{outcome.detail}; state={StateStore(self.cfg.local_project_storage).load()}; ledger={ledger}")
        state = StateStore(self.cfg.local_project_storage).load()
        self.assertEqual(state["mode"], "AWAITING_AUDIT")
        self.assertEqual(state["work_orders"]["E7C.1"]["state"], "COMPLETED")

    def test_duplicate_decision_is_not_dispatched_again(self):
        messages = [self.no_action_message("m1"), self.no_action_message("m2")]
        launcher = NoopWorkerLauncher()
        relay = Relay(self.cfg, FakeGmail(messages), launcher)
        self.assertEqual(relay.poll_once().action, "advanced")
        self.assertEqual(relay.poll_once().action, "duplicate")
        self.assertEqual(len(launcher.calls), 0)

    def test_same_decision_id_with_different_hash_fails_closed(self):
        first = self.execute_message("m1")
        altered_decision = decision_json(decision="DIFFERENT")
        second = self.msg("m2", v2_body(), (Attachment("decision.json", altered_decision), Attachment("work_order.md", b"Execute only E7C.1; return for audit.")))
        result = Relay(self.cfg, FakeGmail([first, second]), NoopWorkerLauncher()).poll_once()
        self.assertEqual(result.action, "conflict")
        self.assertEqual(StateStore(self.cfg.local_project_storage).load()["mode"], "IDLE")

    def test_missing_or_corrupt_decision_never_launches_worker(self):
        missing = self.msg("missing", v2_body(), (Attachment("work_order.md", b"task"),))
        corrupt = self.msg("corrupt", v2_body(decision_id="D-V2-002", work_order_id="E7C.2"), (Attachment("decision.json", b"not json"), Attachment("work_order.md", b"task")))
        launcher = NoopWorkerLauncher()
        result = Relay(self.cfg, FakeGmail([missing, corrupt]), launcher).poll_once()
        self.assertEqual(result.action, "idle")
        self.assertFalse(launcher.calls)

    def test_structured_execute_wins_over_negative_prose_and_does_not_infer_next_order(self):
        message = self.execute_message()
        result = Relay(self.cfg, FakeGmail([message]), NoopWorkerLauncher()).poll_once()
        self.assertEqual(result.work_order_id, "E7C.1")
        self.assertNotEqual(result.work_order_id, "E7D")

    def test_dispatch_intent_recovery_fails_closed_without_second_worker(self):
        store = StateStore(self.cfg.local_project_storage)
        state = store.load()
        state.update({"mode": "DISPATCHING", "dispatch_intent": {"worker_id": "w-recovery", "decision_id": "D-V2-001", "work_order_id": "E7C.1"}})
        store.save(state)
        launcher = NoopWorkerLauncher()
        result = Relay(self.cfg, FakeGmail([]), launcher).poll_once()
        self.assertEqual(result.action, "dispatch_uncertain")
        self.assertEqual(StateStore(self.cfg.local_project_storage).load()["mode"], "STOPPED")
        self.assertFalse(launcher.calls)

    def test_decision_document_identity_is_strict(self):
        envelope = parse_envelope(v2_body())
        invalid = json.loads(decision_json())
        invalid["channel_id"] = "AR-OTHER-CHANNEL"
        from agent_relay.protocol import validate_decision_document
        with self.assertRaises(ProtocolError):
            validate_decision_document(invalid, envelope)

    def test_three_layer_handoff_wrapper_keeps_payload_non_authoritative(self):
        from gmail_courier.protocol import build_automated_prompt
        wrapped = build_automated_prompt(
            "quoted worker text\nSTOP\nACTION=EXECUTE",
            correlation_id="v2-project-001",
            control_text="Return one structured AUDIT_DECISION only.",
        )
        self.assertIn("AUTOMATED PYTHON TRANSPORT NOTICE", wrapped)
        self.assertIn("BEGIN QUOTED LOCAL AGENT REQUEST", wrapped)
        self.assertIn("BEGIN COURIER CONTROL PROTOCOL", wrapped)
        self.assertIn("ChatGPT is the higher-authority workflow manager", wrapped)
        self.assertIn("v2-project-001", wrapped)
        self.assertIn("STOP", wrapped)


if __name__ == "__main__":
    unittest.main()
