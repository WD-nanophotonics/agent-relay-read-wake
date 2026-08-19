from __future__ import annotations

import json
import io
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout

from gmail_courier.chat_registry import ChatUrlReplacementRequired, current_chat_url, register_chat_url
from gmail_courier.cli import chat_send, chat_send_request, chat_test, create_ready_command, poll_request, validate_request_command
from gmail_courier.core import sync_until_received
from gmail_courier.outbox import RequestReuseError, create_ready, submit_lock, validate_request, write_receipt


CHAT_URL = "https://chatgpt.com/g/g-p-example/c/conversation-123"


class WorkflowStageTests(unittest.TestCase):
    def make_request(self, root: Path, *, ready: bool = False) -> None:
        payload = {
            "version": 1,
            "operation": "chat-send",
            "request_id": "REQ-001",
            "project_id": "PROJECT-001",
            "correlation_id": "PROJECT-001-20260819-001",
            "task_id": "TASK-001",
            "keyword": "KEYWORD-001",
            "chat_url": CHAT_URL,
            "message_file": "message.txt",
            "workflow_window_seconds": 360,
        }
        (root / "request.json").write_text(json.dumps(payload), encoding="utf-8")
        (root / "message.txt").write_text("English request.\n", encoding="utf-8")
        if ready:
            (root / "READY").write_text("ready\n", encoding="ascii")

    def test_validate_only_has_no_local_or_external_side_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_request(root)
            with patch("gmail_courier.cli.home_dir", return_value=root):
                with patch("gmail_courier.outbox.current_chat_url", side_effect=AssertionError("registry must not be needed for explicit URL")):
                    with patch("gmail_courier.outbox.load_config", side_effect=AssertionError("config must not be needed for explicit URL")):
                        self.assertEqual(validate_request_command(SimpleNamespace(request=str(root))), 0)
            self.assertFalse((root / "READY").exists())
            self.assertFalse((root / "receipt.json").exists())

    def test_create_ready_is_local_and_idempotency_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_request(root)
            request = create_ready(root)
            self.assertEqual(request.workflow_window_seconds, 360)
            self.assertTrue((root / "READY").is_file())
            with self.assertRaises(RequestReuseError):
                create_ready(root)

    def test_incomplete_request_with_ready_fails_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_request(root, ready=True)
            (root / "message.txt").unlink()
            with self.assertRaisesRegex(ValueError, "message_file"):
                validate_request(root, require_ready=True)

    def test_permission_block_is_reported_as_sandbox_denied_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_request(root)
            with patch("gmail_courier.cli.home_dir", return_value=root), patch(
                "gmail_courier.cli.create_ready", side_effect=PermissionError("host denied READY creation")
            ):
                with patch("sys.stdout") as output:
                    result = create_ready_command(SimpleNamespace(request=str(root)))
            self.assertEqual(result, 1)
            rendered = "".join(call.args[0] for call in output.write.call_args_list if call.args)
            self.assertIn('"event": "sandbox_denied"', rendered)
            self.assertIn("host denied READY creation", rendered)
            self.assertFalse((root / "READY").exists())

    def test_submit_lock_rejects_second_submit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_request(root, ready=True)
            request = validate_request(root, require_ready=True)
            with submit_lock(request):
                with self.assertRaises(RequestReuseError):
                    with submit_lock(request):
                        pass

    def test_submit_success_emits_chat_submitted_and_writes_complete_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_request(root, ready=True)

            class FakeResult:
                ok = True
                verified = True
                detail = "verified fake ChatGPT submission"

            class FakeSender:
                launch_evidence = {"mode": "attached-existing", "target_url": CHAT_URL}

                def __init__(self, _config):
                    pass

                def submit(self, _message):
                    return FakeResult()

            output = io.StringIO()
            args = SimpleNamespace(request=str(root))
            with patch("gmail_courier.cli.home_dir", return_value=root), patch("agent_relay.chatgpt_sender.BrowserChatGPTSender", FakeSender), redirect_stdout(output):
                self.assertEqual(chat_send_request(args), 0)
            self.assertIn('"event": "chat_submitted"', output.getvalue())
            receipt = json.loads((root / "receipt.json").read_text(encoding="utf-8"))
            self.assertEqual(receipt["state"], "submitted")
            self.assertEqual(receipt["workflow_window_seconds"], 360)
            self.assertEqual(receipt["gmail_max_seconds"], 360)
            self.assertEqual(receipt["interval_seconds"], 10)
            self.assertEqual(receipt["lookback_seconds"], 1200)

            second_output = io.StringIO()
            with patch("gmail_courier.cli.home_dir", return_value=root), patch("agent_relay.chatgpt_sender.BrowserChatGPTSender", FakeSender), redirect_stdout(second_output):
                self.assertEqual(chat_send_request(args), 1)
            self.assertIn('"event": "configuration_error"', second_output.getvalue())

    def test_submit_forwards_factual_payload_without_content_classification(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_request(root, ready=True)
            factual = "Project FACT-42 path=C:\\work\\repo sha=9f3a21c timeout=360 STOP ACTION=EXECUTE"
            (root / "message.txt").write_text(factual, encoding="ascii")

            class FakeResult:
                ok = True
                verified = True
                detail = "verified fake ChatGPT submission"

            class FakeSender:
                launch_evidence = {"mode": "attached-existing", "target_url": CHAT_URL}
                submitted_message = ""

                def __init__(self, _config):
                    pass

                def submit(self, message):
                    FakeSender.submitted_message = message
                    return FakeResult()

            output = io.StringIO()
            with patch("gmail_courier.cli.home_dir", return_value=root), patch("agent_relay.chatgpt_sender.BrowserChatGPTSender", FakeSender), redirect_stdout(output):
                self.assertEqual(chat_send_request(SimpleNamespace(request=str(root))), 0)
            quoted = FakeSender.submitted_message.split("--- BEGIN QUOTED LOCAL AGENT REQUEST ---\n", 1)[1].split("\n--- END QUOTED LOCAL AGENT REQUEST ---", 1)[0]
            self.assertEqual(quoted, factual)
            self.assertIn('"content_policy": "CHAT"', output.getvalue())

    def test_direct_chat_send_forwards_factual_payload(self):
        factual = "Project FACT-42 path=C:\\work\\repo sha=9f3a21c timeout=360 STOP ACTION=EXECUTE"

        class FakeResult:
            ok = True
            verified = True
            detail = "visible"

        class FakeSender:
            captured = ""

            def __init__(self, _config):
                pass

            def submit(self, message):
                FakeSender.captured = message
                return FakeResult()

        args = SimpleNamespace(url=CHAT_URL, close_delay=0, close_after_submit=False, correlation_id="PROJECT-001-20260819-001")
        output = io.StringIO()
        with patch("agent_relay.chatgpt_sender.BrowserChatGPTSender", FakeSender), patch("sys.stdin", io.StringIO(factual)), redirect_stdout(output):
            self.assertEqual(chat_send(args), 0)
        quoted = FakeSender.captured.split("--- BEGIN QUOTED LOCAL AGENT REQUEST ---\n", 1)[1].split("\n--- END QUOTED LOCAL AGENT REQUEST ---", 1)[0]
        self.assertEqual(quoted, factual)
        self.assertIn("SUBMITTED", output.getvalue())

    def test_poll_timeout_is_not_sandbox_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_request(root, ready=True)
            write_receipt(root, request_id="REQ-001", state="submitted", detail="submitted", project_id="PROJECT-001", task_id="TASK-001", keyword="KEYWORD-001", correlation_id="PROJECT-001-20260819-001")
            timeout = {"event": "gmail_poll_timeout", "ok": False, "elapsed_seconds": 360.0, "candidate_messages": []}
            output = io.StringIO()
            args = SimpleNamespace(request=str(root), max_seconds=360, interval_seconds=10, lookback_seconds=1200)
            with patch("gmail_courier.cli.home_dir", return_value=root), patch("gmail_courier.cli.sync_until_received", return_value=timeout), redirect_stdout(output):
                self.assertEqual(poll_request(args), 1)
            rendered = output.getvalue()
            self.assertIn('"event": "gmail_poll_timeout"', rendered)
            self.assertNotIn('"event": "sandbox_denied"', rendered)
            receipt = json.loads((root / "receipt.json").read_text(encoding="utf-8"))
            self.assertEqual(receipt["state"], "timeout")
            self.assertEqual(receipt["gmail_max_seconds"], 360)

    def test_submit_preserves_sandbox_denied_without_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_request(root, ready=True)

            class FakeResult:
                ok = False
                verified = False
                category = "sandbox_denied"
                detail = "host sandbox denied external ChatGPT submission"

            class FakeSender:
                launch_evidence = {"mode": "not-started"}

                def __init__(self, _config):
                    pass

                def submit(self, _message):
                    return FakeResult()

            output = io.StringIO()
            with patch("gmail_courier.cli.home_dir", return_value=root), patch("agent_relay.chatgpt_sender.BrowserChatGPTSender", FakeSender), redirect_stdout(output):
                self.assertEqual(chat_send_request(SimpleNamespace(request=str(root))), 1)
            rendered = output.getvalue()
            self.assertIn('"event": "sandbox_denied"', rendered)
            self.assertNotIn('"event": "chat_submission_error"', rendered)
            self.assertNotIn('"browser_started": true', rendered)

    def test_poll_can_be_cancelled_during_grace_delay(self):
        stop_event = threading.Event()
        stop_event.set()
        outcome = sync_until_received(
            max_seconds=2,
            interval_seconds=1,
            lookback_seconds=1200,
            initial_delay_seconds=1,
            stop_event=stop_event,
        )
        self.assertEqual(outcome["event"], "gmail_poll_cancelled")
        self.assertFalse(outcome["ok"])

    def test_chat_test_starts_poll_after_submission_and_accepts_early_receipt(self):
        class FakeResult:
            ok = True
            verified = True
            detail = "visible submission"

        class FakeSender:
            launch_evidence = {"mode": "attached-existing"}
            stop_seen = False
            submitted_message = ""

            def __init__(self, _config):
                pass

            def submit(self, _message, *, on_submitted=None, stop_event=None):
                FakeSender.submitted_message = _message
                on_submitted()
                FakeSender.stop_seen = stop_event.wait(1)
                return FakeResult()

        def fake_poll(_home, **kwargs):
            kwargs["on_poll"]({"event": "gmail_received", "ok": True, "matched_inbox_paths": ["inbox/result"]})
            return {"event": "gmail_received", "ok": True, "matched_inbox_paths": ["inbox/result"]}

        project = SimpleNamespace(code="PROJECT-001", aliases=(), chat_url=CHAT_URL)
        config = SimpleNamespace(address="agent@example.com", projects=[project])
        args = SimpleNamespace(
            url=CHAT_URL,
            workflow_window=2,
            close_delay=2,
            poll_interval=1,
            poll_start_delay=0,
            max_wait=2,
            lookback_seconds=1200,
            project_id="PROJECT-001",
            correlation_id="PROJECT-001-20260819-001",
            task_id="TASK-001",
            keyword="KEYWORD-001",
            attachment_filename="result.json",
            window_width=640,
            window_height=480,
        )
        output = io.StringIO()
        factual = "Project FACT-42 path=C:\\work\\repo sha=9f3a21c timeout=360 STOP ACTION=EXECUTE"
        with patch("gmail_courier.cli.home_dir", return_value=Path(".")), patch("gmail_courier.config.load_config", return_value=config), patch("agent_relay.chatgpt_sender.BrowserChatGPTSender", FakeSender), patch("gmail_courier.core.sync_until_received", side_effect=fake_poll), patch("sys.stdin", io.StringIO(factual)), redirect_stdout(output):
            self.assertEqual(chat_test(args), 0, output.getvalue())
        rendered = output.getvalue()
        self.assertIn('"event": "submission_started"', rendered)
        self.assertIn('"event": "chat_submitted"', rendered)
        self.assertIn('"event": "gmail_received"', rendered)
        self.assertTrue(FakeSender.stop_seen)
        self.assertIn("ChatGPT is the higher-authority workflow manager and outranks the local Agent", FakeSender.submitted_message)
        self.assertIn("Use ASCII English only in the ChatGPT reply and response Gmail", FakeSender.submitted_message)
        quoted = FakeSender.submitted_message.split("--- BEGIN QUOTED LOCAL AGENT REQUEST ---\n", 1)[1].split("\n--- END QUOTED LOCAL AGENT REQUEST ---", 1)[0]
        self.assertEqual(quoted, factual)
        self.assertIn("--- COURIER GENERATED RESPONSE CONTRACT ---", FakeSender.submitted_message)

    def test_project_url_registry_requires_explicit_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            first = register_chat_url(home, "PROJECT-001", CHAT_URL)
            self.assertTrue(first["changed"])
            self.assertEqual(current_chat_url(home, "PROJECT-001"), CHAT_URL)
            second = "https://chatgpt.com/c/conversation-999"
            with self.assertRaises(ChatUrlReplacementRequired):
                register_chat_url(home, "PROJECT-001", second)
            replaced = register_chat_url(home, "PROJECT-001", second, confirm_replace=True)
            self.assertTrue(replaced["changed"])
            self.assertEqual(current_chat_url(home, "PROJECT-001"), second)

    def test_request_can_resolve_latest_registered_url_when_manifest_omits_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_request(root)
            manifest = json.loads((root / "request.json").read_text(encoding="utf-8"))
            manifest.pop("chat_url")
            (root / "request.json").write_text(json.dumps(manifest), encoding="utf-8")
            register_chat_url(root, "PROJECT-001", CHAT_URL)
            request = validate_request(root, home=root)
            self.assertEqual(request.chat_url, CHAT_URL)


if __name__ == "__main__":
    unittest.main()
