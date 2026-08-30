from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from chat_courier.model import ACTIVE_SETUP_BUDGET_SECONDS, CALLER_GRACE_SECONDS, DEFAULT_QUEUE_WAIT_SECONDS, DEFAULT_WINDOW_SECONDS, ValidationError, confirm_url_registration, load_request, minimum_caller_window_seconds, propose_url_registration
from chat_courier.protocol import BEGIN_RESPONSE, END_RESPONSE, REPLY_PROTOCOL, build_prompt, parse_reply


class ModelProtocolTests(unittest.TestCase):
    def make_request(self, root: Path, **changes):
        (root / "message.txt").write_text("Please prepare the next task.", encoding="utf-8")
        raw = {"version": 1, "project_id": "TEST", "request_id": "TEST-001", "chat_url": "https://chatgpt.com/c/abc", "message_file": "message.txt"}
        raw.update(changes)
        (root / "request.json").write_text(json.dumps(raw), encoding="utf-8")
        with patch("chat_courier.model._load_registry", return_value={"TEST": "https://chatgpt.com/c/abc"}):
            return load_request(root)

    def test_valid_reply_and_utf8_payload(self):
        with tempfile.TemporaryDirectory() as value:
            request = self.make_request(Path(value))
            text = f"{REPLY_PROTOCOL}\nPROJECT_ID=TEST\nREQUEST_ID=TEST-001\n{BEGIN_RESPONSE}\n你好\n{END_RESPONSE}"
            self.assertEqual(parse_reply(text, request).body, "你好")

    def test_wrong_identity_fails_closed(self):
        with tempfile.TemporaryDirectory() as value:
            request = self.make_request(Path(value))
            text = f"{REPLY_PROTOCOL}\nPROJECT_ID=OTHER\nREQUEST_ID=TEST-001\n{BEGIN_RESPONSE}\nbody\n{END_RESPONSE}"
            with self.assertRaises(ValidationError): parse_reply(text, request)

    def test_unwrapped_prose_is_bound_by_the_local_capture(self):
        with tempfile.TemporaryDirectory() as value:
            self.assertEqual(parse_reply("ordinary assistant prose", self.make_request(Path(value))).body,
                             "ordinary assistant prose")

    def test_missing_terminator_fails_closed(self):
        with tempfile.TemporaryDirectory() as value:
            request = self.make_request(Path(value))
            with self.assertRaises(ValidationError):
                parse_reply(f"{REPLY_PROTOCOL}\nPROJECT_ID=TEST\nREQUEST_ID=TEST-001\n{BEGIN_RESPONSE}\nbody", request)

    def test_prompt_keeps_agent_request_and_preferences_separate(self):
        with tempfile.TemporaryDirectory() as value:
            request = self.make_request(Path(value), task_difficulty="hard", instruction_level="manual_book")
            prompt = build_prompt(request)
            self.assertIn("Please prepare the next task.", prompt)
            self.assertIn("somewhat more difficult", prompt)
            self.assertIn("manual-book-level", prompt)
            self.assertIn("REQUEST_ID=TEST-001", prompt)

    def test_mephc_closeout_prompt_bootstraps_machine_contract_in_a_fresh_chat(self):
        with tempfile.TemporaryDirectory() as value:
            request = self.make_request(Path(value), flow_schema="mephc-fixed-closeout-v2")
            prompt = build_prompt(request)
            self.assertIn("MEPHC THIN FLOW REPLY CONTRACT", prompt)
            self.assertIn("NEXT_WORK_ORDER_ID=", prompt)
            self.assertIn("WORK_ORDER_CONTRACT_JSON=", prompt)
            self.assertIn("mephc-science-work-order-v1", prompt)

    def test_generic_prompt_does_not_receive_mephc_contract(self):
        with tempfile.TemporaryDirectory() as value:
            prompt = build_prompt(self.make_request(Path(value)))
            self.assertNotIn("MEPHC THIN FLOW REPLY CONTRACT", prompt)

    def test_attachment_cannot_escape_directory(self):
        with tempfile.TemporaryDirectory() as value:
            with self.assertRaises(ValidationError): self.make_request(Path(value), attachments=["../secret.txt"])

    def test_default_workflow_window_is_ten_minutes(self):
        with tempfile.TemporaryDirectory() as value:
            self.assertEqual(DEFAULT_WINDOW_SECONDS, 600)
            self.assertEqual(self.make_request(Path(value)).workflow_window_seconds, 600)
            self.assertEqual(DEFAULT_QUEUE_WAIT_SECONDS, 3600)
            self.assertEqual(self.make_request(Path(value)).queue_wait_seconds, 3600)
            self.assertEqual(minimum_caller_window_seconds(3600, 600), 4260 + ACTIVE_SETUP_BUDGET_SECONDS)
            self.assertEqual(CALLER_GRACE_SECONDS, 60)

    def test_queue_wait_window_is_validated_and_part_of_request_identity(self):
        with tempfile.TemporaryDirectory() as value:
            request = self.make_request(Path(value), queue_wait_seconds=120)
            self.assertEqual(request.queue_wait_seconds, 120)
            with self.assertRaises(ValidationError):
                self.make_request(Path(value), queue_wait_seconds=0)

    def test_new_url_requires_separate_confirmation(self):
        with tempfile.TemporaryDirectory() as value, patch("chat_courier.model.runtime_root", return_value=Path(value)):
            proposed = propose_url_registration("TEST", "https://chatgpt.com/c/new")
            self.assertEqual(proposed["state"], "confirmation_required")
            self.assertFalse((Path(value) / "chat_urls.json").exists())
            confirmed = confirm_url_registration("TEST", proposed["confirmation_id"], "user_direct")
            self.assertEqual(confirmed["url"], "https://chatgpt.com/c/new")

    def test_parallel_project_registration_preserves_both_pending_records(self):
        with tempfile.TemporaryDirectory() as value, patch("chat_courier.model.runtime_root", return_value=Path(value)):
            gate = threading.Barrier(2); results = []
            def propose(project: str, url: str) -> None:
                gate.wait(); results.append(propose_url_registration(project, url))
            first = threading.Thread(target=propose, args=("ALPHA", "https://chatgpt.com/c/alpha"))
            second = threading.Thread(target=propose, args=("BETA", "https://chatgpt.com/c/beta"))
            first.start(); second.start(); first.join(); second.join()
            pending = json.loads((Path(value) / "pending_chat_url_registrations.json").read_text(encoding="utf-8"))
        self.assertEqual(len(results), 2)
        self.assertEqual(set(pending), {"ALPHA", "BETA"})

    def test_request_cannot_override_registered_url(self):
        with tempfile.TemporaryDirectory() as value:
            with self.assertRaises(ValidationError):
                self.make_request(Path(value), chat_url="https://chatgpt.com/c/other")
