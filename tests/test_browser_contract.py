from __future__ import annotations

import unittest

from chat_courier.browser import ChatDom


class BrowserContractTests(unittest.TestCase):
    def test_document_upload_selector_excludes_image_only_inputs(self):
        # This protects the real regression: ChatGPT exposes upload-files,
        # upload-photos, and upload-camera inputs in the same document.
        source = ChatDom.upload.__code__.co_consts
        self.assertIn("#upload-files, input[type='file']:not([accept^='image/'])", source)

    def test_attachment_submit_prefers_explicit_send_button(self):
        self.assertIn("button[data-testid='send-button']", ChatDom.submit_composer.__code__.co_consts[1])

    def test_confirmation_fallback_is_exposed(self):
        self.assertTrue(callable(ChatDom.submission_visible))
