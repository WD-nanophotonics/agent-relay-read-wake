from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent_relay.chatgpt_sender import BrowserChatGPTSender


class ChatGPTSenderHealthTests(unittest.TestCase):
    def make_sender(self) -> BrowserChatGPTSender:
        return BrowserChatGPTSender(SimpleNamespace(chat_url="https://chatgpt.com/c/conversation-123"))

    def test_cdp_websocket_probe_requires_a_websocket_endpoint(self):
        sender = self.make_sender()
        self.assertTrue(sender.close_session_page)
        self.assertFalse(sender._cdp_websocket_ready({"Browser": "Chrome"}))
        self.assertFalse(sender._cdp_websocket_ready({"webSocketDebuggerUrl": "http://127.0.0.1:9222/devtools/browser/x"}))

    def test_cdp_websocket_probe_accepts_http_101(self):
        sender = self.make_sender()
        fake_socket = MagicMock()
        fake_socket.recv.side_effect = [b"HTTP/1.1 101 Switching Protocols\r\n", b"\r\n"]
        fake_socket.__enter__.return_value = fake_socket
        version = {"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/x"}
        with patch("agent_relay.chatgpt_sender.socket.create_connection", return_value=fake_socket) as connect:
            self.assertTrue(sender._cdp_websocket_ready(version))
        connect.assert_called_once_with(("127.0.0.1", 9222), timeout=2.0)
        fake_socket.sendall.assert_called_once()

    def test_existing_http_endpoint_with_dead_websocket_is_skipped(self):
        sender = self.make_sender()
        version = {"Browser": "Chrome", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/x"}
        pages = [{"url": "https://chatgpt.com/c/conversation-123"}]
        with patch.object(sender, "_cdp_at", side_effect=lambda _port, path: version if path == "/json/version" else pages), \
             patch.object(sender, "_cdp_websocket_ready", return_value=False), \
             patch.object(sender, "_matching_chrome_process", return_value={"ProcessId": 123}):
            self.assertIsNone(sender._find_existing_target())
        self.assertFalse(sender.launch_evidence["existing_cdp_probes"][0]["websocket_ready"])

    def test_healthy_cdp_without_target_page_is_not_reused_for_playwright(self):
        sender = self.make_sender()
        version = {"Browser": "Chrome", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/x"}
        pages = [{"url": "https://chatgpt.com/c/another-conversation"}]
        with patch.object(sender, "_cdp_at", side_effect=lambda _port, path: version if path == "/json/version" else pages), \
             patch.object(sender, "_cdp_websocket_ready", return_value=True), \
             patch.object(sender, "_matching_chrome_process", return_value=None):
            result = sender._find_existing_target()
        self.assertIsNone(result)
        self.assertEqual(sender.launch_evidence["existing_cdp_probes"][0]["page_count"], 1)

    def test_prepare_chat_page_rejects_wrong_url_before_composer(self):
        sender = self.make_sender()
        page = MagicMock()
        page.url = "https://chatgpt.com/c/wrong-conversation"
        with self.assertRaisesRegex(RuntimeError, "URL mismatch"):
            sender._prepare_chat_page(page)

    def test_submission_confirmation_requires_rendered_user_turn(self):
        sender = self.make_sender()
        page = MagicMock()
        turns = MagicMock()
        turns.count.return_value = 1
        turns.nth.return_value.inner_text.return_value = "actual user turn"
        page.locator.return_value = turns
        self.assertFalse(sender._submitted_user_turn_visible(page, "text still in composer"))
        turns.nth.return_value.inner_text.return_value = "actual user turn with marker"
        self.assertTrue(sender._submitted_user_turn_visible(page, "marker"))

    def test_page_close_failure_uses_only_matching_cdp_target(self):
        sender = self.make_sender()
        page = MagicMock()
        page.close.side_effect = RuntimeError("detached target")
        sender._cdp_at = MagicMock(return_value=[
            {"type": "page", "id": "target-1", "url": sender.url},
            {"type": "page", "id": "target-2", "url": "https://example.com/"},
        ])
        with patch("agent_relay.chatgpt_sender.urllib.request.urlopen") as urlopen:
            sender._close_session_page(page)
        urlopen.assert_called_once_with(
            "http://127.0.0.1:9222/json/close/target-1", timeout=2
        )

    def test_missing_page_object_still_closes_configured_target(self):
        sender = self.make_sender()
        sender._cdp_at = MagicMock(return_value=[
            {"type": "page", "id": "target-1", "url": sender.url},
        ])
        with patch("agent_relay.chatgpt_sender.urllib.request.urlopen") as urlopen:
            sender._close_session_page(None)
        urlopen.assert_called_once_with(
            "http://127.0.0.1:9222/json/close/target-1", timeout=2
        )


if __name__ == "__main__":
    unittest.main()
