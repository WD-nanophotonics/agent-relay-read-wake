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

from .config import chat_urls_match, is_chat_url
from .storage import atomic_json
from gmail_courier.protocol import build_chat_read_correction_prompt


PROTOCOL = "AGENTRELAY_OUTBOUND/2"
LEGACY_PROTOCOL = "AGENTRELAY_OUTBOUND/1"
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
    protocol: str = PROTOCOL


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
    supported = {PROTOCOL, LEGACY_PROTOCOL}
    marker_indexes = [index for index, line in enumerate(lines) if line in supported]
    unsupported_markers = [line for line in lines if line.startswith("AGENTRELAY_OUTBOUND/") and line not in supported]
    if unsupported_markers:
        raise ChatReadError("unsupported outbound envelope version")
    if not marker_indexes:
        return None
    if len(marker_indexes) != 1:
        raise ChatReadError("multiple outbound envelope markers")
    start = marker_indexes[0]
    protocol = lines[start]
    expected_fields = ("PROJECT_ID", "WORK_ORDER_ID", "ACTION")
    if protocol == LEGACY_PROTOCOL:
        expected_fields += ("PAYLOAD_SHA256",)
    begin_index = start + 1 + len(expected_fields)
    if begin_index >= len(lines):
        raise ChatReadError("incomplete outbound envelope")
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
    if protocol == LEGACY_PROTOCOL and not SHA256_RE.fullmatch(values["PAYLOAD_SHA256"]):
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
    actual_hash = payload_sha256(payload)
    if protocol == LEGACY_PROTOCOL and actual_hash != values["PAYLOAD_SHA256"]:
        raise ChatReadError("PAYLOAD_SHA256 does not match canonical payload")
    raw = "\n".join(lines[start:end_index + 1])
    return OutboundEnvelope(
        values["PROJECT_ID"],
        values["WORK_ORDER_ID"],
        values["ACTION"],
        actual_hash,
        payload,
        canonical,
        raw,
        protocol,
    )


def is_chat_read_url(value: str) -> bool:
    """Accept normal conversation URLs and share URLs for read-only use."""
    # Registered project conversations may use /g/<project>/c/<id>; the
    # conversation identity is still the /c/<id> component and is supported
    # by the sender and URL registry.
    if is_chat_url(value):
        return True
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


def _is_canonical_conversation_url(value: str) -> bool:
    if not is_chat_read_url(value):
        return False
    parts = [part for part in urlsplit(value).path.split("/") if part]
    return parts[0] == "c"


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


