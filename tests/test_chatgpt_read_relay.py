from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_relay.chatgpt_read_relay import (
    AssistantMessage,
    ChatGPTReadRelay,
    ChatGPTDOMReader,
    ChatReadError,
    ChatReadNotReady,
    ChatReadReplayConflict,
    ReadResult,
    consume_once,
    parse_outbound_envelope,
    payload_sha256,
)
from gmail_courier.protocol import build_chat_read_correction_prompt, build_chat_read_prompt


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


def envelope_v2(project: str = "GENERIC", work_order: str = "WO-001", payload: dict | None = None) -> str:
    payload = payload or {"instruction": "continue", "value": 42}
    compact = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "\n".join((
        "AGENTRELAY_OUTBOUND/2",
        f"PROJECT_ID={project}",
        f"WORK_ORDER_ID={work_order}",
        "ACTION=EXECUTE",
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
        self.assertIn("AGENTRELAY_OUTBOUND/2", prompt)
        self.assertIn("Do not send Gmail", prompt)
        self.assertIn("Do not calculate or invent a cryptographic hash", prompt)
        self.assertNotIn("GMAIL RESPONSE CONTRACT", prompt)
        self.assertTrue(prompt.isascii())

    def test_correction_prompt_is_short_and_binds_request_identity(self):
        prompt = build_chat_read_correction_prompt(project_id="GENERIC", work_order_id="WO-001")
        self.assertIn("AGENTRELAY_OUTBOUND/2", prompt)
        self.assertIn("PROJECT_ID=GENERIC", prompt)
        self.assertIn("WORK_ORDER_ID=WO-001", prompt)
        self.assertNotIn("PAYLOAD_SHA256", prompt)
        self.assertTrue(prompt.isascii())

    def test_correction_sender_uses_the_same_project_and_work_order(self):
        class FakeResult:
            ok = True
            verified = True

        class FakeSender:
            def __init__(self):
                self.submitted = ""
                self.post_submit_delay = 360
                self.close_after_submit = False
                self.owned_process = None

            def submit(self, prompt):
                self.submitted = prompt
                return FakeResult()

        sender = FakeSender()
        relay = ChatGPTReadRelay(
            root=Path("."),
            project_id="GENERIC",
            chat_url="https://chatgpt.com/c/test",
            work_order_id="WO-001",
            sender_factory=lambda _url: sender,
        )
        self.assertTrue(relay._send_correction())
        self.assertIn("PROJECT_ID=GENERIC", sender.submitted)
        self.assertIn("WORK_ORDER_ID=WO-001", sender.submitted)
        self.assertEqual(sender.post_submit_delay, 0)
        self.assertTrue(sender.close_after_submit)

    def test_valid_envelope_and_canonical_hash(self):
        text = envelope()
        parsed = parse_outbound_envelope(text, project_id="GENERIC")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.work_order_id, "WO-001")
        self.assertEqual(parsed.payload["value"], 42)
        self.assertEqual(parsed.payload_sha256, payload_sha256(parsed.payload))

    def test_v2_envelope_has_no_chat_supplied_hash_but_gets_local_replay_hash(self):
        parsed = parse_outbound_envelope(envelope_v2(), project_id="GENERIC")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.protocol, "AGENTRELAY_OUTBOUND/2")
        self.assertEqual(parsed.payload_sha256, payload_sha256(parsed.payload))

    def test_v2_work_order_is_consumed_once(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            message = AssistantMessage("message-v2", envelope_v2(), "https://chatgpt.com/c/test")
            parsed = parse_outbound_envelope(message.text, project_id="GENERIC")
            assert parsed is not None
            self.assertEqual(consume_once(root, message, parsed).event, "chat_work_order_received")
            self.assertEqual(consume_once(root, message, parsed).event, "chat_work_order_duplicate")

    def test_read_relay_performs_at_most_one_correction(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            relay = ChatGPTReadRelay(
                root=root,
                project_id="GENERIC",
                chat_url="https://chatgpt.com/c/test",
                work_order_id="WO-001",
            )
            first = ReadResult("chat_no_work_order", "ordinary assistant prose")
            second = ReadResult("chat_work_order_received", "corrected envelope")
            relay._read_once_without_correction = lambda: first if not hasattr(relay, "_test_second") else second
            relay._send_correction = lambda: setattr(relay, "_test_second", True) or True
            result = relay.read_once()
            self.assertEqual(result.event, "chat_work_order_received")

    def test_read_relay_reports_repair_failure_after_second_bad_response(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            relay = ChatGPTReadRelay(
                root=root,
                project_id="GENERIC",
                chat_url="https://chatgpt.com/c/test",
                work_order_id="WO-001",
            )
            relay._read_once_without_correction = lambda: ReadResult("chat_no_work_order", "still ordinary prose")
            calls = []
            relay._send_correction = lambda: calls.append(True) or True
            result = relay.read_once()
            self.assertEqual(result.event, "chat_repair_failed")
            self.assertEqual(len(calls), 1)

    def test_wait_for_work_order_probes_without_correction_until_reply_is_ready(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            page = FakePage([])
            closed = []

            class FakeSender:
                debug_port = 9222
                cdp_connect_timeout_ms = 1000
                owned_process = None
                launches = 0

                def _launch(self):
                    self.launches += 1

                def _close_session_page(self, value):
                    closed.append(value)

            sender = FakeSender()

            class FakeContext:
                pages = [page]

            class FakeBrowser:
                contexts = [FakeContext()]

            class FakeChromium:
                def connect_over_cdp(self, *_args, **_kwargs):
                    return FakeBrowser()

            class FakePlaywright:
                chromium = FakeChromium()

            class FakePlaywrightContext:
                def __enter__(self):
                    return FakePlaywright()

                def __exit__(self, *_args):
                    return False

            pending = ChatReadNotReady("assistant is still generating")
            response = envelope_v2(work_order="WO-001")
            results = iter((pending, AssistantMessage("message-1", response, page.url)))

            class FakeReader:
                instances = 0

                def __init__(self, *_args, **_kwargs):
                    FakeReader.instances += 1

                def latest_completed_assistant(self):
                    value = next(results)
                    if isinstance(value, Exception):
                        raise value
                    return value

            relay = ChatGPTReadRelay(
                root=root,
                project_id="GENERIC",
                chat_url=page.url,
                work_order_id="WO-001",
                close_session_page=False,
                sender_factory=lambda _url: sender,
            )
            with patch("playwright.sync_api.sync_playwright", return_value=FakePlaywrightContext()), patch(
                "agent_relay.chatgpt_read_relay.ChatGPTDOMReader", FakeReader
            ), patch("agent_relay.chatgpt_read_relay.time.sleep") as sleep:
                result = relay.wait_for_work_order(max_seconds=10, interval_seconds=2)
            self.assertEqual(result.event, "chat_work_order_received")
            sleep.assert_called_once()
            self.assertEqual(sender.launches, 1)
            self.assertEqual(FakeReader.instances, 1)
            self.assertEqual(closed, [page])

    def test_unknown_version_fails_closed(self):
        with self.assertRaises(ChatReadError):
            parse_outbound_envelope(envelope().replace("AGENTRELAY_OUTBOUND/1", "AGENTRELAY_OUTBOUND/3"), project_id="GENERIC")

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
