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

    def test_legacy_recovery_anchors_on_latest_user_turn_not_reply_text(self):
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
        found, turns = ChatDom(Page()).assistant_turns_after_latest_user()
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

    def test_stable_turn_waits_until_composer_is_ready(self):
        class Clock:
            value = 0.0
            def __call__(self): return self.value
        class Page:
            def __init__(self, clock): self.clock = clock
            def wait_for_timeout(self, milliseconds): self.clock.value += milliseconds / 1000
        class Owner:
            def update(self, _): pass
        class Dom:
            def __init__(self, clock): self.clock = clock
            def assistant_turns(self): return [type("Turn", (), {"identity": "a1", "text": "reply", "index": 1})()]
            def streaming(self): return False
            def ready_for_next_turn(self): return self.clock.value >= 4.0
        with tempfile.TemporaryDirectory() as value:
            clock = Clock(); session = object.__new__(ChatSession)
            session.page = Page(clock); session.owner = Owner()
            session.request = type("Request", (), {"directory": Path(value), "project_id": "P", "request_id": "P-1"})()
            with patch("chat_courier.browser.ChatDom", return_value=Dom(clock)), patch("chat_courier.browser.time.monotonic", side_effect=clock):
                turn = session.wait_for_reply(set(), 10)
        self.assertEqual(turn.text, "reply")
        self.assertEqual(clock.value, 6.0)

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

    def test_rollover_recovery_finds_unique_successor_by_request_marker(self):
        source = "https://chatgpt.com/g/g-project/c/old"
        successor = "https://chatgpt.com/g/g-project/c/new"

        class Locator:
            def __init__(self, page, selector): self.page, self.selector = page, selector
            def evaluate_all(self, _script): return [successor] if self.selector == "a[href]" else []
            def all_inner_texts(self):
                return ["REQUEST_ID=P-1"] if self.page.url == successor else []
            def inner_text(self, **_kwargs): return ""

        class Page:
            def __init__(self): self.url = source
            def goto(self, url, **_kwargs): self.url = url
            def title(self): return "Project"
            def wait_for_timeout(self, _milliseconds): pass
            def locator(self, selector): return Locator(self, selector)

        session = object.__new__(ChatSession)
        session.page = Page()
        with tempfile.TemporaryDirectory() as value:
            session.request = type("Request", (), {"chat_url": source, "directory": Path(value)})()
            self.assertEqual(session.recover_successor_url("P-1"), successor)
            diagnostic = json.loads((Path(value) / "rollover-recovery-diagnostic.json").read_text(encoding="utf-8"))
        self.assertEqual(diagnostic["same_project_candidate_count"], 1)
        self.assertEqual(diagnostic["match_count"], 1)

    def test_access_denied_text_is_detected_before_composer_use(self):
        class Locator:
            def inner_text(self, **_): return "You don't have access to this conversation"
        class Page:
            def locator(self, _): return Locator()
        self.assertTrue(ChatDom(Page()).access_denied())

    def test_rate_limit_detection_ignores_conversation_text(self):
        class Locator:
            def __init__(self, text="", messages=()):
                self.text, self.messages = text, messages
            def inner_text(self, **_): return self.text
            def all_inner_texts(self): return list(self.messages)
        class Page:
            def __init__(self, banner=""):
                self.banner = banner
            def locator(self, selector):
                if selector == "body":
                    return Locator(f"User said: rate limit {self.banner}")
                if selector == ChatDom.user_selector:
                    return Locator(messages=("User said: rate limit",))
                return Locator(messages=())

        self.assertFalse(ChatDom(Page()).rate_limited())
        self.assertTrue(ChatDom(Page("Too many requests. Try again later.")).rate_limited())

    def test_interrupted_reply_with_usable_composer_is_not_streaming(self):
        class Element:
            def __init__(self, *, text="", visible=True):
                self.text, self.visible = text, visible
            @property
            def last(self): return self
            @property
            def first(self): return self
            def count(self): return 1
            def nth(self, _index): return self
            def inner_text(self): return self.text
            def is_visible(self): return self.visible
            def is_enabled(self): return True
            def is_editable(self): return True
            def get_attribute(self, _name): return None
        class Page:
            def locator(self, selector):
                if selector == ChatDom.assistant_selector:
                    return Element(text="Thinking\n\nConnection interrupted. Waiting for the complete answer")
                if selector in ChatDom.composer_selectors:
                    return Element()
                if selector == ChatDom.stop_selector:
                    return Element()
                return Element(visible=False)

        self.assertFalse(ChatDom(Page()).streaming())

    def test_visible_editable_composer_outweighs_generic_login_labels(self):
        class Element:
            def __init__(self, *, visible=False, editable=False, text=""):
                self.visible, self.editable, self.text = visible, editable, text
            @property
            def last(self): return self
            @property
            def first(self): return self
            def count(self): return 1
            def is_visible(self): return self.visible
            def is_editable(self): return self.editable
            def inner_text(self, **_): return self.text
        class Page:
            url = "https://chatgpt.com/g/project/c/conversation"
            def locator(self, selector):
                if selector in ChatDom.composer_selectors:
                    return Element(visible=True, editable=True)
                if selector == "body":
                    return Element(text="MePhC project Continue as another account")
                return Element(visible=True)
        self.assertFalse(ChatDom(Page()).authentication_required())

    def test_empty_composer_is_ready_without_a_visible_send_button(self):
        class Element:
            @property
            def last(self): return self
            @property
            def first(self): return self
            def count(self): return 1
            def is_visible(self): return True
            def is_enabled(self): return True
            def is_editable(self): return True
            def get_attribute(self, _name): return None
        class Missing:
            @property
            def last(self): return self
            @property
            def first(self): return self
            def count(self): return 0
        class Page:
            url = "https://chatgpt.com/c/conversation"
            def title(self): return "Chat"
            def locator(self, selector):
                if selector in ChatDom.composer_selectors:
                    return Element()
                return Missing()
        self.assertTrue(ChatDom(Page()).ready_for_next_turn())

    def test_latest_message_snapshot_records_role_identity_hash_and_request_id(self):
        class Node:
            def inner_text(self): return "REQUEST_ID=P-1\nreply"
            def get_attribute(self, name):
                return {"data-message-author-role": "assistant",
                        "data-message-id": "message-7"}.get(name)
        class Locator:
            def count(self): return 1
            def nth(self, _index): return Node()
        class Page:
            def locator(self, _selector): return Locator()
        snapshot = ChatDom(Page()).latest_message_snapshot()
        self.assertEqual(snapshot["role"], "assistant")
        self.assertEqual(snapshot["identity"], "message-7")
        self.assertEqual(snapshot["request_ids"], ["P-1"])
        self.assertEqual(len(snapshot["text_sha256"]), 64)

    def test_marker_reply_survives_a_later_human_turn(self):
        turns = [
            ("user", "REQUEST_ID=P-1\nrequest", "u1"),
            ("assistant", "R4 reply", "a1"),
            ("user", "test", "u2"),
            ("assistant", "1", "a2"),
        ]
        class Node:
            def __init__(self, value): self.value = value
            def inner_text(self): return self.value[1]
            def get_attribute(self, name):
                return {"data-message-author-role": self.value[0],
                        "data-message-id": self.value[2]}.get(name)
        class Locator:
            def count(self): return len(turns)
            def nth(self, index): return Node(turns[index])
        class Page:
            def locator(self, _selector): return Locator()
        dom = ChatDom(Page())
        found, replies = dom.assistant_turns_after_user_marker("REQUEST_ID=P-1")
        self.assertTrue(found)
        self.assertEqual([turn.identity for turn in replies], ["a1"])
        snapshot = dom.conversation_snapshot()
        self.assertEqual(snapshot["messages"][1]["in_reply_to_request_id"], "P-1")
        self.assertIsNone(snapshot["messages"][3]["in_reply_to_request_id"])

    def test_conflicting_final_reply_uses_native_regenerate_without_resending(self):
        class EmptyControls:
            def count(self): return 0
            def nth(self, _index): raise AssertionError("no control")

        class Control:
            def __init__(self, page): self.page = page
            def is_visible(self): return True
            def is_enabled(self): return True
            def click(self, **_kwargs):
                self.page.nodes[1].text = (
                    "CHAT_COURIER_REPLY/1\nPROJECT_ID=P\nREQUEST_ID=P-2\n"
                    "BEGIN_RESPONSE\nnew\nEND_RESPONSE"
                )

        class Controls:
            def __init__(self, control): self.control = control
            def count(self): return 1
            def nth(self, _index): return self.control

        class Node:
            def __init__(self, page, role, text, identity):
                self.page, self.role, self.text, self.identity = page, role, text, identity
            def inner_text(self): return self.text
            def get_attribute(self, name):
                return {"data-message-author-role": self.role,
                        "data-message-id": self.identity}.get(name)
            def hover(self, **_kwargs): pass
            def locator(self, selector):
                if selector == ChatDom.regenerate_selectors[0]:
                    return Controls(Control(self.page))
                return EmptyControls()

        class Conversation:
            def __init__(self, nodes): self.nodes = nodes
            def count(self): return len(self.nodes)
            def nth(self, index): return self.nodes[index]

        class Page:
            def __init__(self):
                self.nodes = [Node(self, "user", "REQUEST_ID=P-2", "u2")]
                self.nodes.append(Node(
                    self, "assistant",
                    "CHAT_COURIER_REPLY/1\nPROJECT_ID=P\nREQUEST_ID=P-1\n"
                    "BEGIN_RESPONSE\nold\nEND_RESPONSE", "a1",
                ))
            def locator(self, selector):
                if "data-message-author-role" in selector:
                    return Conversation(self.nodes)
                return EmptyControls()
            def wait_for_timeout(self, _milliseconds): pass

        dom = ChatDom(Page())
        dom.streaming = lambda: False
        dom.ready_for_next_turn = lambda: True
        intents = []
        result = dom.regenerate_conflicting_reply(
            "REQUEST_ID=P-2", "P-2", before_click=intents.append,
        )
        self.assertEqual(result["method"], "page_native_regenerate")
        self.assertEqual(result["scope"], "assistant_turn")
        self.assertEqual(result["conflicting_request_ids"], ["P-1"])
        self.assertEqual(len(intents), 1)

    def test_auth_url_remains_authoritative_even_if_composer_is_visible(self):
        class Composer:
            @property
            def last(self): return self
            def count(self): return 1
            def is_visible(self): return True
            def is_editable(self): return True
        class Page:
            url = "https://chatgpt.com/auth/login"
            def locator(self, _): return Composer()
        self.assertTrue(ChatDom(Page()).authentication_required())

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

    def test_large_contenteditable_fill_uses_bounded_dom_insertion(self):
        value = "x" * 40000
        class Composer:
            def __init__(self): self.value = ""
            def count(self): return 1
            def is_visible(self): return True
            def is_editable(self): return True
            def focus(self, **_kwargs): return None
            def fill(self, *_args, **_kwargs): raise RuntimeError("fill actionability timeout")
            def evaluate(self, _script, text=None):
                if text is None: return "DIV"
                self.value = text
            def inner_text(self): return self.value
        class Page:
            def __init__(self): self.composer = Composer()
            def locator(self, _selector): return type("Locator", (), {"last": self.composer})()
        page = Page()
        self.assertEqual(ChatDom(page).fill_composer(page.composer, value), "dom_insert_text")
        self.assertEqual(page.composer.value, value)

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