def _assistant_edge_hashes(page: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return constant-size visible anchors for share-to-/c/ linkage."""
    try:
        messages = page.locator(ChatGPTDOMReader.ASSISTANT_MESSAGES)
        texts = []
        for index in range(messages.count()):
            text = messages.nth(index).inner_text(timeout=5000)
            if text.strip():
                texts.append(hashlib.sha256(text.encode("utf-8")).hexdigest())
        return tuple(texts[:2]), tuple(texts[-2:])
    except Exception:
        return (), ()


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
        return ReadResult(
            "chat_work_order_duplicate",
            "work order was already consumed",
            envelope=envelope,
            message_identity=message.identity,
        )
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
            "protocol": envelope.protocol,
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
    def __init__(self, *, root: Path, project_id: str, chat_url: str, work_order_id: str | None = None, stability_wait_seconds: float = 0.35, sender_factory: Callable[[str], Any] | None = None, close_session_page: bool = True):
        if not is_chat_read_url(chat_url):
            raise ChatReadError("chat_url must be an HTTPS /c/<id> or /share/<id> URL")
        if not PROJECT_RE.fullmatch(project_id):
            raise ChatReadError("project_id is invalid")
        self.root = root.resolve()
        self.project_id = project_id
        self.chat_url = chat_url
        if work_order_id is not None and not WORK_ORDER_RE.fullmatch(work_order_id):
            raise ChatReadError("work_order_id is invalid")
        self.work_order_id = work_order_id
        self.stability_wait_seconds = stability_wait_seconds
        self.sender_factory = sender_factory
        self.close_session_page = close_session_page
        self._last_debug_port: int | None = None

    def _sender(self) -> Any:
        if self.sender_factory is not None:
            sender = self.sender_factory(self.chat_url)
            if hasattr(sender, "close_session_page"):
                sender.close_session_page = self.close_session_page
            return sender
        from .chatgpt_sender import BrowserChatGPTSender
        return BrowserChatGPTSender(type("Config", (), {"chat_url": self.chat_url, "close_session_page": self.close_session_page})())

    def _select_page(self, context: Any) -> Any:
        pages = list(context.pages)
        direct = [page for page in pages if read_url_matches(page.url, self.chat_url)]
        if not direct:
            raise ChatReadError("configured ChatGPT conversation is not open")
        selected = direct[0]
        configured_parts = [part for part in urlsplit(self.chat_url).path.split("/") if part]
        if configured_parts[0] != "share":
            if len(direct) > 1:
                raise ChatReadError("multiple pages match configured conversation")
            return selected
        # A writable share copy can open a canonical /c/ page after a prompt is
        # submitted. Link it only with the page title and a two-message
        # content overlap; arbitrary /c/ tabs are never accepted.
        share_page = selected
        try:
            share_title = share_page.title()
        except Exception:
            share_title = ""
        _share_head, share_tail = _assistant_edge_hashes(share_page)
        candidates = []
        for page in pages:
            if not _is_canonical_conversation_url(page.url):
                continue
            try:
                title_matches = bool(share_title) and page.title() == share_title
            except Exception:
                title_matches = False
            candidate_head, _candidate_tail = _assistant_edge_hashes(page)
            overlap = bool(share_tail and candidate_head and share_tail == candidate_head)
            if len(share_tail) == 1 and len(candidate_head) == 1:
                overlap = share_tail == candidate_head
            if title_matches and overlap:
                candidates.append(page)
        if len(candidates) > 1:
            raise ChatReadError("multiple canonical pages match the configured share conversation")
        return candidates[0] if candidates else selected

    @staticmethod
    def _close_page(page: Any | None) -> None:
        if page is None:
            return
        try:
            page.close()
        except Exception:
            pass

    def _read_once_without_correction(self) -> ReadResult:
        sender = self._sender()
        session_page = None
        try:
            from playwright.sync_api import sync_playwright
            sender._launch()
            self._last_debug_port = sender.debug_port
            with sync_playwright() as playwright:
                browser = playwright.chromium.connect_over_cdp(
                    f"http://127.0.0.1:{sender.debug_port}",
                    timeout=getattr(sender, "cdp_connect_timeout_ms", 15000),
                )
                context = browser.contexts[0] if browser.contexts else None
                if context is None:
                    raise ChatReadError("Chrome CDP has no browser context")
                try:
                    page = self._select_page(context)
                except ChatReadError as exc:
                    if "not open" not in str(exc):
                        raise
                    page = context.new_page()
                    # Register before navigation so a navigation timeout does
                    # not leave an orphaned Courier tab behind.
                    session_page = page
                    page.goto(self.chat_url, wait_until="domcontentloaded", timeout=120000)
                    if not read_url_matches(page.url, self.chat_url):
                        raise ChatReadError("configured conversation identity was not preserved after navigation")
                session_page = page
                verify_page = getattr(sender, "_verify_chat_page_identity", None)
                if callable(verify_page) and is_chat_url(self.chat_url):
                    verify_page(page)
                latest = ChatGPTDOMReader(
                    page,
                    stability_wait_seconds=self.stability_wait_seconds,
                    initial_render_wait_seconds=max(60.0, float(getattr(sender, "page_ready_timeout_seconds", 30))),
                ).latest_completed_assistant()
                if latest is None:
                    return ReadResult("chat_no_assistant_message", "conversation has no visible assistant message")
                try:
                    envelope = parse_outbound_envelope(latest.text, project_id=self.project_id)
                except ChatReadError as exc:
                    if self.work_order_id is not None:
                        return ReadResult(
                            "chat_not_ready",
                            f"newest completed assistant response is not for expected work_order_id {self.work_order_id}: {exc}",
                            message_identity=latest.identity,
                        )
                    raise
                if envelope is None:
                    return ReadResult("chat_no_work_order", "newest completed assistant message has no outbound envelope", message_identity=latest.identity)
                if self.work_order_id is not None and envelope.work_order_id != self.work_order_id:
                    return ReadResult(
                        "chat_not_ready",
                        f"newest completed assistant response has work_order_id {envelope.work_order_id!r}, expected {self.work_order_id!r}",
                        message_identity=latest.identity,
                    )
                return consume_once(self.root, latest, envelope)
        except ChatReadNotReady:
            return ReadResult("chat_not_ready", "newest assistant response is not complete")
        finally:
            # The selected page is always the configured conversation (or a
            # page created for it). Close that exact page when requested even
            # if CDP attached to an already-running Chrome process. The
            # external browser process itself remains untouched.
            if getattr(sender, "close_session_page", False):
                close_page = getattr(sender, "_close_session_page", None)
                if callable(close_page):
                    close_page(session_page)
                elif session_page is not None:
                    self._close_page(session_page)
            if getattr(sender, "owned_process", None) is not None:
                try:
                    sender.owned_process.terminate()
                    sender.owned_process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                sender.owned_process = None

    def _close_target_page(self) -> None:
        """Close the configured page after a wait loop reaches a terminal state."""
        sender = self._sender()
        if self._last_debug_port is not None:
            sender.debug_port = self._last_debug_port
        close_page = getattr(sender, "_close_session_page", None)
        if callable(close_page):
            close_page(None)

    def wait_for_work_order(self, *, max_seconds: int, interval_seconds: float = 2.0) -> ReadResult:
        """Probe until the expected completed assistant work order is available.

        This is the formal Chat-only receive phase. It never sends a correction
        while the expected assistant response is absent, stale, or streaming.

        The browser/CDP connection and target page are intentionally opened
        once for the whole wait. Reconnecting Playwright on every probe can
        leave stale protocol sessions behind and can outlive the workflow
        deadline. The deadline here is therefore owned by one bounded relay
        invocation, not by a chain of independent browser launches.
        """
        window = max(1, int(max_seconds))
        deadline = time.monotonic() + window
        sender = self._sender()
        session_page = None
        try:
            from playwright.sync_api import sync_playwright

            sender._launch()
            self._last_debug_port = sender.debug_port
            with sync_playwright() as playwright:
                browser = playwright.chromium.connect_over_cdp(
                    f"http://127.0.0.1:{sender.debug_port}",
                    timeout=getattr(sender, "cdp_connect_timeout_ms", 15000),
                )
                context = browser.contexts[0] if browser.contexts else None
                if context is None:
                    raise ChatReadError("Chrome CDP has no browser context")
                try:
                    page = self._select_page(context)
                except ChatReadError as exc:
                    if "not open" not in str(exc):
                        raise
                    page = context.new_page()
                    # Register before navigation so a failed navigation is
                    # still closed by the single finalizer below.
                    session_page = page
                    remaining = max(1.0, deadline - time.monotonic())
                    page.goto(
                        self.chat_url,
                        wait_until="domcontentloaded",
                        timeout=min(15000, int(remaining * 1000)),
                    )
                    if not read_url_matches(page.url, self.chat_url):
                        raise ChatReadError("configured conversation identity was not preserved after navigation")
                session_page = page
                if not read_url_matches(str(page.url), self.chat_url):
                    raise ChatReadError("configured ChatGPT conversation page does not match the registered URL")

                # A read probe must not wait for a full page-ready/composer
                # timeout. The DOM reader itself performs a short render and
                # stability check; subsequent probes reuse that same page.
                reader = ChatGPTDOMReader(
                    page,
                    stability_wait_seconds=self.stability_wait_seconds,
                    initial_render_wait_seconds=2.0,
                )
                pending_events = {"chat_no_assistant_message", "chat_no_work_order", "chat_not_ready"}
                while True:
                    if time.monotonic() >= deadline:
                        return ReadResult(
                            "chat_read_timeout",
                            f"no completed assistant work order arrived within {window}s",
                        )
                    try:
                        latest = reader.latest_completed_assistant()
                    except ChatReadNotReady as exc:
                        result = ReadResult("chat_not_ready", str(exc))
                    except Exception as exc:
                        return ReadResult("chat_read_error", f"{type(exc).__name__}: {exc}")
                    else:
                        if latest is None:
                            result = ReadResult("chat_no_assistant_message", "conversation has no visible assistant message")
                        else:
                            try:
                                envelope = parse_outbound_envelope(latest.text, project_id=self.project_id)
                            except ChatReadError as exc:
                                if self.work_order_id is not None:
                                    result = ReadResult(
                                        "chat_not_ready",
                                        f"newest completed assistant response is not for expected work_order_id {self.work_order_id}: {exc}",
                                        message_identity=latest.identity,
                                    )
                                else:
                                    return ReadResult("chat_read_error", str(exc), message_identity=latest.identity)
                            else:
                                if envelope is None:
                                    result = ReadResult(
                                        "chat_no_work_order",
                                        "newest completed assistant message has no outbound envelope",
                                        message_identity=latest.identity,
                                    )
                                elif self.work_order_id is not None and envelope.work_order_id != self.work_order_id:
                                    result = ReadResult(
                                        "chat_not_ready",
                                        f"newest completed assistant response has work_order_id {envelope.work_order_id!r}, expected {self.work_order_id!r}",
                                        message_identity=latest.identity,
                                    )
                                else:
                                    try:
                                        result = consume_once(self.root, latest, envelope)
                                    except ChatReadReplayConflict as exc:
                                        return ReadResult("chat_read_error", str(exc), message_identity=latest.identity)
                    if result.event in {"chat_work_order_received", "chat_work_order_duplicate"}:
                        return result
                    if result.event not in pending_events:
                        return result
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return ReadResult(
                            "chat_read_timeout",
                            f"no completed assistant work order arrived within {window}s; last_event={result.event}",
                            message_identity=result.message_identity,
                        )
                    time.sleep(min(max(0.1, float(interval_seconds)), remaining))
        except ChatReadNotReady as exc:
            return ReadResult("chat_not_ready", str(exc))
        except ChatReadError as exc:
            return ReadResult("chat_read_error", str(exc))
        except Exception as exc:
            return ReadResult("chat_read_error", f"{type(exc).__name__}: {exc}")
        finally:
            # Always close the exact session page used by this bounded
            # operation, including timeout and error paths. This is what
            # prevents an abandoned visible Courier window from surviving a
            # failed read. The external Chrome process is never terminated
            # unless this invocation launched it itself.
            if session_page is not None:
                close_page = getattr(sender, "_close_session_page", None)
                if callable(close_page):
                    close_page(session_page)
                else:
                    self._close_page(session_page)
            elif self._last_debug_port is not None:
                close_page = getattr(sender, "_close_session_page", None)
                if callable(close_page):
                    close_page(None)
            if getattr(sender, "owned_process", None) is not None:
                try:
                    sender.owned_process.terminate()
                    sender.owned_process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                sender.owned_process = None

    def _send_correction(self) -> bool:
        if self.work_order_id is None:
            return False
        sender = self._sender()
        sender.post_submit_delay = 0
        sender.close_after_submit = True
        try:
            prompt = build_chat_read_correction_prompt(
                project_id=self.project_id,
                work_order_id=self.work_order_id,
            )
            result = sender.submit(prompt)
            return bool(result.ok and result.verified)
        finally:
            if getattr(sender, "owned_process", None) is not None:
                try:
                    sender.owned_process.terminate()
                    sender.owned_process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                sender.owned_process = None

    def read_once(self) -> ReadResult:
        correction_attempted = False
        for attempt in range(2):
            try:
                result = self._read_once_without_correction()
            except ChatReadError as exc:
                result = ReadResult("chat_read_error", f"{type(exc).__name__}: {exc}")
            needs_correction = result.event in {"chat_no_work_order", "chat_read_error"}
            if not needs_correction or self.work_order_id is None or correction_attempted or attempt:
                if correction_attempted and result.event in {"chat_no_work_order", "chat_read_error"}:
                    return ReadResult("chat_repair_failed", result.detail, result.work_order_path, result.envelope, result.message_identity)
                return result
            correction_attempted = True
            try:
                corrected = self._send_correction()
            except Exception as exc:
                return ReadResult("chat_repair_failed", f"ChatGPT correction request failed: {type(exc).__name__}: {exc}")
            if not corrected:
                return ReadResult("chat_repair_failed", "ChatGPT correction request was not visibly submitted")
        return ReadResult("chat_repair_failed", "ChatGPT correction attempt was exhausted")


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="agent-relay-chatgpt-read-once")
    parser.add_argument("--root", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--chat-url", required=True)
    parser.add_argument("--work-order-id", help="expected work-order ID; enables one bounded correction request")
    args = parser.parse_args(argv)
    try:
        result = ChatGPTReadRelay(root=Path(args.root), project_id=args.project_id, chat_url=args.chat_url, work_order_id=args.work_order_id).read_once()
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
