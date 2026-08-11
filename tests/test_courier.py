from __future__ import annotations

import base64
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from gmail_courier.config import CourierConfig, ProjectConfig, load_config
from gmail_courier.core import State, assert_project_repo, commit_and_push, parse_subject, receive_message, route_project, safe_name


class Request:
    def __init__(self, value, error=None):
        self.value = value
        self.error = error

    def execute(self):
        if self.error:
            raise self.error
        return self.value


class Attachments:
    def __init__(self, data=b"payload", error=None):
        self.data = data
        self.error = error
        self.downloads = 0

    def get(self, **_):
        self.downloads += 1
        encoded = base64.urlsafe_b64encode(self.data).decode().rstrip("=")
        return Request({"data": encoded}, self.error)


class Messages:
    def __init__(self, message, attachments):
        self.message = message
        self.attachments_api = attachments

    def get(self, **_):
        return Request(self.message)

    def attachments(self):
        return self.attachments_api

    def list(self, **_):
        return Request({"messages": [{"id": self.message["id"]}]})


class Users:
    def __init__(self, messages):
        self.messages_api = messages

    def messages(self):
        return self.messages_api


class Gmail:
    def __init__(self, message, data=b"payload", error=None):
        self.attachments_api = Attachments(data, error)
        self.users_api = Users(Messages(message, self.attachments_api))

    def users(self):
        return self.users_api


def project(root: Path, *, code="ALPHA", push=False, branch="main", legacy=()) -> ProjectConfig:
    return ProjectConfig(code, (), root, Path("inbox"), branch, "origin", None, push, tuple(legacy))


def config_for(projects: tuple[ProjectConfig, ...]) -> CourierConfig:
    return CourierConfig("me@example.com", "[GMAIL-COURIER]", "[GC-BRIDGE]", projects)


def message(subject="[GMAIL-COURIER][ALPHA][TASK] test", attachments=True):
    payload = {"headers": [
        {"name": "Subject", "value": subject},
        {"name": "From", "value": "me@example.com"},
        {"name": "To", "value": "me@example.com"},
    ]}
    if attachments:
        payload["parts"] = [{"filename": "task.md", "body": {"attachmentId": "a1"}}]
    return {"id": "gmail-1", "threadId": "thread-1", "internalDate": "1", "payload": payload}


