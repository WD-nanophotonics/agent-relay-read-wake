from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from chat_courier.model import ValidationError, load_request
from chat_courier.protocol import BEGIN_RESPONSE, END_RESPONSE, REPLY_PROTOCOL, build_prompt, parse_reply


class ModelProtocolTests(unittest.TestCase):
    def make_request(self, root: Path, **changes):
        (root / "message.txt").write_text("Please prepare the next task.", encoding="utf-8")
        raw = {"version": 1, "project_id": "TEST", "request_id": "TEST-001", "chat_url": "https://chatgpt.com/c/abc", "message_file": "message.txt"}
        raw.update(changes)
        (root / "request.json").write_text(json.dumps(raw), encoding="utf-8")
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

    def test_prose_is_not_a_reply(self):
        with tempfile.TemporaryDirectory() as value:
            self.assertIsNone(parse_reply("ordinary assistant prose", self.make_request(Path(value))))

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

    def test_attachment_cannot_escape_directory(self):
        with tempfile.TemporaryDirectory() as value:
            with self.assertRaises(ValidationError): self.make_request(Path(value), attachments=["../secret.txt"])
