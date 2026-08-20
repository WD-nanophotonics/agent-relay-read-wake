from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from chat_courier.model import ValidationError, load_request
from chat_courier.storage import load_receipt, receipt, save_response


class StorageTests(unittest.TestCase):
    def request(self, root: Path):
        (root / "message.txt").write_text("message", encoding="utf-8")
        (root / "request.json").write_text(json.dumps({"version": 1, "project_id": "P", "request_id": "P-1", "chat_url": "https://chatgpt.com/c/x"}), encoding="utf-8")
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
