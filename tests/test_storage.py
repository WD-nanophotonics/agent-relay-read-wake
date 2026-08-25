from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from chat_courier.model import ValidationError, load_request
from chat_courier.storage import load_receipt, receipt, save_response
from chat_courier.cli import _safe_pre_browser_turn_recovery, _submission_confirmed


class StorageTests(unittest.TestCase):
    def request(self, root: Path):
        (root / "message.txt").write_text("message", encoding="utf-8")
        (root / "request.json").write_text(json.dumps({"version": 1, "project_id": "P", "request_id": "P-1", "chat_url": "https://chatgpt.com/c/x"}), encoding="utf-8")
        with patch("chat_courier.model._load_registry", return_value={"P": "https://chatgpt.com/c/x"}):
            return load_request(root)

    def test_receipt_and_response_are_atomic_and_reusable(self):
        with tempfile.TemporaryDirectory() as value:
            request = self.request(Path(value)); receipt(request, "response_received", "ok")
            self.assertEqual(load_receipt(request)["state"], "response_received")
            self.assertEqual(save_response(request, "result").read_text(encoding="utf-8"), "result")

    def test_changed_request_is_rejected_after_receipt(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value); request = self.request(root); receipt(request, "request_submitted", "sent")
            (root / "message.txt").write_text("changed", encoding="utf-8")
            with self.assertRaises(ValidationError): load_receipt(load_request(root))

    def test_only_explicit_post_send_states_enter_read_only_recovery(self):
        self.assertFalse(_submission_confirmed({"state": "submission_intent"}))
        self.assertFalse(_submission_confirmed({"state": "browser_error"}))
        self.assertFalse(_submission_confirmed({"state": "courier_error"}))
        self.assertFalse(_submission_confirmed({"state": "submission_not_started"}))
        self.assertTrue(_submission_confirmed({"state": "request_submitted"}))
        self.assertTrue(_submission_confirmed({"state": "submission_unconfirmed"}))

    def test_queue_provenance_survives_final_receipt_transition(self):
        with tempfile.TemporaryDirectory() as value:
            request = self.request(Path(value))
            receipt(request, "queue_turn_acquired", "turn", queue_ticket="ticket-1", queue_waited_seconds=12, execution_started_at=100)
            receipt(request, "response_received", "done", response_path="response.txt")
            final = load_receipt(request)
        self.assertEqual(final["queue_ticket"], "ticket-1")
        self.assertEqual(final["queue_waited_seconds"], 12)
        self.assertEqual(final["execution_started_at"], 100)

    def test_queue_turn_without_an_owner_is_safe_to_recover_before_submission(self):
        with patch("chat_courier.cli.read_owner", return_value=None):
            self.assertTrue(_safe_pre_browser_turn_recovery({"state": "queue_turn_acquired"}))
        with patch("chat_courier.cli.read_owner", return_value=object()):
            self.assertFalse(_safe_pre_browser_turn_recovery({"state": "queue_turn_acquired"}))
        with patch("chat_courier.cli.read_owner", return_value=None):
            self.assertTrue(_safe_pre_browser_turn_recovery({"state": "courier_interrupted", "interruption_stage": "pre_browser"}))
        self.assertFalse(_safe_pre_browser_turn_recovery({"state": "submission_intent"}))
