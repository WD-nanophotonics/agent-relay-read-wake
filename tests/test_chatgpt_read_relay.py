from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from agent_relay.chatgpt_read_relay import (
    AssistantMessage,
    ChatGPTDOMReader,
    ChatReadError,
    ChatReadNotReady,
    ChatReadReplayConflict,
    consume_once,
    parse_outbound_envelope,
    payload_sha256,
)
from gmail_courier.protocol import build_chat_read_prompt


def envelope(project: str = "GENERIC", work_order: str = "WO-001", payload: dict | None = None) -> str:
    payload = payload or {"instruction": "continue", "value": 42}
    compact = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(compact.encode("utf-8")).hexdigest()
    return "\n".join((
        "AGENTRELAY_OUTBOUND/1",
        f"PROJECT_ID={project}",
        f"WORK_ORDER_ID={work_order}",
        "ACTION=EXECUTE",
        f"PAYLOAD_SHA256={digest}",
        "BEGIN_PAYLOAD",
        compact,
        "END_PAYLOAD",
    ))


class FakeMessage:
    def __init__(self, identity: str, text: str, *, streaming: bool = False):
        self.identity = identity
        self.text = text
        self.streaming = streaming

    def get_attribute(self, name: str):
        if name == "data-message-id":
            return self.identity
        if name == "data-is-streaming":
            return "true" if self.streaming else "false"
        return None

    def inner_text(self, timeout=None):
        return self.text


class FakeItems:
    def __init__(self, items):
        self.items = items

    def count(self):
        return len(self.items)

    def nth(self, index):
        return self.items[index]


class FakePage:
    def __init__(self, messages, *, stop=False):
        self.messages = messages
        self.stop = stop
        self.url = "https://chatgpt.com/c/test"

    def locator(self, selector):
        if "data-message-author-role" in selector:
            return FakeItems(self.messages)
        if "Stop" in selector:
            return FakeItems([object()] if self.stop else [])
        raise AssertionError(selector)


class ChatGPTReadRelayTests(unittest.TestCase):
    def test_official_chat_only_prompt_has_no_gmail_contract(self):
        prompt = build_chat_read_prompt(
            "Return the current status.",
            project_id="GENERIC",
            work_order_id="WO-001",
        )
        self.assertIn("AGENTRELAY_OUTBOUND/1", prompt)
        self.assertIn("Do not send Gmail", prompt)
        self.assertNotIn("GMAIL RESPONSE CONTRACT", prompt)
        self.assertTrue(prompt.isascii())

    def test_valid_envelope_and_canonical_hash(self):
        text = envelope()
        parsed = parse_outbound_envelope(text, project_id="GENERIC")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.work_order_id, "WO-001")
        self.assertEqual(parsed.payload["value"], 42)
        self.assertEqual(parsed.payload_sha256, payload_sha256(parsed.payload))

    def test_invalid_version_fails_closed(self):
        with self.assertRaises(ChatReadError):
            parse_outbound_envelope(envelope().replace("AGENTRELAY_OUTBOUND/1", "AGENTRELAY_OUTBOUND/2"), project_id="GENERIC")

    def test_wrong_project_id_fails_closed(self):
        with self.assertRaisesRegex(ChatReadError, "project_id"):
            parse_outbound_envelope(envelope(project="OTHER"), project_id="GENERIC")

    def test_malformed_json_fails_closed(self):
        text = envelope().replace('{"instruction":"continue","value":42}', '{"instruction":')
        with self.assertRaises(ChatReadError):
            parse_outbound_envelope(text, project_id="GENERIC")

    def test_missing_terminator_fails_closed(self):
        with self.assertRaisesRegex(ChatReadError, "END_PAYLOAD"):
            parse_outbound_envelope(envelope().replace("\nEND_PAYLOAD", ""), project_id="GENERIC")

    def test_incorrect_hash_fails_closed(self):
        bad = envelope().replace(payload_sha256({"instruction": "continue", "value": 42}), "0" * 64)
        with self.assertRaisesRegex(ChatReadError, "SHA256"):
            parse_outbound_envelope(bad, project_id="GENERIC")

    def test_ordinary_prose_has_no_work_order(self):
        self.assertIsNone(parse_outbound_envelope("The work is complete; no machine response follows.", project_id="GENERIC"))

    def test_duplicate_work_order_replay_is_rejected_as_duplicate(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            message = AssistantMessage("message-1", envelope(), "https://chatgpt.com/c/test")
            parsed = parse_outbound_envelope(message.text, project_id="GENERIC")
            assert parsed is not None
            first = consume_once(root, message, parsed)
            second = consume_once(root, message, parsed)
            self.assertEqual(first.event, "chat_work_order_received")
            self.assertEqual(second.event, "chat_work_order_duplicate")

    def test_reused_work_order_with_changed_payload_fails_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            first_message = AssistantMessage("message-1", envelope(payload={"value": 1}), "https://chatgpt.com/c/test")
            first = parse_outbound_envelope(first_message.text, project_id="GENERIC")
            assert first is not None
            consume_once(root, first_message, first)
            second_message = AssistantMessage("message-2", envelope(payload={"value": 2}), "https://chatgpt.com/c/test")
            second = parse_outbound_envelope(second_message.text, project_id="GENERIC")
            assert second is not None
            with self.assertRaises(ChatReadReplayConflict):
                consume_once(root, second_message, second)

    def test_incomplete_assistant_message_is_not_ready(self):
        page = FakePage([FakeMessage("message-1", envelope(), streaming=True)])
        with self.assertRaises(ChatReadNotReady):
            ChatGPTDOMReader(page, stability_wait_seconds=0).latest_completed_assistant()

    def test_newest_completed_assistant_message_is_selected(self):
        page = FakePage([
            FakeMessage("old", envelope(work_order="WO-OLD")),
            FakeMessage("new", envelope(work_order="WO-NEW")),
        ])
        selected = ChatGPTDOMReader(page, stability_wait_seconds=0).latest_completed_assistant()
        self.assertIsNotNone(selected)
        assert selected is not None
        parsed = parse_outbound_envelope(selected.text, project_id="GENERIC")
        self.assertEqual(selected.identity, "new")
        self.assertEqual(parsed.work_order_id if parsed else None, "WO-NEW")

    def test_newest_ordinary_message_does_not_fall_back_to_old_envelope(self):
        page = FakePage([FakeMessage("old", envelope()), FakeMessage("new", "ordinary answer")])
        selected = ChatGPTDOMReader(page, stability_wait_seconds=0).latest_completed_assistant()
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertIsNone(parse_outbound_envelope(selected.text, project_id="GENERIC"))

    def test_restart_reloads_replay_receipt(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            message = AssistantMessage("message-1", envelope(), "https://chatgpt.com/c/test")
            parsed = parse_outbound_envelope(message.text, project_id="GENERIC")
            assert parsed is not None
            consume_once(root, message, parsed)
            receipt = json.loads((root / "chatgpt" / "outbound_receipts.json").read_text(encoding="utf-8"))
            self.assertIn("WO-001", receipt["records"])
            parsed_again = parse_outbound_envelope(message.text, project_id="GENERIC")
            assert parsed_again is not None
            self.assertEqual(consume_once(root, message, parsed_again).event, "chat_work_order_duplicate")


if __name__ == "__main__":
    unittest.main()
