from __future__ import annotations

from pathlib import Path
import inspect
import json
import tempfile
import unittest
from unittest.mock import patch

from chat_courier.browser import ChatDom, ChatSession, PreSubmissionError, ProfileConfigurationError, SubmissionUnconfirmed, validate_profile_path
from chat_courier.model import conversation_id_from_url


class BrowserContractTests(unittest.TestCase):
    def test_document_upload_selector_excludes_image_only_inputs(self):
        # This protects the real regression: ChatGPT exposes upload-files,
        # upload-photos, and upload-camera inputs in the same document.
        source = ChatDom.upload.__code__.co_consts
        self.assertIn("#upload-files, input[type='file']:not([accept^='image/'])", source)

    def test_attachment_submit_prefers_explicit_send_button(self):
        self.assertIn("button[data-testid='send-button']", ChatDom.send_selectors)

    def test_confirmation_fallback_is_exposed(self):
        self.assertTrue(callable(ChatDom.submission_visible))

    def test_reply_wait_never_filters_assistant_text(self):
        source = inspect.getsource(ChatSession.wait_for_reply)
        self.assertNotIn("required_text", source)
        self.assertNotIn("request_id in turn.text", source)

    def test_legacy_recovery_anchors_on_outbound_user_turn_not_reply_text(self):
        class Node:
            def __init__(self, role, text, identity): self.role, self.text, self.identity = role, text, identity
            def inner_text(self): return self.text
            def get_attribute(self, name):
                if name == "data-message-author-role": return self.role
                if name == "data-message-id": return self.identity
                return None
        class Locator:
            def __init__(self, nodes): self.nodes = nodes
            def count(self): return len(self.nodes)
            def nth(self, index): return self.nodes[index]
        class Page:
            def __init__(self):
                self.nodes = [Node("assistant", "old reply", "a0"),
                              Node("user", "REQUEST_ID=P-1", "u1"),
                              Node("assistant", "new reply without id", "a1")]
            def locator(self, _): return Locator(self.nodes)
        found, turns = ChatDom(Page()).assistant_turns_after_user("REQUEST_ID=P-1")
        self.assertTrue(found)
        self.assertEqual([turn.text for turn in turns], ["new reply without id"])

    def test_completed_turn_is_returned_after_three_stable_samples(self):
        class Clock:
            value = 0.0
            def __call__(self): return self.value
        class Page:
            def __init__(self, clock): self.clock = clock
            def wait_for_timeout(self, milliseconds): self.clock.value += milliseconds / 1000
        class Owner:
            def update(self, _): pass
        class Dom:
            def assistant_turns(self): return [type("Turn", (), {"identity": "a1", "text": "reply", "index": 1})()]
            def streaming(self): return False
            def ready_for_next_turn(self): return True
        with tempfile.TemporaryDirectory() as value:
            clock = Clock(); session = object.__new__(ChatSession)
            session.page = Page(clock); session.owner = Owner()
            session.request = type("Request", (), {"directory": Path(value), "project_id": "P", "request_id": "P-1"})()
            with patch("chat_courier.browser.ChatDom", return_value=Dom()), patch("chat_courier.browser.time.monotonic", side_effect=clock):
                turn = session.wait_for_reply(set(), 10)
        self.assertEqual(turn.text, "reply")
        self.assertEqual(clock.value, 2.0)

    def test_reply_timeout_records_dom_detection_evidence(self):
        class Clock:
            value = 0.0
            def __call__(self): return self.value
        class Page:
            def __init__(self, clock): self.clock = clock
            def wait_for_timeout(self, milliseconds): self.clock.value += milliseconds / 1000
        class Owner:
            def update(self, _): pass
        class Dom:
            def assistant_turns(self): return []
            def streaming(self): return False
            def ready_for_next_turn(self): return True
        with tempfile.TemporaryDirectory() as value:
            root = Path(value); clock = Clock(); session = object.__new__(ChatSession)
            session.page = Page(clock); session.owner = Owner()
            session.request = type("Request", (), {"directory": root, "project_id": "P", "request_id": "P-1"})()
            with patch("chat_courier.browser.ChatDom", return_value=Dom()), patch("chat_courier.browser.time.monotonic", side_effect=clock):
                self.assertIsNone(session.wait_for_reply(set(), 2))
            diagnostic = json.loads((root / "response-diagnostic.json").read_text(encoding="utf-8"))
        self.assertEqual(diagnostic["failure_stage"], "reply_not_detected")
        self.assertEqual(diagnostic["candidate_count"], 0)
        self.assertTrue(diagnostic["composer_ready"])

    def test_normal_chrome_user_data_is_rejected(self):
        with self.assertRaises(ProfileConfigurationError):
            validate_profile_path(Path(r"C:\Users\test\AppData\Local\Google\Chrome\User Data\Default"), "Default")

    def test_profile_directory_cannot_escape_user_data_root(self):
        with self.assertRaises(ProfileConfigurationError):
            validate_profile_path(Path(r"C:\Courier\profile"), r"Default\Profile 1")

    def test_registered_conversation_identity_is_not_the_chatgpt_home_page(self):
        self.assertEqual(conversation_id_from_url("https://chatgpt.com/c/conversation-1"), "conversation-1")
        self.assertEqual(conversation_id_from_url("https://chatgpt.com/g/g-project/c/conversation-2"), "conversation-2")
        self.assertIsNone(conversation_id_from_url("https://chatgpt.com/"))

    def test_access_denied_text_is_detected_before_composer_use(self):
        class Locator:
            def inner_text(self, **_): return "You don't have access to this conversation"
        class Page:
            def locator(self, _): return Locator()
        self.assertTrue(ChatDom(Page()).access_denied())

    def test_unconfirmed_submission_keeps_a_diagnostic_reference(self):
        path = Path("submission_diagnostic.json")
        error = SubmissionUnconfirmed("unconfirmed", path)
        self.assertEqual(error.diagnostic_path, path)

    def test_composer_fill_error_is_distinct_from_uncertain_send(self):
        self.assertTrue(issubclass(PreSubmissionError, RuntimeError))
        self.assertNotEqual(PreSubmissionError, SubmissionUnconfirmed)

    def test_submit_captures_diagnostics_without_clearing_the_failed_draft(self):
        source = inspect.getsource(ChatSession.submit)
        self.assertIn("_write_submission_diagnostic", source)
        self.assertNotIn('composer.fill("")', source)

    def test_contenteditable_fill_has_keyboard_fallback(self):
        class Keyboard:
            def __init__(self): self.inserted = None; self.pressed = []
            def insert_text(self, value): self.inserted = value
            def press(self, value): self.pressed.append(value)
        class Composer:
            def count(self): return 1
            def is_visible(self): return True
            def is_editable(self): return True
            def focus(self, **_kwargs): return None
            def fill(self, *_args, **_kwargs): raise RuntimeError("fill actionability timeout")
        class Page:
            def __init__(self):
                self.keyboard = Keyboard()
                self.composer = Composer()
            def locator(self, _selector):
                return type("Locator", (), {"last": self.composer})()
        page = Page()
        method = ChatDom(page).fill_composer(page.composer, "plain text")
        self.assertEqual(method, "keyboard_insert_text")
        self.assertEqual(page.keyboard.inserted, "plain text")
        self.assertEqual(page.keyboard.pressed, ["ControlOrMeta+A", "Backspace"])

    def test_send_button_uses_force_click_after_normal_click_failure(self):
        class Button:
            def count(self): return 1
            def is_visible(self): return True
            def is_enabled(self): return True
            def click(self, **kwargs):
                if not kwargs.get("force"): raise RuntimeError("intercepted")
            def evaluate(self, _): raise AssertionError("force click should succeed first")
        class Page:
            def locator(self, _):
                class Locator:
                    last = Button()
                return Locator()
        class Composer:
            def press(self, _): raise AssertionError("Enter must not be used with an enabled button")
        result = ChatDom(Page()).submit_composer(Composer())
        self.assertEqual(result["method"], "button_force")
        self.assertEqual(result["attempts"][0]["method"], "button")

    def test_enabled_button_exhaustion_does_not_fall_back_to_enter(self):
        class Button:
            def count(self): return 1
            def is_visible(self): return True
            def is_enabled(self): return True
            def click(self, **_): raise RuntimeError("blocked")
            def evaluate(self, _): raise RuntimeError("blocked")
        class Page:
            def locator(self, _):
                class Locator:
                    last = Button()
                return Locator()
        class Composer:
            def press(self, _): raise AssertionError("Enter must not be used with an enabled button")
        result = ChatDom(Page()).submit_composer(Composer())
        self.assertEqual(result["method"], "unavailable")
        self.assertEqual(len(result["attempts"]), 9)

    def test_attachment_wait_timeout_never_falls_back_to_enter(self):
        class Button:
            def count(self): return 1
            def is_visible(self): return True
            def is_enabled(self): return False
        class Page:
            def locator(self, _):
                class Locator:
                    last = Button()
                return Locator()
            def wait_for_timeout(self, _):
                raise AssertionError("zero-timeout attachment check must not wait")
        class Composer:
            def press(self, _): raise AssertionError("attachments must never use Enter fallback")
        result = ChatDom(Page()).submit_composer(Composer(), require_button=True, timeout_seconds=0)
        self.assertEqual(result["method"], "unavailable")
        self.assertIn("wait_for_enabled_send_button", [attempt["method"] for attempt in result["attempts"]])

    def test_upload_state_machine_classifies_success_and_failures(self):
        class Clock:
            value = 0.0
            def __call__(self): return self.value
            def advance(self, milliseconds): self.value += milliseconds / 1000
        class Field:
            @property
            def first(self): return self
            @property
            def last(self): return self
            def count(self): return 1
            def is_visible(self): return True
            def is_editable(self): return True
            def set_input_files(self, _): return None
        class Body(Field):
            def __init__(self, page): self.page = page
            def inner_text(self, **_):
                if self.page.closed: raise TargetClosedError("Target page, context or browser has been closed")
                if self.page.unresponsive: raise RuntimeError("CDP did not respond")
                return self.page.body
        class TargetClosedError(Exception): pass
        class Page:
            def __init__(self, body, clock, *, unresponsive=False, closed=False):
                self.body = body; self.clock = clock; self.unresponsive = unresponsive; self.closed = closed; self.url = "https://chatgpt.com/c/test"
            def title(self): return "ChatGPT"
            def locator(self, selector):
                return Body(self) if selector == "body" else Field()
            def wait_for_timeout(self, milliseconds): self.clock.advance(milliseconds)
        with tempfile.TemporaryDirectory() as value, patch("chat_courier.browser.time.monotonic") as monotonic:
            path = Path(value) / "evidence.json"; path.write_text("{}", encoding="utf-8")
            clock = Clock(); monotonic.side_effect = clock
            events = []
            timeline = ChatDom(Page("evidence.json", clock)).upload((path,), on_event=lambda name, **data: events.append((name, data)))
            self.assertEqual(len(timeline), 3)
            self.assertIn("attachment_upload_started", [name for name, _ in events])
            self.assertEqual(events[-1][0], "attachment_upload_progress")

            for body, expected in (("Unable to upload evidence.json", "attachment_upload_failed"), ("evidence.json Uploading", "attachment_upload_stalled")):
                clock = Clock(); monotonic.side_effect = clock
                with self.assertRaises(PreSubmissionError) as captured:
                    ChatDom(Page(body, clock)).upload((path,), timeout_seconds=120, stall_seconds=30)
                self.assertEqual(captured.exception.failure_stage, expected)
            clock = Clock(); monotonic.side_effect = clock
            with self.assertRaises(PreSubmissionError) as captured:
                ChatDom(Page("", clock)).upload((path,), timeout_seconds=3, stall_seconds=999)
            self.assertEqual(captured.exception.failure_stage, "attachment_upload_timeout")
            clock = Clock(); monotonic.side_effect = clock
            with self.assertRaises(PreSubmissionError) as captured:
                ChatDom(Page("", clock, unresponsive=True)).upload((path,))
            self.assertEqual(captured.exception.failure_stage, "browser_page_unresponsive")
            clock = Clock(); monotonic.side_effect = clock
            with self.assertRaises(PreSubmissionError) as captured:
                ChatDom(Page("", clock, closed=True)).upload((path,))
            self.assertEqual(captured.exception.failure_stage, "page_closed_during_upload")
