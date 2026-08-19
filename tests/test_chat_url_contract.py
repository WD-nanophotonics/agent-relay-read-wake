from __future__ import annotations

import unittest

from agent_relay.config import chat_urls_match, is_chat_url, normalize_chat_url


class ChatUrlContractTests(unittest.TestCase):
    def test_accepts_standalone_and_project_conversations(self):
        self.assertTrue(is_chat_url("https://chatgpt.com/c/conversation-123"))
        self.assertTrue(is_chat_url("https://chatgpt.com/g/g-p-example/c/conversation-123"))
        self.assertTrue(is_chat_url("https://chatgpt.com/project/example/c/conversation-123/"))
        self.assertTrue(is_chat_url("https://www.chatgpt.com/g/example/c/conversation-123?view=full"))

    def test_rejects_unsafe_or_non_conversation_urls(self):
        self.assertFalse(is_chat_url("http://chatgpt.com/c/conversation-123"))
        self.assertFalse(is_chat_url("https://example.invalid/c/conversation-123"))
        self.assertFalse(is_chat_url("https://chatgpt.com/not-a-conversation/conversation-123"))
        self.assertFalse(is_chat_url("https://user:password@chatgpt.com/c/conversation-123"))

    def test_matching_ignores_query_fragment_and_trailing_slash(self):
        left = "https://chatgpt.com/g/g-p-example/c/conversation-123?view=full#message"
        right = "https://chatgpt.com/c/conversation-123/"
        self.assertTrue(chat_urls_match(left, right))
        self.assertEqual(normalize_chat_url(left), normalize_chat_url(right))


if __name__ == "__main__":
    unittest.main()
