"""Read-only ChatGPT assistant-to-local transport.

This module is deliberately narrower than the existing Gmail and outbound
browser paths.  It reads one completed assistant turn from a configured
conversation, accepts only a versioned machine envelope, and records a
durable receipt before exposing the payload in the local inbox.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from typing import Any, Callable
from urllib.parse import urlsplit

from .config import chat_urls_match
from .storage import atomic_json


PROTOCOL = "AGENTRELAY_OUTBOUND/1"
BEGIN_PAYLOAD = "BEGIN_PAYLOAD"
END_PAYLOAD = "END_PAYLOAD"
PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
WORK_ORDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
ACTION_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,63}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ChatReadError(ValueError):
    """A fail-closed read, envelope, or replay-protection error."""


class ChatReadNotReady(RuntimeError):
    """The newest assistant response exists but is not demonstrably complete."""


class ChatReadReplayConflict(ChatReadError):
    """A work-order or message identity was reused with different content."""


class ChatReadNoEnvelope(LookupError):
    """The completed assistant response contains no machine envelope."""


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant is not allowed: {value}")


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def canonical_payload(payload: dict[str, Any]) -> str:
    """Return the exact canonical JSON representation used for hashing."""
    if not isinstance(payload, dict):
        raise ChatReadError("payload must be a JSON object")
    try:
        text = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        text.encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ChatReadError("payload cannot be canonicalized as UTF-8 JSON") from exc
    return text


def payload_sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_payload(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class OutboundEnvelope:
    project_id: str
    work_order_id: str
    action: str
    payload_sha256: str
    payload: dict[str, Any]
    canonical_payload: str
    raw_envelope: str


def parse_outbound_envelope(text: str, *, project_id: str) -> OutboundEnvelope | None:
    """Parse one strict envelope from an assistant response.

    Ordinary prose before or after the envelope is ignored as non-authoritative.
    If the protocol marker is present but malformed, this raises instead of
    searching for a more convenient interpretation.
    """
    if not isinstance(text, str):
        raise ChatReadError("assistant content is not text")
    if not isinstance(project_id, str) or not PROJECT_RE.fullmatch(project_id):
        raise ChatReadError("configured project_id is invalid")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    marker_indexes = [index for index, line in enumerate(lines) if line == PROTOCOL]
    unsupported_markers = [line for line in lines if line.startswith("AGENTRELAY_OUTBOUND/") and line != PROTOCOL]
    if unsupported_markers:
        raise ChatReadError("unsupported outbound envelope version")
    if not marker_indexes:
        return None
    if len(marker_indexes) != 1:
        raise ChatReadError("multiple outbound envelope markers")
    start = marker_indexes[0]
    if start + 6 >= len(lines):
        raise ChatReadError("incomplete outbound envelope")
    expected_fields = ("PROJECT_ID", "WORK_ORDER_ID", "ACTION", "PAYLOAD_SHA256")
    values: dict[str, str] = {}
    for offset, name in enumerate(expected_fields, start=1):
        line = lines[start + offset]
        prefix = f"{name}="
        if not line.startswith(prefix) or name in values:
            raise ChatReadError(f"missing or malformed {name}")
        value = line[len(prefix):]
        if not value or "\n" in value:
            raise ChatReadError(f"empty or malformed {name}")
        values[name] = value
    begin_index = start + 5
    if lines[begin_index] != BEGIN_PAYLOAD:
        raise ChatReadError("missing BEGIN_PAYLOAD")
    try:
        end_index = lines.index(END_PAYLOAD, begin_index + 1)
    except ValueError as exc:
        raise ChatReadError("missing END_PAYLOAD") from exc
    payload_text = "\n".join(lines[begin_index + 1:end_index])
    if not payload_text.strip():
        raise ChatReadError("empty payload")
    if any(line == PROTOCOL for line in lines[end_index + 1:]):
        raise ChatReadError("multiple outbound envelope markers")
    if values["PROJECT_ID"] != project_id:
        raise ChatReadError("outbound project_id does not match configured project")
    if not WORK_ORDER_RE.fullmatch(values["WORK_ORDER_ID"]):
        raise ChatReadError("invalid WORK_ORDER_ID")
    if not ACTION_RE.fullmatch(values["ACTION"]):
        raise ChatReadError("invalid ACTION")
    if not SHA256_RE.fullmatch(values["PAYLOAD_SHA256"]):
        raise ChatReadError("invalid PAYLOAD_SHA256")
    try:
        payload = json.loads(
            payload_text,
            parse_constant=_reject_constant,
            object_pairs_hook=_reject_duplicate_pairs,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
        raise ChatReadError("payload is not strict UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ChatReadError("payload must be a JSON object")
    canonical = canonical_payload(payload)
    actual_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if actual_hash != values["PAYLOAD_SHA256"]:
        raise ChatReadError("PAYLOAD_SHA256 does not match canonical payload")
    raw = "\n".join(lines[start:end_index + 1])
    return OutboundEnvelope(
        values["PROJECT_ID"],
        values["WORK_ORDER_ID"],
        values["ACTION"],
        values["PAYLOAD_SHA256"],
        payload,
        canonical,
        raw,
    )


def is_chat_read_url(value: str) -> bool:
    """Accept normal conversation URLs and share URLs for read-only use."""
    try:
        parsed = urlsplit(str(value))
    except ValueError:
        return False
    if parsed.scheme.lower() != "https" or parsed.hostname is None:
        return False
    if parsed.hostname.lower() not in {"chatgpt.com", "www.chatgpt.com"}:
        return False
    if parsed.port is not None or parsed.username or parsed.password:
        return False
    parts = [part for part in parsed.path.split("/") if part]
    return len(parts) == 2 and parts[0] in {"c", "share"} and bool(parts[1])


def read_url_matches(page_url: str, configured_url: str) -> bool:
    if chat_urls_match(page_url, configured_url):
        return True
    if not is_chat_read_url(page_url) or not is_chat_read_url(configured_url):
        return False
    left = [part for part in urlsplit(page_url).path.split("/") if part]
    right = [part for part in urlsplit(configured_url).path.split("/") if part]
    return left == right


@dataclass(frozen=True)
class AssistantMessage:
    identity: str
    text: str
    source_url: str


@dataclass(frozen=True)
class ReadResult:
    event: str
    detail: str
    work_order_path: Path | None = None
    envelope: OutboundEnvelope | None = None
    message_identity: str | None = None


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


class ChatGPTDOMReader:
    """Centralized selectors and completion checks for the rendered Chat UI."""

    ASSISTANT_MESSAGES = "[data-message-author-role='assistant']"
    STREAMING_ATTRIBUTES = ("data-is-streaming", "data-message-is-streaming", "aria-busy")
    STOP_BUTTON = "button[aria-label*='Stop'], button:has-text('Stop generating')"

    def __init__(self, page: Any, *, stability_wait_seconds: float = 0.35, initial_render_wait_seconds: float = 15.0):
        self.page = page
        self.stability_wait_seconds = stability_wait_seconds
        self.initial_render_wait_seconds = initial_render_wait_seconds

    def _message_is_streaming(self, message: Any) -> bool:
        for attribute in self.STREAMING_ATTRIBUTES:
            if _truthy(message.get_attribute(attribute)):
                return True
        return False

    def _stop_button_visible(self) -> bool:
        try:
            buttons = self.page.locator(self.STOP_BUTTON)
            for index in range(buttons.count()):
                if buttons.nth(index).is_visible():
                    return True
        except Exception:
            return False
        return False

    def latest_completed_assistant(self) -> AssistantMessage | None:
        messages = self.page.locator(self.ASSISTANT_MESSAGES)
        count = messages.count()
        deadline = time.monotonic() + self.initial_render_wait_seconds
        while count == 0 and time.monotonic() < deadline:
            try:
                self.page.wait_for_timeout(250)
            except Exception:
                time.sleep(0.25)
            count = messages.count()
        if not count:
            return None
        message = messages.nth(count - 1)
        if self._message_is_streaming(message) or self._stop_button_visible():
            raise ChatReadNotReady("newest assistant message is still generating")
        first = message.inner_text(timeout=5000)
        if not first.strip():
            raise ChatReadNotReady("newest assistant message has no visible text")
        time.sleep(self.stability_wait_seconds)
        if self._message_is_streaming(message) or self._stop_button_visible():
            raise ChatReadNotReady("assistant generation became active during stability check")
        second = message.inner_text(timeout=5000)
        if first != second:
            raise ChatReadNotReady("assistant text changed during stability check")
        message_id = message.get_attribute("data-message-id")
        identity = message_id or "dom-text:" + hashlib.sha256(second.encode("utf-8")).hexdigest()
        return AssistantMessage(identity, second, str(self.page.url))


def _state_path(root: Path) -> Path:
    return root / "chatgpt" / "outbound_receipts.json"


def _load_receipts(root: Path) -> dict[str, Any]:
    path = _state_path(root)
    if not path.exists():
        return {"version": 1, "records": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ChatReadError(f"malformed ChatGPT receipt state: {path}") from exc
    if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("records"), dict):
        raise ChatReadError("malformed ChatGPT receipt state schema")
    return value


def _find_existing_work_order(root: Path, work_order_id: str) -> dict[str, Any] | None:
    inbox = root / "inbox" / "chatgpt"
    if not inbox.exists():
        return None
    for manifest_path in inbox.glob("*/manifest.json"):
        try:
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if value.get("work_order_id") == work_order_id:
            value["_path"] = str(manifest_path.parent)
            return value
    return None


def consume_once(root: Path, message: AssistantMessage, envelope: OutboundEnvelope) -> ReadResult:
    """Persist one validated work order and reject replay/conflict."""
    state = _load_receipts(root)
    records = state["records"]
    existing = records.get(envelope.work_order_id)
    if existing is not None:
        if existing.get("payload_sha256") != envelope.payload_sha256:
            raise ChatReadReplayConflict("work-order ID was reused with changed payload")
        return ReadResult("chat_work_order_duplicate", "work order was already consumed", message_identity=message.identity)
    recovered = _find_existing_work_order(root, envelope.work_order_id)
    if recovered is not None:
        if recovered.get("payload_sha256") != envelope.payload_sha256:
            raise ChatReadReplayConflict("existing work-order directory has a different payload")
        records[envelope.work_order_id] = recovered | {"recovered": True}
        atomic_json(_state_path(root), state)
        return ReadResult("chat_work_order_duplicate", "work order was recovered from durable inbox", Path(recovered["_path"]), envelope, message.identity)
    for prior in records.values():
        if prior.get("message_identity") == message.identity and prior.get("payload_sha256") != envelope.payload_sha256:
            raise ChatReadReplayConflict("assistant message identity was reused with changed content")
    safe_id = re.sub(r"[^A-Za-z0-9._-]", "_", envelope.work_order_id)[:96]
    target = root / "inbox" / "chatgpt" / f"{safe_id}-{envelope.payload_sha256[:12]}"
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="chatgpt-work-order-", dir=target.parent) as temp_name:
        temp = Path(temp_name)
        (temp / "payload.json").write_text(envelope.canonical_payload, encoding="utf-8", newline="\n")
        (temp / "envelope.txt").write_text(envelope.raw_envelope + "\n", encoding="utf-8", newline="\n")
        (temp / "assistant_message.txt").write_text(message.text, encoding="utf-8", newline="\n")
        atomic_json(temp / "manifest.json", {
            "transport": "chatgpt_dom_read",
            "protocol": PROTOCOL,
            "project_id": envelope.project_id,
            "work_order_id": envelope.work_order_id,
            "action": envelope.action,
            "payload_sha256": envelope.payload_sha256,
            "message_identity": message.identity,
            "source_url": message.source_url,
        })
        os.replace(temp, target)
    record = {
        "work_order_id": envelope.work_order_id,
        "payload_sha256": envelope.payload_sha256,
        "message_identity": message.identity,
        "source_url": message.source_url,
        "path": str(target),
        "consumed_at": datetime.now(UTC).isoformat(),
    }
    records[envelope.work_order_id] = record
    atomic_json(_state_path(root), state)
    return ReadResult("chat_work_order_received", "validated work order consumed once", target, envelope, message.identity)


class ChatGPTReadRelay:
    def __init__(self, *, root: Path, project_id: str, chat_url: str, stability_wait_seconds: float = 0.35, sender_factory: Callable[[str], Any] | None = None):
        if not is_chat_read_url(chat_url):
            raise ChatReadError("chat_url must be an HTTPS /c/<id> or /share/<id> URL")
        if not PROJECT_RE.fullmatch(project_id):
            raise ChatReadError("project_id is invalid")
        self.root = root.resolve()
        self.project_id = project_id
        self.chat_url = chat_url
        self.stability_wait_seconds = stability_wait_seconds
        self.sender_factory = sender_factory

    def _sender(self) -> Any:
        if self.sender_factory is not None:
            return self.sender_factory(self.chat_url)
        from .chatgpt_sender import BrowserChatGPTSender
        return BrowserChatGPTSender(type("Config", (), {"chat_url": self.chat_url})())

    def read_once(self) -> ReadResult:
        sender = self._sender()
        opened_page = None
        try:
            from playwright.sync_api import sync_playwright
            sender._launch()
            with sync_playwright() as playwright:
                browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{sender.debug_port}")
                context = browser.contexts[0] if browser.contexts else None
                if context is None:
                    raise ChatReadError("Chrome CDP has no browser context")
                page = next((item for item in context.pages if read_url_matches(item.url, self.chat_url)), None)
                if page is None:
                    page = context.new_page()
                    opened_page = page
                    page.goto(self.chat_url, wait_until="domcontentloaded", timeout=120000)
                    if not read_url_matches(page.url, self.chat_url):
                        raise ChatReadError("configured conversation identity was not preserved after navigation")
                latest = ChatGPTDOMReader(page, stability_wait_seconds=self.stability_wait_seconds).latest_completed_assistant()
                if latest is None:
                    return ReadResult("chat_no_assistant_message", "conversation has no visible assistant message")
                envelope = parse_outbound_envelope(latest.text, project_id=self.project_id)
                if envelope is None:
                    return ReadResult("chat_no_work_order", "newest completed assistant message has no outbound envelope", message_identity=latest.identity)
                return consume_once(self.root, latest, envelope)
        except ChatReadNotReady:
            return ReadResult("chat_not_ready", "newest assistant response is not complete")
        finally:
            if opened_page is not None:
                try:
                    opened_page.close()
                except Exception:
                    pass
            if getattr(sender, "owned_process", None) is not None:
                try:
                    sender.owned_process.terminate()
                    sender.owned_process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                sender.owned_process = None


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="agent-relay-chatgpt-read-once")
    parser.add_argument("--root", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--chat-url", required=True)
    args = parser.parse_args(argv)
    try:
        result = ChatGPTReadRelay(root=Path(args.root), project_id=args.project_id, chat_url=args.chat_url).read_once()
    except ChatReadNotReady as exc:
        result = ReadResult("chat_not_ready", str(exc))
    except Exception as exc:
        result = ReadResult("chat_read_error", f"{type(exc).__name__}: {exc}")
    print(json.dumps({
        "event": result.event,
        "detail": result.detail,
        "work_order_path": str(result.work_order_path) if result.work_order_path else None,
        "work_order_id": result.envelope.work_order_id if result.envelope else None,
        "message_identity": result.message_identity,
    }, ensure_ascii=False, sort_keys=True))
    return 0 if result.event in {"chat_work_order_received", "chat_work_order_duplicate", "chat_no_work_order", "chat_no_assistant_message", "chat_not_ready"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
