from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from chat_courier.browser import ChatAuthenticationRequired, ChatComposerNotReady, PreSubmissionError
from chat_courier.cli import main
from chat_courier.owner import OwnerRecord
from chat_courier.queue import QueueStatus


class CliPreflightTests(unittest.TestCase):
    def request_directory(self, root: Path) -> Path:
        (root / "message.txt").write_text("message", encoding="utf-8")
        (root / "request.json").write_text(json.dumps({
            "version": 1,
            "project_id": "P",
            "request_id": "P-1",
            "chat_url": "https://chatgpt.com/c/x",
        }), encoding="utf-8")
        return root

    def test_preflight_reports_ready_without_running_submit(self):
        class Session:
            profile = Path(r"C:\Courier\profile")
            profile_directory = "Default"
            def __init__(self, request, *, prepare_only=False):
                self.request = request
                self.prepare_only = prepare_only
            def __enter__(self): return self
            def __exit__(self, *_): return False

        with tempfile.TemporaryDirectory() as value, patch("chat_courier.cli.ChatSession", Session), patch("chat_courier.model._load_registry", return_value={"P": "https://chatgpt.com/c/x"}), patch("chat_courier.queue.runtime_root", return_value=Path(value) / "runtime"):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["preflight", str(self.request_directory(Path(value)))])
        event = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(event["event"], "chat_ready")
        self.assertRegex(event["courier_build_id"], r"^[0-9a-f]{16}$")

    def test_preflight_reports_auth_required_without_submit(self):
        class Session:
            def __init__(self, request, *, prepare_only=False):
                self.prepare_only = prepare_only
            def __enter__(self):
                raise ChatAuthenticationRequired("login required")
            def __exit__(self, *_): return False

        with tempfile.TemporaryDirectory() as value, patch("chat_courier.cli.ChatSession", Session), patch("chat_courier.model._load_registry", return_value={"P": "https://chatgpt.com/c/x"}), patch("chat_courier.queue.runtime_root", return_value=Path(value) / "runtime"):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["preflight", str(self.request_directory(Path(value)))])
        event = json.loads(output.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(event["event"], "chat_auth_required")
        self.assertEqual(event["phase"], "preflight")

    def test_preflight_rejects_visible_but_noneditable_composer(self):
        class Session:
            def __init__(self, request, *, prepare_only=False): pass
            def __enter__(self):
                raise ChatComposerNotReady("not editable", {"visible": True, "editable": False, "ready": False})
            def __exit__(self, *_): return False

        with tempfile.TemporaryDirectory() as value, patch("chat_courier.cli.ChatSession", Session), patch("chat_courier.model._load_registry", return_value={"P": "https://chatgpt.com/c/x"}), patch("chat_courier.queue.runtime_root", return_value=Path(value) / "runtime"):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["preflight", str(self.request_directory(Path(value)))])
        event = json.loads(output.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(event["event"], "chat_composer_not_ready")
        self.assertFalse(event["composer_snapshot"]["ready"])

    def test_pre_submit_attachment_failure_writes_actionable_receipt(self):
        class Session:
            profile = Path(r"C:\Courier\profile")
            attached_existing = False
            def __init__(self, request, *, recovery=False, status_callback=None):
                self.request = request
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def submit(self, *_):
                raise PreSubmissionError("upload stalled", failure_stage="attachment_upload_stalled", diagnostic_path=self.request.directory / "transport_diagnostic.json")

        with tempfile.TemporaryDirectory() as value, patch("chat_courier.cli.ChatSession", Session), patch("chat_courier.model._load_registry", return_value={"P": "https://chatgpt.com/c/x"}), patch("chat_courier.queue.runtime_root", return_value=Path(value) / "runtime"):
            root = self.request_directory(Path(value)); output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["run", str(root)])
            receipt = json.loads((root / "receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 1)
        self.assertEqual(receipt["state"], "submission_not_started")
        self.assertEqual(receipt["failure_stage"], "attachment_upload_stalled")
        self.assertEqual(receipt["next_action"], "agent_decision_required")
        self.assertTrue(receipt["safe_to_retry_same_request"])

    def test_queue_timeout_does_not_construct_a_browser_session(self):
        class Queue:
            def __init__(self, request): pass
            def join(self, **_): return QueueStatus("joined", ticket="ticket-1", position=1)
            def poll(self): return QueueStatus("timeout", ticket="ticket-1", position=1)
            def complete(self): raise AssertionError("timeout must not complete a browser turn")

        class Session:
            def __init__(self, *_args, **_kwargs): raise AssertionError("queue timeout must not start Chrome")

        with tempfile.TemporaryDirectory() as value, patch("chat_courier.cli.CourierQueue", Queue), patch("chat_courier.cli.ChatSession", Session), patch("chat_courier.model._load_registry", return_value={"P": "https://chatgpt.com/c/x"}):
            root = self.request_directory(Path(value)); output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["run", str(root)])
            receipt = json.loads((root / "receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 1)
        self.assertEqual(receipt["state"], "queue_timeout")
        self.assertTrue(receipt["safe_to_retry_same_request"])
        self.assertFalse(receipt["browser_started"])

    def test_pre_browser_keyboard_interrupt_is_structured_and_releases_ticket(self):
        class Queue:
            completed = False
            def __init__(self, request): pass
            def join(self, **_): return QueueStatus("joined", ticket="ticket-1", position=1)
            def poll(self): return QueueStatus("turn_acquired", ticket="ticket-1", position=1)
            def complete(self): Queue.completed = True
            def mark_recovery_required(self, *_): raise AssertionError("pre-browser interruption must release the ticket")

        with tempfile.TemporaryDirectory() as value, patch("chat_courier.cli.CourierQueue", Queue), patch("chat_courier.cli._run_after_queue", side_effect=KeyboardInterrupt), patch("chat_courier.cli.read_owner", return_value=None), patch("chat_courier.model._load_registry", return_value={"P": "https://chatgpt.com/c/x"}):
            root = self.request_directory(Path(value)); output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["run", str(root)])
            receipt = json.loads((root / "receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 130)
        self.assertTrue(Queue.completed)
        self.assertEqual(receipt["state"], "courier_interrupted")
        self.assertEqual(receipt["interruption_stage"], "pre_browser")
        self.assertTrue(receipt["safe_to_retry_same_request"])
        self.assertIn('"event": "courier_interrupted"', output.getvalue())

    def test_dead_starting_owner_without_browser_is_safe_pre_browser_recovery(self):
        from chat_courier.cli import _safe_pre_browser_turn_recovery
        owner = OwnerRecord("P", "P-1", 9999, "nonce", "starting", "now")
        previous = {"state": "submission_intent"}
        request = type("Request", (), {"project_id": "P", "request_id": "P-1"})()
        with patch("chat_courier.cli.read_owner", return_value=owner), patch("chat_courier.cli.process_alive", return_value=False):
            self.assertTrue(_safe_pre_browser_turn_recovery(previous, request))