class CourierTests(unittest.TestCase):
    def test_subject_routes_by_exact_code_and_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            alpha = project(Path(tmp), code="ALPHA")
            beta = ProjectConfig("BETA", ("B",), Path(tmp), Path("inbox2"), "main", "origin", None, False, ())
            config = config_for((alpha, beta))
            info = parse_subject("[GMAIL-COURIER][b][AUDIT] report", config)
            routed, reason = route_project(info, config)
            self.assertEqual(routed.code, "BETA")
            self.assertIsNone(reason)
            self.assertIsNone(parse_subject("ordinary mail", config))

    def test_legacy_subject_requires_explicit_project_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            registered = project(Path(tmp), legacy=("[GC-BRIDGE]",))
            info = parse_subject("[GC-BRIDGE][TASK] old", config_for((registered,)))
            routed, reason = route_project(info, config_for((registered,)))
            self.assertEqual(routed.code, "ALPHA")
            self.assertIsNone(reason)

    def test_filename_sanitization(self):
        for raw in ("../escape.py", "C:\\evil.ps1", "CON", "", ".."):
            value = safe_name(raw)
            self.assertNotIn("/", value)
            self.assertNotIn("\\", value)
            self.assertNotIn(value, {"", ".", ".."})

    def test_receive_is_atomic_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            runtime = Path(tmp) / "runtime"
            config = config_for((project(root),))
            full = message()
            with patch("gmail_courier.core.commit_and_push") as publish:
                state = State(runtime)
                service = Gmail(full)
                self.assertEqual(receive_message(service, config, state, {"id": "gmail-1"}, runtime), "received")
                state.mark("gmail-1", committed=1, pushed=1)
                self.assertEqual(receive_message(service, config, state, {"id": "gmail-1"}, runtime), "duplicate")
                self.assertEqual(service.attachments_api.downloads, 1)
                manifest = next((root / "inbox").glob("*/manifest.json"))
                data = json.loads(manifest.read_text(encoding="utf-8"))
                self.assertEqual(data["attachments"][0]["filename"], "task.md")
                self.assertEqual(data["attachments"][0]["sha256"], __import__("hashlib").sha256(b"payload").hexdigest())
                self.assertEqual(publish.call_count, 1)
                state.close()

    def test_failed_download_leaves_no_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            runtime = Path(tmp) / "runtime"
            state = State(runtime)
            try:
                with self.assertRaisesRegex(RuntimeError, "download failed"):
                    receive_message(Gmail(message(), error=RuntimeError("download failed")), config_for((project(root),)), state, {"id": "gmail-1"}, runtime)
                self.assertFalse((root / "inbox").exists())
                self.assertIsNone(state.get("gmail-1"))
            finally:
                state.close()

    def test_unknown_project_is_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            runtime = Path(tmp) / "runtime"
            state = State(runtime)
            result = receive_message(Gmail(message("[GMAIL-COURIER][UNKNOWN][TASK] bad")), config_for((project(root),)), state, {"id": "gmail-1"}, runtime)
            self.assertEqual(result, "quarantined")
            self.assertTrue((runtime / "quarantine" / "gmail-1.json").exists())
            self.assertFalse((root / "inbox").exists())
            state.close()

    def test_no_attachments_is_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            runtime = Path(tmp) / "runtime"
            state = State(runtime)
            result = receive_message(Gmail(message(attachments=False)), config_for((project(root),)), state, {"id": "gmail-1"}, runtime)
            self.assertEqual(result, "quarantined")
            self.assertEqual(json.loads((runtime / "quarantine" / "gmail-1.json").read_text())["reason"], "no-attachments")
            state.close()

    def test_git_commit_only_delivery_preserves_unrelated_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.run_git(root, "init", "-b", "main")
            self.run_git(root, "config", "user.email", "test@example.com")
            self.run_git(root, "config", "user.name", "Test")
            unrelated = root / "unrelated.txt"
            unrelated.write_text("before", encoding="utf-8")
            self.run_git(root, "add", "unrelated.txt")
            self.run_git(root, "commit", "-m", "initial")
            unrelated.write_text("after", encoding="utf-8")
            delivery = root / "inbox" / "task"
            delivery.mkdir(parents=True)
            (delivery / "manifest.json").write_text("{}", encoding="utf-8")
            state = State(root / ".runtime")
            state.record("gmail-1", "ALPHA", "inbox/task")
            commit_and_push(project(root), state, state.get("gmail-1"))
            self.assertIn(" M unrelated.txt", self.run_git(root, "status", "--short"))
            self.assertEqual(self.run_git(root, "log", "-1", "--pretty=%s").strip(), "Receive Gmail Courier delivery task")
            state.close()

    def test_wrong_branch_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.run_git(root, "init", "-b", "main")
            configured = project(root, branch="chat")
            with self.assertRaisesRegex(RuntimeError, "expected branch chat"):
                assert_project_repo(configured)

    def test_config_rejects_duplicate_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "projects.toml"
            path.write_text(
                '[account]\naddress="me@example.com"\n\n'
                '[[projects]]\ncode="A"\nroot="C:/a"\nbranch="main"\n\n'
                '[[projects]]\ncode="A"\nroot="C:/b"\nbranch="main"\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "ambiguous"):
                load_config(Path(tmp))

    @staticmethod
    def run_git(root: Path, *args: str) -> str:
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True)
        return result.stdout


if __name__ == "__main__":
    unittest.main()
