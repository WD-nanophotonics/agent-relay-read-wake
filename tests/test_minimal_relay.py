from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from dataclasses import replace

from agent_relay.config import RelayConfig
from agent_relay.config import EXPECTED_CHAT_URL
from agent_relay.gmail import Attachment, GmailMessage
from agent_relay.relay import NoopWorkerLauncher, Relay
from agent_relay.storage import StateStore, stage_instruction
from agent_relay.watchdog import run_watchdog
from agent_relay.worker import OneShotWorker, WorkerOutcome
from agent_relay.protocol import ProtocolError, parse_envelope


def envelope(step=1, parent=0, disposition="WAKE", run="RUN-TEST-001"):
    return f"AGENTRELAY/1\n\nCHANNEL: AR-GMAILCOURIER-A1R7P\nRUN: {run}\nSTEP: {step:04d}\nPARENT: {parent:04d}\nDISPOSITION: {disposition}\nPROJECT: gmail-courier\n\nTask {step}"


class FakeGmail:
    def __init__(self, messages=()):
        self.messages = {m.message_id: m for m in messages}
        self.list_calls = 0
        self.fetch_calls = []
    def list_messages(self):
        self.list_calls += 1
        return list(self.messages)
    def fetch_message(self, message_id):
        self.fetch_calls.append(message_id)
        return self.messages[message_id]


class RecordingSender:
    def __init__(self, ok=True):
        self.ok = ok; self.calls = []
    def submit(self, report):
        from agent_relay.handoff import HandoffSubmission
        self.calls.append(report)
        return HandoffSubmission(self.ok, "SUBMITTED" if self.ok else "rejected", verified=self.ok)


class MinimalRelayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = RelayConfig("gmail-courier", "Gmail Courier", "AR-GMAILCOURIER-A1R7P", Path.cwd(), root / "storage", "mock", "", "mock", EXPECTED_CHAT_URL, 20, True, root)
    def tearDown(self):
        self.tmp.cleanup()
    def msg(self, mid="m1", step=1, parent=0, disposition="WAKE", attachments=()):
        return GmailMessage(mid, "t", None, envelope(step, parent, disposition), tuple(attachments))
    def relay(self, messages, launcher=None):
        return Relay(self.cfg, FakeGmail(messages), launcher or NoopWorkerLauncher())
    def test_no_mail_is_one_cycle(self):
        r = self.relay([]); self.assertEqual(r.poll_once().action, "idle"); self.assertEqual(r.gmail.list_calls, 1)
    def test_valid_wake_stages_and_launches_once(self):
        l = NoopWorkerLauncher(); result = self.relay([self.msg()], l).poll_once(); self.assertEqual(result.action, "launched"); self.assertEqual(len(l.calls), 1)
        self.assertTrue((result.staged_path / "manifest.json").exists())
    def test_duplicate_message_is_ignored(self):
        l = NoopWorkerLauncher(); r = self.relay([self.msg()], l); self.assertEqual(r.poll_once().action, "launched"); self.assertEqual(r.poll_once().action, "busy"); self.assertEqual(len(l.calls), 1)
    def test_old_step_is_ignored(self):
        s = StateStore(self.cfg.local_project_storage); st = s.load(); st.update({"current_run": "RUN-TEST-001", "expected_step": 2, "expected_parent": 1}); s.save(st)
        self.assertEqual(self.relay([self.msg(step=1)]).poll_once().action, "idle")
    def test_future_step_is_deferred_and_not_consumed(self):
        r = self.relay([self.msg(step=3)]); self.assertEqual(r.poll_once().action, "idle"); self.assertEqual(StateStore(self.cfg.local_project_storage).load()["consumed_message_ids"], [])
    def test_conflict_fails_closed(self):
        s = StateStore(self.cfg.local_project_storage); st = s.load(); st["current_run"] = "RUN-TEST-001"; st["logical_hashes"]["RUN-TEST-001:0001"] = "different"; s.save(st)
        self.assertEqual(self.relay([self.msg()]).poll_once().action, "conflict")
    def test_no_action_advances_without_worker(self):
        l = NoopWorkerLauncher(); r = self.relay([self.msg(disposition="NO_ACTION")], l); self.assertEqual(r.poll_once().action, "advanced"); self.assertFalse(l.calls); self.assertEqual(StateStore(self.cfg.local_project_storage).load()["expected_step"], 2)
    def test_human_required_does_not_wake(self):
        l = NoopWorkerLauncher(); self.assertEqual(self.relay([self.msg(disposition="HUMAN_REQUIRED")], l).poll_once().action, "human_required"); self.assertFalse(l.calls)
    def test_wrong_binding_does_not_consume(self):
        m = GmailMessage("x", "t", None, envelope(), ()); r = Relay(replace(self.cfg, channel_id="OTHER"), FakeGmail([m]), NoopWorkerLauncher())
        self.assertEqual(r.poll_once().action, "idle")
    def test_attachment_manifest_hash(self):
        a = Attachment("a.txt", b"abc"); result = self.relay([self.msg(attachments=(a,))]).poll_once(); manifest = json.loads((result.staged_path / "manifest.json").read_text()); self.assertEqual(manifest["attachments"][0]["sha256"], "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
    def test_state_is_atomic_json(self):
        self.relay([self.msg(disposition="NO_ACTION")]).poll_once(); self.assertIsInstance(json.loads((self.cfg.local_project_storage / "state.json").read_text()), dict)
    def test_single_active_worker_barrier(self):
        l = NoopWorkerLauncher(); r = self.relay([self.msg()], l); r.poll_once(); self.assertEqual(r.poll_once().action, "busy")
    def test_stale_owner_is_cleared_exactly(self):
        s = StateStore(self.cfg.local_project_storage); st = s.load(); st.update({"mode": "BUSY", "active_worker": {"worker_id": "dead", "pid": 99999999}}); s.save(st)
        self.assertEqual(self.relay([]).poll_once().action, "idle"); self.assertIsNone(s.load()["active_worker"])
    def test_stop_prevents_poll(self):
        s = StateStore(self.cfg.local_project_storage); st = s.load(); st.update({"stop_requested": True, "mode": "STOPPED"}); s.save(st); r = self.relay([self.msg()]); self.assertEqual(r.poll_once().action, "stopped"); self.assertEqual(r.gmail.list_calls, 0)
    def test_worker_claim_and_exit(self):
        staged = stage_instruction(self.cfg.local_project_storage, self.msg(), __import__("agent_relay.protocol", fromlist=["parse_envelope"]).parse_envelope(self.msg().body)); worker = OneShotWorker(self.cfg, executor=lambda text, path: WorkerOutcome(True, "ok"), handoff_sender=RecordingSender()); self.assertTrue(worker.run(run_id="RUN-TEST-001", step=1, staged_path=staged).ok); self.assertEqual(StateStore(self.cfg.local_project_storage).load()["mode"], "IDLE")
    def test_worker_failure_still_exits(self):
        staged = stage_instruction(self.cfg.local_project_storage, self.msg(), __import__("agent_relay.protocol", fromlist=["parse_envelope"]).parse_envelope(self.msg().body)); worker = OneShotWorker(self.cfg, executor=lambda text, path: WorkerOutcome(False, "failed"), handoff_sender=RecordingSender()); self.assertFalse(worker.run(run_id="RUN-TEST-001", step=1, staged_path=staged).ok); self.assertIsNone(StateStore(self.cfg.local_project_storage).load()["active_worker"])
    def test_watchdog_dedupes(self):
        sleeps = []; factory = lambda: self.relay([]); result = run_watchdog(self.cfg, run_id="RUN-TEST-001", after_step=1, poll_factory=factory, sleep=lambda n: sleeps.append(n)); self.assertEqual(result, "exhausted"); self.assertEqual(sleeps, [120, 120]); self.assertEqual(run_watchdog(self.cfg, run_id="RUN-TEST-001", after_step=1, poll_factory=factory, sleep=lambda n: None), "deduped")
    def test_watchdog_stops(self):
        s = StateStore(self.cfg.local_project_storage); st = s.load(); st["stop_requested"] = True; s.save(st); self.assertEqual(run_watchdog(self.cfg, run_id="RUN-TEST-001", after_step=1, poll_factory=lambda: self.relay([]), sleep=lambda n: None), "stopped")
    def test_watchdog_active_worker_never_polls(self):
        s = StateStore(self.cfg.local_project_storage); st = s.load(); st["active_worker"] = {"worker_id": "self", "pid": __import__("os").getpid()}; s.save(st); called = []; self.assertEqual(run_watchdog(self.cfg, run_id="RUN-TEST-001", after_step=1, poll_factory=lambda: called.append(1), sleep=lambda n: None), "active"); self.assertFalse(called)
    def test_consecutive_steps_without_daemon(self):
        l = NoopWorkerLauncher(); r = self.relay([self.msg(disposition="NO_ACTION")], l); self.assertEqual(r.poll_once().action, "advanced"); self.assertEqual(StateStore(self.cfg.local_project_storage).load()["mode"], "IDLE")
    def test_list_fetch_once_per_poll(self):
        g = FakeGmail([self.msg()]); r = Relay(self.cfg, g, NoopWorkerLauncher()); r.poll_once(); self.assertEqual(g.list_calls, 1); self.assertEqual(g.fetch_calls, ["m1"])
    def test_corrupt_state_is_rejected(self):
        self.cfg.local_project_storage.mkdir(parents=True, exist_ok=True); (self.cfg.local_project_storage / "state.json").write_text("{bad", encoding="utf-8")
        with self.assertRaises(ValueError): StateStore(self.cfg.local_project_storage).load()
    def test_protocol_version_is_strict(self):
        with self.assertRaises(ProtocolError): parse_envelope(self.msg().body.replace("AGENTRELAY/1", "AGENTRELAY/9"))
    def test_protocol_disposition_is_strict(self):
        with self.assertRaises(ProtocolError): parse_envelope(self.msg(disposition="MAYBE").body)


if __name__ == "__main__":
    unittest.main()
