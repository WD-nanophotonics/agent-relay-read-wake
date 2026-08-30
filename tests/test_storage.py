from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from chat_courier.model import ValidationError, load_request
from chat_courier.storage import (load_receipt, load_response_capture, load_response_cursor,
                                  receipt, save_response, save_response_capture,
                                  save_response_cursor, submission_count)
from chat_courier.cli import (_safe_pre_browser_turn_recovery, _submission_confirmed,
                              resend_once_command, rollover_target_command)


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

    def test_response_cursor_and_raw_capture_are_hash_bound(self):
        with tempfile.TemporaryDirectory() as value:
            request = self.request(Path(value))
            save_response_cursor(request, {"old-a", "old-b"})
            self.assertEqual(load_response_cursor(request), {"old-a", "old-b"})
            saved = save_response_capture(request, identity="new-c", index=3, text="reply without an envelope")
            capture, text = load_response_capture(request)
            self.assertEqual(capture["raw_sha256"], saved["raw_sha256"])
            self.assertEqual(text, "reply without an envelope")
            (Path(value) / "response.raw.txt").write_text("drift", encoding="utf-8")
            with self.assertRaises(ValidationError): load_response_capture(request)

    def test_only_explicit_post_send_states_enter_read_only_recovery(self):
        self.assertFalse(_submission_confirmed({"state": "submission_intent"}))
        self.assertFalse(_submission_confirmed({"state": "browser_error"}))
        self.assertFalse(_submission_confirmed({"state": "courier_error"}))
        self.assertFalse(_submission_confirmed({"state": "submission_not_started"}))
        self.assertTrue(_submission_confirmed({"state": "request_submitted"}))
        self.assertTrue(_submission_confirmed({"state": "submission_unconfirmed"}))
        self.assertTrue(_submission_confirmed({"state": "response_captured"}))

    def test_same_request_resend_is_bounded_to_second_submission(self):
        with tempfile.TemporaryDirectory() as value:
            request = self.request(Path(value))
            event_path = request.directory / "events.jsonl"
            event_path.write_text(json.dumps({"event": "request_submitted", "project_id": "P",
                                              "request_id": "P-1"}) + "\n", encoding="utf-8")
            args = type("Args", (), {"request_directory": str(request.directory)})()
            with patch("chat_courier.model._load_registry", return_value={"P": "https://chatgpt.com/c/x"}), \
                    patch("chat_courier.cli.run_command", return_value=0) as run:
                self.assertEqual(resend_once_command(args), 0)
                self.assertTrue(args.resend_once)
                run.assert_called_once_with(args)
            with event_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"event": "request_submitted", "project_id": "P",
                                         "request_id": "P-1"}) + "\n")
            self.assertEqual(submission_count(request), 2)
            with patch("chat_courier.model._load_registry", return_value={"P": "https://chatgpt.com/c/x"}), \
                    patch("chat_courier.cli.run_command") as run:
                self.assertEqual(resend_once_command(args), 2)
                run.assert_not_called()

    def test_resend_archives_a_previously_accepted_ui_error(self):
        with tempfile.TemporaryDirectory() as value:
            request = self.request(Path(value))
            (request.directory / "events.jsonl").write_text(
                json.dumps({"event": "request_submitted", "project_id": "P",
                            "request_id": "P-1"}) + "\n", encoding="utf-8")
            (request.directory / "response.txt").write_text(
                "This content can't be shown", encoding="utf-8")
            (request.directory / "response.raw.txt").write_text(
                "This content can't be shown", encoding="utf-8")
            (request.directory / "response-capture.json").write_text("{}", encoding="utf-8")
            receipt(request, "response_received", "legacy false positive")
            args = type("Args", (), {"request_directory": str(request.directory)})()
            with patch("chat_courier.model._load_registry", return_value={"P": "https://chatgpt.com/c/x"}), \
                    patch("chat_courier.cli.run_command", return_value=0) as run:
                self.assertEqual(resend_once_command(args), 0)
                self.assertTrue(args.resend_once)
                run.assert_called_once_with(args)
            self.assertFalse((request.directory / "response.txt").exists())
            self.assertEqual(
                (request.directory / "attempt-1-response.txt").read_text(encoding="utf-8"),
                "This content can't be shown",
            )

    def test_resend_archive_never_leaves_current_capture_when_prior_name_exists(self):
        with tempfile.TemporaryDirectory() as value:
            request = self.request(Path(value))
            (request.directory / "events.jsonl").write_text(
                json.dumps({"event": "request_submitted", "project_id": "P",
                            "request_id": "P-1"}) + "\n", encoding="utf-8")
            for name in ("response.txt", "response.raw.txt", "response-capture.json"):
                (request.directory / f"attempt-1-{name}").write_text("old", encoding="utf-8")
                (request.directory / name).write_text("current", encoding="utf-8")
            args = type("Args", (), {"request_directory": str(request.directory)})()
            with patch("chat_courier.model._load_registry", return_value={"P": "https://chatgpt.com/c/x"}), \
                    patch("chat_courier.cli.run_command", return_value=0):
                self.assertEqual(resend_once_command(args), 0)
            for name in ("response.txt", "response.raw.txt", "response-capture.json"):
                self.assertFalse((request.directory / name).exists())
                self.assertEqual(
                    (request.directory / f"attempt-1-2-{name}").read_text(encoding="utf-8"),
                    "current",
                )

    def test_user_confirmed_target_rollover_preserves_old_state_and_resets_active_count(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            (root / "message.txt").write_text("immutable report", encoding="utf-8")
            (root / "request.json").write_text(json.dumps({
                "version": 1, "project_id": "P", "request_id": "P-1",
            }), encoding="utf-8")
            with patch("chat_courier.model._load_registry", return_value={"P": "https://chatgpt.com/c/old"}):
                old_request = load_request(root)
            receipt(old_request, "response_received", "old target exhausted")
            (root / "response.txt").write_text(
                "You've reached the maximum length for this conversation, but you can keep talking by starting a new chat.",
                encoding="utf-8",
            )
            (root / "events.jsonl").write_text(
                "\n".join(json.dumps({"event": "request_submitted", "project_id": "P",
                                       "request_id": "P-1"}) for _ in range(2)) + "\n",
                encoding="utf-8",
            )
            args = type("Args", (), {"request_directory": str(root)})()
            with patch("chat_courier.model._load_registry", return_value={"P": "https://chatgpt.com/c/new"}), \
                    patch("chat_courier.cli.run_command", return_value=0) as run:
                self.assertEqual(rollover_target_command(args), 0)
                run.assert_called_once_with(args)
                active_request = load_request(root)
            self.assertEqual(submission_count(active_request), 0)
            self.assertEqual(submission_count(active_request, total=True), 2)
            self.assertTrue((root / "target-generation-1" / "receipt.json").is_file())
            self.assertTrue((root / "target-generation-1" / "response.txt").is_file())

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
