from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from chat_courier.model import MAX_INLINE_MESSAGE_BYTES, ValidationError, load_request
from chat_courier.browser import BrowserError
from chat_courier.storage import (event, load_receipt, load_response_capture, load_response_cursor,
                                  ensure_target_binding, ledger_reply, merge_conversation_ledger,
                                  receipt, request_events, save_latest_probe, save_response, save_response_capture,
                                  save_latest_response_capture, save_response_cursor, submission_count)
from chat_courier.cli import (_latest_conflicting_envelope, _regenerate_conflicting_envelope_once,
                              _regenerate_interrupted_response_once,
                              _safe_pre_browser_turn_recovery, _submission_confirmed,
                              reconcile_command, resend_once_command, retry_once_command,
                              rollover_target_command)


class StorageTests(unittest.TestCase):
    def test_interrupted_response_native_retry_is_one_shot(self):
        with tempfile.TemporaryDirectory() as value:
            request = self.request(Path(value))

            def native_retry(*_args, **kwargs):
                kwargs["before_click"]({"method": "page_native_regenerate"})
                return {"method": "page_native_regenerate"}

            class Session:
                def __init__(self, *_args, **_kwargs): self.page = object()
                def __enter__(self): return self
                def __exit__(self, *_args): return False

            with patch("chat_courier.cli.ChatSession", Session), \
                    patch("chat_courier.cli.ChatDom.regenerate_conflicting_reply",
                          side_effect=native_retry) as retry, \
                    patch("chat_courier.cli.run_command", return_value=0):
                self.assertEqual(_regenerate_interrupted_response_once(request), 0)
                self.assertEqual(_regenerate_interrupted_response_once(request), 1)
            self.assertEqual(retry.call_count, 1)

    def test_interrupted_response_falls_back_to_one_labeled_same_request_turn(self):
        with tempfile.TemporaryDirectory() as value:
            request = self.request(Path(value))
            submitted: list[str] = []

            class Session:
                def __init__(self, *_args, **_kwargs): self.page = object()
                def __enter__(self): return self
                def __exit__(self, *_args): return False
                def submit(self, prompt, attachments):
                    if attachments != ():
                        raise AssertionError("recovery test unexpectedly received attachments")
                    submitted.append(prompt)

            with patch("chat_courier.cli.ChatSession", Session), \
                    patch("chat_courier.cli.ChatDom.regenerate_conflicting_reply",
                          side_effect=BrowserError(
                              "the exact Courier request has no assistant reply to regenerate"
                          )), \
                    patch("chat_courier.cli.run_command", return_value=0):
                self.assertEqual(_regenerate_interrupted_response_once(request), 0)
                self.assertEqual(_regenerate_interrupted_response_once(request), 1)

            self.assertEqual(len(submitted), 1)
            self.assertIn("CHAT_COURIER_RECOVERY_NOTICE/1", submitted[0])
            self.assertIn("REQUEST_ID=P-1", submitted[0])

    def test_interrupted_recovery_pre_send_busy_does_not_consume_budget(self):
        with tempfile.TemporaryDirectory() as value:
            request = self.request(Path(value))
            event(request, "interrupted_reply_recovery_resend_intent", phase="reconcile")

            class Session:
                def __init__(self, *_args, **_kwargs): self.page = object()
                def __enter__(self): return self
                def __exit__(self, *_args): return False

            with patch("chat_courier.cli.ChatSession", Session), \
                    patch("chat_courier.cli.ChatDom.regenerate_conflicting_reply",
                          return_value={"method": "page_native_regenerate"}) as retry, \
                    patch("chat_courier.cli.run_command", return_value=0):
                self.assertEqual(_regenerate_interrupted_response_once(request), 0)
            self.assertEqual(retry.call_count, 1)

    def test_wrong_id_reply_falls_back_to_one_labeled_same_request_turn(self):
        with tempfile.TemporaryDirectory() as value:
            request = self.request(Path(value))
            submitted: list[str] = []
            case = self

            class Session:
                def __init__(self, *_args, **_kwargs): self.page = object()
                def __enter__(self): return self
                def __exit__(self, *_args): return False
                def submit(self, prompt, attachments):
                    case.assertEqual(attachments, ())
                    submitted.append(prompt)

            conflict = {
                "assistant_identity": "a-old",
                "raw_sha256": "deadbeef",
                "conflicting_request_ids": ["P-OLD"],
            }
            with patch("chat_courier.cli.ChatSession", Session), \
                    patch("chat_courier.cli.ChatDom.regenerate_conflicting_reply",
                          side_effect=BrowserError(
                              "no unambiguous page-native regenerate control was available for the conflicting reply"
                          )), \
                    patch("chat_courier.cli.run_command", return_value=0) as run:
                self.assertEqual(_regenerate_conflicting_envelope_once(request, conflict), 0)

            self.assertEqual(len(submitted), 1)
            self.assertIn("CHAT_COURIER_RECOVERY_NOTICE/1", submitted[0])
            self.assertIn("REQUEST_ID=P-1", submitted[0])
            self.assertIn("This is the same logical request", submitted[0])
            self.assertEqual(run.call_count, 1)
            events = request_events(request)
            self.assertEqual(sum(item.get("event") == "conflicting_reply_recovery_resend_intent"
                                 for item in events), 1)
            self.assertEqual(sum(item.get("event") == "conflicting_reply_recovery_resend_submitted"
                                 for item in events), 1)

    def test_wrong_id_reply_does_not_resend_for_unrelated_browser_error(self):
        with tempfile.TemporaryDirectory() as value:
            request = self.request(Path(value))

            class Session:
                def __init__(self, *_args, **_kwargs): self.page = object()
                def __enter__(self): return self
                def __exit__(self, *_args): return False

            with patch("chat_courier.cli.ChatSession", Session), \
                    patch("chat_courier.cli.ChatDom.regenerate_conflicting_reply",
                          side_effect=BrowserError("ChatGPT is not idle")):
                result = _regenerate_conflicting_envelope_once(request, {
                    "assistant_identity": "a-old", "raw_sha256": "deadbeef",
                    "conflicting_request_ids": ["P-OLD"],
                })

            self.assertEqual(result, 1)
            self.assertFalse(any(item.get("event") == "conflicting_reply_recovery_resend_intent"
                                 for item in request_events(request)))

    def test_latest_conflicting_envelope_is_exact_and_hash_bound(self):
        with tempfile.TemporaryDirectory() as value:
            request = self.request(Path(value))
            text = (
                "CHAT_COURIER_REPLY/1\nPROJECT_ID=P\nREQUEST_ID=P-OLD\n"
                "BEGIN_RESPONSE\nstale\nEND_RESPONSE"
            )
            save_latest_response_capture(
                request, identity="a-old", index=3, text=text, user_turn_found=True,
            )
            conflict = _latest_conflicting_envelope(request)
            self.assertEqual(conflict["assistant_identity"], "a-old")
            self.assertEqual(conflict["conflicting_request_ids"], ["P-OLD"])

            (request.directory / "latest-response.raw.txt").write_text(
                text + "tampered", encoding="utf-8",
            )
            self.assertIsNone(_latest_conflicting_envelope(request))

    def test_latest_conflicting_envelope_survives_rejected_capture_via_ledger(self):
        with tempfile.TemporaryDirectory() as value:
            request = self.request(Path(value))
            text = (
                "CHAT_COURIER_REPLY/1\nPROJECT_ID=P\nREQUEST_ID=P-OLD\n"
                "BEGIN_RESPONSE\nstale\nEND_RESPONSE"
            )
            merge_conversation_ledger(request, {"message_count": 2, "messages": [
                {"ordinal": 0, "role": "user", "identity": "u-new",
                 "text": "REQUEST_ID=P-1", "text_sha256": "user",
                 "request_ids": ["P-1"]},
                {"ordinal": 1, "role": "assistant", "identity": "a-old",
                 "text": text,
                 "text_sha256": __import__("hashlib").sha256(text.encode("utf-8")).hexdigest(),
                 "request_ids": ["P-OLD"]},
            ]}, streaming=False, composer_ready=True, session_id="ledger")
            conflict = _latest_conflicting_envelope(request)
            self.assertEqual(conflict["evidence_source"], "conversation_ledger")
            self.assertEqual(conflict["conflicting_request_ids"], ["P-OLD"])

    def test_reconcile_uses_two_absence_observations_once_then_freezes_uncertainty(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value); request = self.request(root)
            receipt(request, "submission_unconfirmed", "uncertain")
            event(request, "chat_submission_unconfirmed", phase="submit")
            now = __import__("time").time()
            (root / "absence-observations.json").write_text(json.dumps({
                "version": 1, "project_id": "P", "request_id": "P-1",
                "observations": [
                    {"observed_at": now - 40, "session_id": "one",
                     "payload_fingerprint": request.payload_fingerprint,
                     "chat_url": request.chat_url, "streaming": False,
                     "composer_ready": True, "anchor_found": False},
                    {"observed_at": now, "session_id": "two",
                     "payload_fingerprint": request.payload_fingerprint,
                     "chat_url": request.chat_url, "streaming": False,
                     "composer_ready": True, "anchor_found": False},
                ],
            }), encoding="utf-8")
            def uncertain_again(_args):
                receipt(request, "submission_unconfirmed", "still uncertain")
                return 1
            args = type("Args", (), {"request_directory": str(root)})()
            with patch("chat_courier.model._load_registry", return_value={"P": request.chat_url}), \
                    patch("chat_courier.cli.capture_latest_command", return_value=1), \
                    patch("chat_courier.cli.run_command", side_effect=uncertain_again) as run:
                self.assertEqual(reconcile_command(args), 1)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(load_receipt(request)["state"], "request_frozen")
            self.assertEqual(sum(item.get("event") == "uncertain_auto_resend_authorized"
                                 for item in request_events(request)), 1)

    def test_target_rollover_changes_binding_not_payload_identity(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            (root / "message.txt").write_text("message", encoding="utf-8")
            (root / "request.json").write_text(json.dumps({
                "version": 1, "project_id": "P", "request_id": "P-1",
            }), encoding="utf-8")
            with patch("chat_courier.model._load_registry", return_value={"P": "https://chatgpt.com/g/project/c/old"}):
                old = load_request(root)
            ensure_target_binding(old); receipt(old, "waiting_for_response", "sent")
            with patch("chat_courier.model._load_registry", return_value={"P": "https://chatgpt.com/g/project/c/new"}):
                new = load_request(root)
            binding = ensure_target_binding(new, basis="user_direct")
            self.assertNotEqual(old.fingerprint, new.fingerprint)
            self.assertEqual(old.payload_fingerprint, new.payload_fingerprint)
            self.assertEqual(binding["generation"], 2)
            self.assertEqual(binding["previous_url"], old.chat_url)
            self.assertEqual(load_receipt(new)["state"], "waiting_for_response")

    def test_partial_dom_snapshots_merge_and_exact_envelope_owns_reply(self):
        with tempfile.TemporaryDirectory() as value:
            request = self.request(Path(value))
            first = {"message_count": 2, "messages": [
                {"ordinal": 0, "role": "user", "identity": "u1", "text": "REQUEST_ID=P-1",
                 "text_sha256": "a", "request_ids": ["P-1"], "in_reply_to_request_id": None},
            ]}
            second = {"message_count": 2, "messages": [
                {"ordinal": 1, "role": "assistant", "identity": "a1",
                 "text": "CHAT_COURIER_REPLY/1\nREQUEST_ID=P-1\nSTATUS=CONTINUE\nBODY:\nok",
                 "text_sha256": "b", "request_ids": ["P-1"], "in_reply_to_request_id": "P-1"},
            ]}
            merge_conversation_ledger(request, first, streaming=False, composer_ready=True, session_id="s1")
            ledger = merge_conversation_ledger(request, second, streaming=False, composer_ready=True, session_id="s2")
            self.assertEqual([item["identity"] for item in ledger["messages"]], ["u1", "a1"])
            self.assertEqual(ledger_reply(request)["identity"], "a1")

    def test_oversized_inline_message_is_rejected_before_browser_use(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            (root / "message.txt").write_text(
                "x" * (MAX_INLINE_MESSAGE_BYTES + 1), encoding="utf-8")
            (root / "request.json").write_text(json.dumps({
                "version": 1, "project_id": "P", "request_id": "P-1",
                "chat_url": "https://chatgpt.com/c/x"}), encoding="utf-8")
            with patch("chat_courier.model._load_registry", return_value={"P": "https://chatgpt.com/c/x"}):
                with self.assertRaisesRegex(ValidationError, "inline limit"):
                    load_request(root)

    def request(self, root: Path, retry: bool = False):
        (root / "message.txt").write_text("message", encoding="utf-8")
        manifest = {"version": 1, "project_id": "P", "request_id": "P-1",
                    "chat_url": "https://chatgpt.com/c/x"}
        if retry:
            (root / "retry-message.txt").write_text("compact message", encoding="utf-8")
            manifest["retry_message_file"] = "retry-message.txt"
        (root / "request.json").write_text(json.dumps(manifest), encoding="utf-8")
        with patch("chat_courier.model._load_registry", return_value={"P": "https://chatgpt.com/c/x"}):
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

    def test_response_cursor_and_raw_capture_are_hash_bound(self):
        with tempfile.TemporaryDirectory() as value:
            request = self.request(Path(value))
            save_response_cursor(request, {"old-a", "old-b"})
            self.assertEqual(load_response_cursor(request), {"old-a", "old-b"})
            saved = save_response_capture(request, identity="new-c", index=3, text="reply without an envelope")
            capture, text = load_response_capture(request)
            self.assertEqual(capture["raw_sha256"], saved["raw_sha256"])
            self.assertEqual(text, "reply without an envelope")
            (Path(value) / "response.raw.txt").write_text("drift", encoding="utf-8")
            self.assertIsNone(load_response_capture(request))

    def test_cursor_and_receipt_caches_recover_without_blocking_reconcile(self):
        with tempfile.TemporaryDirectory() as value:
            request = self.request(Path(value))
            save_response_cursor(request, {"old-a"})
            (request.directory / "response-cursor.json").write_text("{broken", encoding="utf-8")
            self.assertIsNone(load_response_cursor(request))
            receipt(request, "queued", "first")
            receipt(request, "waiting_for_response", "second")
            (request.directory / "receipt.json").write_text("{broken", encoding="utf-8")
            self.assertEqual(load_receipt(request)["state"], "queued")

    def test_only_explicit_post_send_states_enter_read_only_recovery(self):
        self.assertFalse(_submission_confirmed({"state": "submission_intent"}))
        self.assertFalse(_submission_confirmed({"state": "browser_error"}))
        self.assertFalse(_submission_confirmed({"state": "courier_error"}))
        self.assertFalse(_submission_confirmed({"state": "submission_not_started"}))
        self.assertTrue(_submission_confirmed({"state": "request_submitted"}))
        self.assertTrue(_submission_confirmed({"state": "submission_unconfirmed"}))
        self.assertTrue(_submission_confirmed({"state": "response_captured"}))

    def test_same_request_resend_is_bounded_to_second_submission(self):
        with tempfile.TemporaryDirectory() as value:
            request = self.request(Path(value))
            event_path = request.directory / "events.jsonl"
            event_path.write_text(json.dumps({"event": "request_submitted", "project_id": "P",
                                              "request_id": "P-1"}) + "\n", encoding="utf-8")
            args = type("Args", (), {"request_directory": str(request.directory)})()
            with patch("chat_courier.model._load_registry", return_value={"P": "https://chatgpt.com/c/x"}), \
                    patch("chat_courier.cli.run_command", return_value=0) as run:
                self.assertEqual(resend_once_command(args), 0)
                self.assertTrue(args.resend_once)
                run.assert_called_once_with(args)
            with event_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"event": "request_submitted", "project_id": "P",
                                         "request_id": "P-1"}) + "\n")
            self.assertEqual(submission_count(request), 2)
            with patch("chat_courier.model._load_registry", return_value={"P": "https://chatgpt.com/c/x"}), \
                    patch("chat_courier.cli.run_command") as run:
                self.assertEqual(resend_once_command(args), 2)
                run.assert_not_called()

    def test_same_request_resend_selects_optional_compact_message(self):
        with tempfile.TemporaryDirectory() as value:
            request = self.request(Path(value), retry=True)
            (request.directory / "events.jsonl").write_text(
                json.dumps({"event": "request_submitted", "project_id": "P",
                            "request_id": "P-1"}) + "\n", encoding="utf-8")
            args = type("Args", (), {"request_directory": str(request.directory)})()
            with patch("chat_courier.model._load_registry", return_value={"P": "https://chatgpt.com/c/x"}), \
                    patch("chat_courier.cli.run_command", return_value=0) as run:
                self.assertEqual(resend_once_command(args), 0)
                self.assertTrue(args.use_retry_message)
                self.assertEqual(load_request(request.directory).retry_message, "compact message")
                run.assert_called_once_with(args)

    def test_supervisor_resend_allows_one_zero_submission_retry_after_unconfirmed_send(self):
        with tempfile.TemporaryDirectory() as value:
            request = self.request(Path(value))
            receipt(request, "submission_unconfirmed", "Send was not visibly accepted")
            save_latest_probe(request, user_turn_found=False, reply_found=False,
                              live_owner_found=False)
            args = type("Args", (), {"request_directory": str(request.directory)})()
            with patch("chat_courier.model._load_registry", return_value={"P": "https://chatgpt.com/c/x"}), \
                    patch("chat_courier.cli.read_owner", return_value=None), \
                    patch("chat_courier.cli.run_command", return_value=0) as run:
                self.assertEqual(resend_once_command(args), 0)
                self.assertTrue(args.evidence_retry)
                run.assert_called_once_with(args)
                self.assertEqual(resend_once_command(args), 2)

    def test_evidence_retry_allows_one_unsent_browser_started_request(self):
        with tempfile.TemporaryDirectory() as value:
            request = self.request(Path(value))
            receipt(request, "queue_recovery_required", "lost after browser start")
            (request.directory / "events.jsonl").write_text(
                json.dumps({"event": "browser_started", "project_id": "P",
                            "request_id": "P-1"}) + "\n", encoding="utf-8")
            save_latest_probe(request, user_turn_found=False, reply_found=False,
                              live_owner_found=False)
            args = type("Args", (), {"request_directory": str(request.directory)})()
            with patch("chat_courier.model._load_registry", return_value={"P": "https://chatgpt.com/c/x"}), \
                    patch("chat_courier.cli.read_owner", return_value=None), \
                    patch("chat_courier.cli.run_command", return_value=0) as run:
                self.assertEqual(retry_once_command(args), 0)
                self.assertTrue(args.evidence_retry)
                run.assert_called_once_with(args)
                self.assertEqual(retry_once_command(args), 2)

    def test_evidence_retry_allows_resolved_zero_submission_auth_failure(self):
        with tempfile.TemporaryDirectory() as value:
            request = self.request(Path(value))
            receipt(request, "chat_auth_required", "sign in once")
            save_latest_probe(request, user_turn_found=False, reply_found=False,
                              live_owner_found=False)
            args = type("Args", (), {"request_directory": str(request.directory)})()
            with patch("chat_courier.model._load_registry", return_value={"P": "https://chatgpt.com/c/x"}), \
                    patch("chat_courier.cli.read_owner", return_value=None), \
                    patch("chat_courier.cli.run_command", return_value=0) as run:
                self.assertEqual(retry_once_command(args), 0)
                self.assertTrue(args.evidence_retry)
                run.assert_called_once_with(args)
                self.assertEqual(retry_once_command(args), 2)

    def test_evidence_retry_allows_verified_submission_not_started(self):
        with tempfile.TemporaryDirectory() as value:
            request = self.request(Path(value))
            receipt(
                request,
                "submission_not_started",
                "composer was busy before Send",
                safe_to_retry_same_request=True,
            )
            save_latest_probe(request, user_turn_found=False, reply_found=False,
                              live_owner_found=False)
            args = type("Args", (), {"request_directory": str(request.directory)})()
            with patch("chat_courier.model._load_registry", return_value={"P": "https://chatgpt.com/c/x"}), \
                    patch("chat_courier.cli.read_owner", return_value=None), \
                    patch("chat_courier.cli.run_command", return_value=0) as run:
                self.assertEqual(retry_once_command(args), 0)
                self.assertTrue(args.evidence_retry)
                run.assert_called_once_with(args)
                self.assertEqual(retry_once_command(args), 2)

    def test_evidence_retry_allows_interrupted_submission_intent_write(self):
        with tempfile.TemporaryDirectory() as value:
            request = self.request(Path(value))
            receipt(request, "queue_turn_acquired", "turn acquired")
            event(request, "submission_intent_writing", phase="submit")
            save_latest_probe(request, user_turn_found=False, reply_found=False,
                              live_owner_found=False)
            args = type("Args", (), {"request_directory": str(request.directory)})()
            with patch("chat_courier.model._load_registry", return_value={
                        "P": "https://chatgpt.com/c/x"}), \
                    patch("chat_courier.cli.read_owner", return_value=None), \
                    patch("chat_courier.cli.run_command", return_value=0) as run:
                self.assertEqual(retry_once_command(args), 0)
                self.assertTrue(args.evidence_retry)
                run.assert_called_once_with(args)
                self.assertEqual(retry_once_command(args), 2)

    def test_evidence_retry_adopts_user_registered_empty_rollover_target(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            (root / "message.txt").write_text("message", encoding="utf-8")
            (root / "request.json").write_text(json.dumps({
                "version": 1, "project_id": "P", "request_id": "P-1",
            }), encoding="utf-8")
            source = "https://chatgpt.com/g/g-project/c/old"
            successor = "https://chatgpt.com/g/g-project/c/new"
            with patch("chat_courier.model._load_registry", return_value={"P": source}):
                old = load_request(root)
                receipt(old, "queue_recovery_required", "successor URL was not established")
                event(old, "request_submitted", phase="submit")
            archive = root / "target-generation-1"
            archive.mkdir()
            (root / "target-rollover.json").write_text(json.dumps({
                "version": 1, "phase": "prepared", "basis": "user_direct",
                "project_id": "P", "request_id": "P-1", "source_url": source,
                "source_fingerprint": old.fingerprint,
                "archive_directory": archive.name,
                "prior_total_submission_count": 1,
            }), encoding="utf-8")
            with patch("chat_courier.model._load_registry", return_value={"P": successor}):
                current = load_request(root)
                save_latest_probe(current, user_turn_found=False, reply_found=False,
                                  live_owner_found=False)
                args = type("Args", (), {"request_directory": str(root)})()
                with patch("chat_courier.cli.read_owner", return_value=None), \
                        patch("chat_courier.cli.run_command", return_value=0) as run:
                    self.assertEqual(retry_once_command(args), 0)
                    run.assert_called_once_with(args)
                self.assertEqual(load_receipt(current)["fingerprint"], current.fingerprint)
                self.assertEqual(submission_count(current), 0)
                self.assertTrue((archive / "target-rollover-prepared.json").is_file())

    def test_evidence_retry_rejects_chat_anchor_submission_and_live_owner(self):
        with tempfile.TemporaryDirectory() as value:
            request = self.request(Path(value))
            receipt(request, "queue_recovery_required", "lost")
            args = type("Args", (), {"request_directory": str(request.directory)})()
            save_latest_probe(request, user_turn_found=True, reply_found=False,
                              live_owner_found=False)
            with patch("chat_courier.model._load_registry", return_value={"P": "https://chatgpt.com/c/x"}), \
                    patch("chat_courier.cli.read_owner", return_value=None), \
                    patch("chat_courier.cli.run_command") as run:
                self.assertEqual(retry_once_command(args), 2)
                run.assert_not_called()
            save_latest_probe(request, user_turn_found=False, reply_found=False,
                              live_owner_found=False)
            (request.directory / "events.jsonl").write_text(
                json.dumps({"event": "request_submitted", "project_id": "P",
                            "request_id": "P-1"}) + "\n", encoding="utf-8")
            with patch("chat_courier.model._load_registry", return_value={"P": "https://chatgpt.com/c/x"}), \
                    patch("chat_courier.cli.read_owner", return_value=None):
                self.assertEqual(retry_once_command(args), 2)

    def test_evidence_retry_rejects_stale_or_mismatched_probe(self):
        with tempfile.TemporaryDirectory() as value:
            request = self.request(Path(value))
            receipt(request, "queue_recovery_required", "lost")
            save_latest_probe(request, user_turn_found=False, reply_found=False,
                              live_owner_found=False)
            probe_path = request.directory / "latest-probe.json"
            probe = json.loads(probe_path.read_text(encoding="utf-8"))
            probe["captured_at"] = 1
            probe_path.write_text(json.dumps(probe), encoding="utf-8")
            args = type("Args", (), {"request_directory": str(request.directory)})()
            with patch("chat_courier.model._load_registry", return_value={"P": "https://chatgpt.com/c/x"}), \
                    patch("chat_courier.cli.read_owner", return_value=None):
                self.assertEqual(retry_once_command(args), 2)
            save_latest_probe(request, user_turn_found=False, reply_found=False,
                              live_owner_found=False)
            probe = json.loads(probe_path.read_text(encoding="utf-8"))
            probe["fingerprint"] = "0" * 64
            probe_path.write_text(json.dumps(probe), encoding="utf-8")
            with patch("chat_courier.model._load_registry", return_value={"P": "https://chatgpt.com/c/x"}), \
                    patch("chat_courier.cli.read_owner", return_value=None):
                self.assertEqual(retry_once_command(args), 2)

    def test_resend_archives_a_previously_accepted_ui_error(self):
        with tempfile.TemporaryDirectory() as value:
            request = self.request(Path(value))
            (request.directory / "events.jsonl").write_text(
                json.dumps({"event": "request_submitted", "project_id": "P",
                            "request_id": "P-1"}) + "\n", encoding="utf-8")
            (request.directory / "response.txt").write_text(
                "This content can't be shown", encoding="utf-8")
            (request.directory / "response.raw.txt").write_text(
                "This content can't be shown", encoding="utf-8")
            (request.directory / "response-capture.json").write_text("{}", encoding="utf-8")
            receipt(request, "response_received", "legacy false positive")
            args = type("Args", (), {"request_directory": str(request.directory)})()
            with patch("chat_courier.model._load_registry", return_value={"P": "https://chatgpt.com/c/x"}), \
                    patch("chat_courier.cli.run_command", return_value=0) as run:
                self.assertEqual(resend_once_command(args), 0)
                self.assertTrue(args.resend_once)
                run.assert_called_once_with(args)
            self.assertFalse((request.directory / "response.txt").exists())
            self.assertEqual(
                (request.directory / "attempt-1-response.txt").read_text(encoding="utf-8"),
                "This content can't be shown",
            )

    def test_resend_archive_never_leaves_current_capture_when_prior_name_exists(self):
        with tempfile.TemporaryDirectory() as value:
            request = self.request(Path(value))
            (request.directory / "events.jsonl").write_text(
                json.dumps({"event": "request_submitted", "project_id": "P",
                            "request_id": "P-1"}) + "\n", encoding="utf-8")
            for name in ("response.txt", "response.raw.txt", "response-capture.json"):
                (request.directory / f"attempt-1-{name}").write_text("old", encoding="utf-8")
                (request.directory / name).write_text("current", encoding="utf-8")
            args = type("Args", (), {"request_directory": str(request.directory)})()
            with patch("chat_courier.model._load_registry", return_value={"P": "https://chatgpt.com/c/x"}), \
                    patch("chat_courier.cli.run_command", return_value=0):
                self.assertEqual(resend_once_command(args), 0)
            for name in ("response.txt", "response.raw.txt", "response-capture.json"):
                self.assertFalse((request.directory / name).exists())
                self.assertEqual(
                    (request.directory / f"attempt-1-2-{name}").read_text(encoding="utf-8"),
                    "current",
                )

    def test_verified_same_project_rollover_preserves_old_state_and_resets_active_count(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            (root / "message.txt").write_text("immutable report", encoding="utf-8")
            (root / "request.json").write_text(json.dumps({
                "version": 1, "project_id": "P", "request_id": "P-1",
            }), encoding="utf-8")
            old_url = "https://chatgpt.com/g/g-p-project/c/old"
            new_url = "https://chatgpt.com/g/g-p-project/c/new"
            with patch("chat_courier.model._load_registry", return_value={"P": old_url}):
                old_request = load_request(root)
            receipt(old_request, "response_received", "old target exhausted")
            (root / "response.txt").write_text(
                "You've reached the maximum length for this conversation, but you can keep talking by starting a new chat.",
                encoding="utf-8",
            )
            (root / "events.jsonl").write_text(
                "\n".join(json.dumps({"event": "request_submitted", "project_id": "P",
                                       "request_id": "P-1"}) for _ in range(2)) + "\n",
                encoding="utf-8",
            )
            args = type("Args", (), {"request_directory": str(root)})()
            with patch("chat_courier.model._load_registry", return_value={"P": new_url}):
                active_request = load_request(root)

            class Session:
                profile = Path("profile")
                def __init__(self, *_args, **_kwargs): pass
                def __enter__(self): return self
                def __exit__(self, *_args): pass
                def prepare_successor_project_chat(self): pass
                def submit(self, *_args): return {"baseline"}
                def wait_for_successor_url(self): return new_url

            class Queue:
                def complete(self): pass

            with patch("chat_courier.cli.load_request", side_effect=[old_request, active_request]), \
                    patch("chat_courier.cli.ChatSession", Session), \
                    patch("chat_courier.cli._wait_for_queue", return_value=(Queue(), None)), \
                    patch("chat_courier.cli.commit_conversation_rollover"), \
                    patch("chat_courier.cli._capture_response", return_value="response_captured"), \
                    patch("chat_courier.cli._parse_captured_response",
                          return_value=("response_received", "continued", {})):
                self.assertEqual(rollover_target_command(args), 0)
            self.assertEqual(submission_count(active_request), 1)
            self.assertEqual(submission_count(active_request, total=True), 3)
            self.assertTrue((root / "target-generation-1" / "receipt.json").is_file())
            self.assertTrue((root / "target-generation-1" / "response.txt").is_file())
            self.assertEqual((root / "response.txt").read_text(encoding="utf-8"), "continued")

    def test_prepared_rollover_can_retry_once_after_explicit_authority_and_absence_proof(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            (root / "message.txt").write_text("immutable closeout", encoding="utf-8")
            (root / "request.json").write_text(json.dumps({
                "version": 1, "project_id": "P", "request_id": "P-1",
            }), encoding="utf-8")
            old_url = "https://chatgpt.com/g/g-p-project/c/old"
            new_url = "https://chatgpt.com/g/g-p-project/c/new"
            with patch("chat_courier.model._load_registry", return_value={"P": old_url}):
                old_request = load_request(root)
            with patch("chat_courier.model._load_registry", return_value={"P": new_url}):
                active_request = load_request(root)
            event(old_request, "request_submitted", phase="submit", submission_attempt=1)
            (root / "target-generation-1").mkdir()
            (root / "target-rollover.json").write_text(json.dumps({
                "version": 1,
                "phase": "prepared",
                "project_id": "P",
                "request_id": "P-1",
                "source_url": old_url,
                "source_fingerprint": old_request.fingerprint,
                "archive_directory": "target-generation-1",
                "basis": "verified_context_capacity",
                "prior_total_submission_count": 1,
            }), encoding="utf-8")
            (root / "rollover-recovery-diagnostic.json").write_text(json.dumps({
                "page_url": "https://chatgpt.com/g/g-p-project/project",
                "authentication_required": False,
                "access_denied": False,
                "rate_limited": False,
                "match_count": 0,
            }), encoding="utf-8")
            submitted: list[str] = []

            class Session:
                def __init__(self, *_args, **_kwargs): pass
                def __enter__(self): return self
                def __exit__(self, *_args): pass
                def recover_successor_url(self, _marker):
                    raise BrowserError("no successor marker")
                def prepare_successor_project_chat(self): pass
                def submit(self, prompt, _attachments):
                    submitted.append(prompt)
                    return {"baseline"}
                def wait_for_successor_url(self): return new_url

            class Queue:
                def complete(self): pass

            args = type("Args", (), {
                "request_directory": str(root),
                "basis": "verified_context_capacity",
                "prepared_retry_authorized": True,
            })()
            with patch("chat_courier.cli.load_request",
                       side_effect=[old_request, active_request, active_request]), \
                    patch("chat_courier.cli.ChatSession", Session), \
                    patch("chat_courier.cli._wait_for_queue", return_value=(Queue(), None)), \
                    patch("chat_courier.cli.commit_conversation_rollover"), \
                    patch("chat_courier.cli.run_command", return_value=0):
                self.assertEqual(rollover_target_command(args), 0)
            self.assertEqual(len(submitted), 1)
            self.assertIn("CHAT_COURIER_ROLLOVER_RECOVERY_NOTICE/1", submitted[0])
            self.assertEqual(submission_count(active_request), 1)
            self.assertTrue(any(
                value.get("event") == "target_rollover_prepared_retry_authorized"
                for value in request_events(active_request)
            ))

    def test_user_direct_rollover_requires_and_uses_fresh_handoff_request(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            (root / "message.txt").write_text("handoff", encoding="utf-8")
            (root / "request.json").write_text(json.dumps({
                "version": 1, "project_id": "P", "request_id": "P-HANDOFF-1",
            }), encoding="utf-8")
            old_url = "https://chatgpt.com/g/g-p-project/c/old"
            new_url = "https://chatgpt.com/g/g-p-project/c/new"
            with patch("chat_courier.model._load_registry", return_value={"P": old_url}):
                request = load_request(root)

            class Session:
                profile = Path("profile")
                def __init__(self, *_args, **kwargs):
                    if kwargs.get("inspect_project") is not True:
                        raise AssertionError("rollover must not wait for the old composer")
                def __enter__(self): return self
                def __exit__(self, *_args): pass
                def prepare_successor_project_chat(self): pass
                def submit(self, *_args): return {"baseline"}
                def wait_for_successor_url(self): return new_url

            class Queue:
                def complete(self): pass

            args = type("Args", (), {
                "request_directory": str(root), "basis": "user_direct",
            })()
            with patch("chat_courier.cli.load_request", side_effect=[request, request]), \
                    patch("chat_courier.cli.ChatSession", Session), \
                    patch("chat_courier.cli._wait_for_queue", return_value=(Queue(), None)), \
                    patch("chat_courier.cli.commit_conversation_rollover") as commit, \
                    patch("chat_courier.cli._capture_response", return_value="response_captured"), \
                    patch("chat_courier.cli._parse_captured_response",
                          return_value=("response_received", "ready", {})):
                self.assertEqual(rollover_target_command(args), 0)
            commit.assert_called_once_with("P", old_url, new_url, basis="user_direct")
            self.assertEqual(submission_count(request), 1)
            self.assertFalse((root / "target-generation-1").exists())

    def test_user_direct_rollover_archives_one_confirmed_busy_submission(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            (root / "message.txt").write_text("handoff", encoding="utf-8")
            (root / "request.json").write_text(json.dumps({
                "version": 1, "project_id": "P", "request_id": "P-HANDOFF-1",
            }), encoding="utf-8")
            old_url = "https://chatgpt.com/g/g-p-project/c/old"
            new_url = "https://chatgpt.com/g/g-p-project/c/new"
            with patch("chat_courier.model._load_registry", return_value={"P": old_url}):
                old_request = load_request(root)
            event(old_request, "request_submitted", phase="submit", submission_attempt=1)
            receipt(old_request, "chat_busy_reconnecting", "stuck generation",
                    same_request_preserved=True, agent_action_required=False)
            with patch("chat_courier.model._load_registry", return_value={"P": new_url}):
                active_request = load_request(root)

            class Session:
                profile = Path("profile")
                def __init__(self, *_args, **_kwargs): pass
                def __enter__(self): return self
                def __exit__(self, *_args): pass
                def prepare_successor_project_chat(self): pass
                def submit(self, *_args): return {"baseline"}
                def wait_for_successor_url(self): return new_url

            class Queue:
                def complete(self): pass

            args = type("Args", (), {
                "request_directory": str(root), "basis": "user_direct",
            })()
            with patch("chat_courier.cli.load_request",
                       side_effect=[old_request, active_request]), \
                    patch("chat_courier.cli.ChatSession", Session), \
                    patch("chat_courier.cli._wait_for_queue", return_value=(Queue(), None)), \
                    patch("chat_courier.cli.commit_conversation_rollover"), \
                    patch("chat_courier.cli._capture_response", return_value="response_captured"), \
                    patch("chat_courier.cli._parse_captured_response",
                          return_value=("response_received", "ready", {})):
                self.assertEqual(rollover_target_command(args), 0)
            self.assertEqual(submission_count(active_request), 1)
            self.assertEqual(submission_count(active_request, total=True), 2)
            self.assertTrue((root / "target-generation-1" / "receipt.json").is_file())

    def test_user_direct_rollover_accepts_proven_unsent_request(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            (root / "message.txt").write_text("handoff", encoding="utf-8")
            (root / "request.json").write_text(json.dumps({
                "version": 1, "project_id": "P", "request_id": "P-HANDOFF-1",
            }), encoding="utf-8")
            old_url = "https://chatgpt.com/g/g-p-project/c/old"
            new_url = "https://chatgpt.com/g/g-p-project/c/new"
            with patch("chat_courier.model._load_registry", return_value={"P": old_url}):
                request = load_request(root)
            event(request, "submission_not_started", phase="submit",
                  safe_to_retry_same_request=True)
            receipt(request, "submission_not_started", "composer remained busy",
                    safe_to_retry_same_request=True)
            receipt(request, "queue_turn_acquired", "failed rollover replaced the receipt")

            class Session:
                profile = Path("profile")
                def __init__(self, *_args, **_kwargs): pass
                def __enter__(self): return self
                def __exit__(self, *_args): pass
                def prepare_successor_project_chat(self): pass
                def submit(self, *_args): return {"baseline"}
                def wait_for_successor_url(self): return new_url

            class Queue:
                def complete(self): pass

            args = type("Args", (), {
                "request_directory": str(root), "basis": "user_direct",
            })()
            with patch("chat_courier.cli.load_request", side_effect=[request, request]), \
                    patch("chat_courier.cli.ChatSession", Session), \
                    patch("chat_courier.cli._wait_for_queue", return_value=(Queue(), None)), \
                    patch("chat_courier.cli.commit_conversation_rollover"), \
                    patch("chat_courier.cli._capture_response", return_value="response_captured"), \
                    patch("chat_courier.cli._parse_captured_response",
                          return_value=("response_received", "ready", {})):
                self.assertEqual(rollover_target_command(args), 0)
            self.assertEqual(submission_count(request), 1)

    def test_user_direct_rollover_accepts_unconfirmed_action_proven_unsent(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            (root / "message.txt").write_text("handoff", encoding="utf-8")
            (root / "request.json").write_text(json.dumps({
                "version": 1, "project_id": "P", "request_id": "P-HANDOFF-1",
            }), encoding="utf-8")
            old_url = "https://chatgpt.com/g/g-p-project/c/old"
            new_url = "https://chatgpt.com/g/g-p-project/c/new"
            with patch("chat_courier.model._load_registry", return_value={"P": old_url}):
                request = load_request(root)
            event(request, "chat_submission_unconfirmed", phase="submit")
            receipt(request, "submission_unconfirmed", "send was not visibly accepted")
            (root / "submission_diagnostic.json").write_text(json.dumps({
                "request_id": "P-HANDOFF-1",
                "marker": "REQUEST_ID=P-HANDOFF-1",
                "composer_contains_marker": True,
                "user_turns_with_marker": [],
                "page_url": old_url,
            }), encoding="utf-8")

            class Session:
                profile = Path("profile")
                def __init__(self, *_args, **_kwargs): pass
                def __enter__(self): return self
                def __exit__(self, *_args): pass
                def prepare_successor_project_chat(self): pass
                def submit(self, *_args): return {"baseline"}
                def wait_for_successor_url(self): return new_url

            class Queue:
                def complete(self): pass

            args = type("Args", (), {
                "request_directory": str(root), "basis": "user_direct",
                "prepared_retry_authorized": False,
            })()
            with patch("chat_courier.cli.load_request", side_effect=[request, request]), \
                    patch("chat_courier.cli.ChatSession", Session), \
                    patch("chat_courier.cli._wait_for_queue", return_value=(Queue(), None)), \
                    patch("chat_courier.cli.commit_conversation_rollover"), \
                    patch("chat_courier.cli._capture_response", return_value="response_captured"), \
                    patch("chat_courier.cli._parse_captured_response",
                          return_value=("response_received", "ready", {})):
                self.assertEqual(rollover_target_command(args), 0)
            self.assertEqual(submission_count(request), 1)
            self.assertTrue((root / "target-generation-1" / "submission_diagnostic.json").is_file())
            self.assertTrue((root / "target-generation-1" / "receipt.json").is_file())

    def test_user_direct_rollover_rejects_previously_used_request(self):
        with tempfile.TemporaryDirectory() as value:
            request = self.request(Path(value))
            receipt(request, "response_received", "already used")
            args = type("Args", (), {
                "request_directory": str(request.directory), "basis": "user_direct",
            })()
            self.assertEqual(rollover_target_command(args), 2)

    def test_queue_provenance_survives_final_receipt_transition(self):
        with tempfile.TemporaryDirectory() as value:
            request = self.request(Path(value))
            receipt(request, "queue_turn_acquired", "turn", queue_ticket="ticket-1", queue_waited_seconds=12, execution_started_at=100)
            receipt(request, "response_received", "done", response_path="response.txt")
            final = load_receipt(request)
        self.assertEqual(final["queue_ticket"], "ticket-1")
        self.assertEqual(final["queue_waited_seconds"], 12)
        self.assertEqual(final["execution_started_at"], 100)

    def test_queue_turn_without_an_owner_is_safe_to_recover_before_submission(self):
        with patch("chat_courier.cli.read_owner", return_value=None):
            self.assertTrue(_safe_pre_browser_turn_recovery({"state": "queue_turn_acquired"}))
        with patch("chat_courier.cli.read_owner", return_value=object()):
            self.assertFalse(_safe_pre_browser_turn_recovery({"state": "queue_turn_acquired"}))
        with patch("chat_courier.cli.read_owner", return_value=None):
            self.assertTrue(_safe_pre_browser_turn_recovery({"state": "courier_interrupted", "interruption_stage": "pre_browser"}))
        self.assertFalse(_safe_pre_browser_turn_recovery({"state": "submission_intent"}))
