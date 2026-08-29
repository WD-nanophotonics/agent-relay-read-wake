from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from typing import Any, Callable

from .model import Request, atomic_json, conversation_id_from_url, runtime_root
from .owner import OwnerBusy, OwnerLease, OwnerRecord, process_alive, read_owner, terminate_orphan_browser
from .storage import save_response_cursor


class BrowserError(RuntimeError):
    pass


class ProfileConfigurationError(BrowserError):
    """The caller selected an unsafe or invalid Chrome profile path."""


class ChatAuthenticationRequired(BrowserError):
    """The selected Chrome profile is not authenticated to ChatGPT."""


class ChatAccessDenied(BrowserError):
    """ChatGPT rendered an access-denied page for the registered conversation."""


class ChatConversationMismatch(BrowserError):
    """Navigation was redirected away from the registered conversation."""


class ChatComposerNotReady(BrowserError):
    """The visible composer cannot safely accept a Courier draft yet."""

    def __init__(self, detail: str, snapshot: dict[str, Any] | None = None):
        super().__init__(detail)
        self.snapshot = snapshot or {}


class SubmissionUnconfirmed(BrowserError):
    """Courier attempted Send but cannot prove whether ChatGPT accepted it."""

    def __init__(self, detail: str, diagnostic_path: Path | None = None):
        super().__init__(detail)
        self.diagnostic_path = diagnostic_path


class PreSubmissionError(BrowserError):
    """Courier could not place the local draft; no Send action was attempted."""

    def __init__(self, detail: str, *, failure_stage: str = "pre_submission_failed",
                 timeline: list[dict[str, Any]] | None = None, diagnostic_path: Path | None = None):
        super().__init__(detail)
        self.failure_stage = failure_stage
        self.timeline = timeline or []
        self.diagnostic_path = diagnostic_path


def validate_profile_path(profile: Path, profile_directory: str) -> Path:
    """Validate and normalize a Courier-owned Chrome profile selection."""
    try:
        normalized = profile.resolve()
    except OSError as exc:
        raise ProfileConfigurationError(f"cannot resolve Courier profile: {profile}") from exc
    if "user data" in {part.lower() for part in normalized.parts}:
        raise ProfileConfigurationError(
            "CHAT_COURIER_PROFILE points into a normal Chrome User Data tree; "
            "use a dedicated Courier profile instead"
        )
    if not profile_directory or any(char in profile_directory for char in "\\/"):
        raise ProfileConfigurationError("CHAT_COURIER_PROFILE_DIRECTORY must be one Chrome profile name")
    return normalized



@dataclass(frozen=True)
class AssistantTurn:
    identity: str
    text: str
    index: int


class ChatDom:
    """All ChatGPT selector assumptions live in this adapter."""
    user_selector = "[data-message-author-role='user'], [data-testid='conversation-turn-user']"
    assistant_selector = "[data-message-author-role='assistant'], [data-testid='conversation-turn-assistant']"
    composer_selectors = ("#prompt-textarea", "textarea[placeholder*='Message']", "div[contenteditable='true'][role='textbox']")
    # data-testid is language-neutral.  The localized fallbacks matter for
    # Courier profiles whose ChatGPT UI is in Chinese or another language.
    stop_selector = (
        "button[data-testid='stop-button'], button[data-testid='stop-generating-button'], "
        "button[aria-label*='Stop'], button[aria-label*='停止'], "
        "button:has-text('Stop generating'), button:has-text('停止生成')"
    )
    send_selectors = ("button[data-testid='send-button']", "button[aria-label*='Send']", "button[aria-label*='发送']")
    auth_selectors = (
        "a[href*='/auth/login']",
        "button:has-text('Continue with Google')",
        "button:has-text('Continue as')",
        "button:has-text('Log in')",
    )
    access_denied_text = (
        "you don't have access",
        "you do not have access",
        "conversation not found",
        "unable to load conversation",
    )

    def __init__(self, page: Any): self.page = page

    def composer(self) -> Any:
        for selector in self.composer_selectors:
            locator = self.page.locator(selector).last
            try:
                if locator.count() and locator.is_visible(): return locator
            except Exception:
                continue
        raise BrowserError("ChatGPT composer was not found")

    def composer_health(self, *, focus: bool = False) -> dict[str, Any]:
        """Capture actionability signals without reading the conversation payload."""
        sample: dict[str, Any] = {"page_url": getattr(self.page, "url", "<unavailable>")}
        try:
            sample["page_title"] = self.page.title()
        except Exception as exc:
            sample["page_title_error"] = f"{type(exc).__name__}: {exc}"
        try:
            composer = self.composer()
            sample.update({
                "visible": bool(composer.is_visible()),
                "enabled": bool(composer.is_enabled()),
                "editable": bool(composer.is_editable()),
                "streaming": self.streaming(),
            })
            if focus and sample["visible"] and sample["enabled"] and sample["editable"] and not sample["streaming"]:
                composer.focus(timeout=3000)
                sample["focused"] = bool(composer.evaluate("el => document.activeElement === el || el.contains(document.activeElement)"))
            else:
                sample["focused"] = None
            sample["ready"] = bool(sample["visible"] and sample["enabled"] and sample["editable"] and not sample["streaming"] and (not focus or sample["focused"]))
        except Exception as exc:
            sample.update({"ready": False, "error": f"{type(exc).__name__}: {exc}"})
        return sample

    def authentication_required(self) -> bool:
        """Detect the ChatGPT login wall without inspecting credentials."""
        try:
            url = (self.page.url or "").lower()
            if any(part in url for part in ("/auth/login", "/auth/", "/login")):
                return True
            for selector in self.auth_selectors:
                locator = self.page.locator(selector)
                if locator.count() and locator.first.is_visible():
                    return True
            body = self.page.locator("body").inner_text(timeout=2000).lower()
            return "log in to chatgpt" in body or "continue with google" in body or "continue as " in body
        except Exception:
            return False

    def access_denied(self) -> bool:
        try:
            body = self.page.locator("body").inner_text(timeout=2000).lower()
            return any(marker in body for marker in self.access_denied_text)
        except Exception:
            return False

    def wait_for_composer(self, timeout_seconds: float = 30.0) -> Any:
        deadline = time.monotonic() + timeout_seconds
        last_sample: dict[str, Any] = {}
        while time.monotonic() < deadline:
            if self.access_denied():
                raise ChatAccessDenied("ChatGPT reports that this account does not have access to the registered conversation")
            if self.authentication_required():
                raise ChatAuthenticationRequired(
                    "selected Chrome profile is not logged in to ChatGPT; "
                    "sign in once in this profile, then retry"
                )
            last_sample = self.composer_health(focus=True)
            if last_sample.get("ready"):
                return self.composer()
            try:
                self.page.wait_for_timeout(500)
            except Exception as exc:
                last_sample = {**last_sample, "wait_error": f"{type(exc).__name__}: {exc}"}
                break
        if self.authentication_required():
            raise ChatAuthenticationRequired(
                "selected Chrome profile is not logged in to ChatGPT; "
                "sign in once in this profile, then retry"
            )
        if self.access_denied():
            raise ChatAccessDenied("ChatGPT reports that this account does not have access to the registered conversation")
        raise ChatComposerNotReady(
            f"ChatGPT composer was not ready after {int(timeout_seconds)} seconds",
            last_sample,
        )

    def user_contains(self, marker: str) -> bool:
        try: return any(marker in text for text in self.page.locator(self.user_selector).all_inner_texts())
        except Exception: return False

    def submission_visible(self, marker: str, composer: Any) -> bool:
        """Confirm a sent turn without mistaking draft text for a sent turn."""
        if self.user_contains(marker):
            return True
        try:
            draft = composer.input_value() if composer.evaluate("el => el.tagName") == "TEXTAREA" else composer.inner_text()
            return marker in self.page.locator("body").inner_text() and marker not in draft
        except Exception:
            return False

    @staticmethod
    def composer_text(composer: Any) -> str:
        try:
            return composer.input_value() if composer.evaluate("el => el.tagName") == "TEXTAREA" else composer.inner_text()
        except Exception:
            return "<unavailable>"

    def fill_composer(self, composer: Any, text: str, *, timeout: int = 30000) -> str:
        """Fill ChatGPT's composer, with a keyboard path for ProseMirror inputs."""
        try:
            composer.fill(text, timeout=timeout)
            return "fill"
        except Exception as fill_error:
            # ChatGPT's current composer is a ProseMirror contenteditable. In
            # some UI states Playwright reports it as editable but its locator
            # fill actionability wait never completes. Reacquire the element,
            # focus it, and use the page-level keyboard path instead.
            try:
                fresh_composer = self.composer()
                fresh_composer.focus(timeout=5000)
                self.page.keyboard.press("ControlOrMeta+A")
                self.page.keyboard.press("Backspace")
                self.page.keyboard.insert_text(text)
                return "keyboard_insert_text"
            except Exception as keyboard_error:
                raise RuntimeError(
                    f"composer fill failed; keyboard fallback failed: {keyboard_error}"
                ) from fill_error

    def send_controls(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for selector in self.send_selectors:
            control = {"selector": selector, "count": 0, "visible": False, "enabled": False}
            try:
                button = self.page.locator(selector).last
                control["count"] = button.count()
                if control["count"]:
                    control["visible"] = button.is_visible()
                    control["enabled"] = button.is_enabled() if control["visible"] else False
            except Exception as exc:
                control["error"] = f"{type(exc).__name__}: {exc}"
            result.append(control)
        return result

    def attachment_evidence(self, files: tuple[Path, ...]) -> dict[str, Any]:
        names = [path.name for path in files]
        try:
            body = self.page.locator("body").inner_text()
            lowered = body.lower()
            return {
                "names": names,
                "names_visible": {name: name in body for name in names},
                "uploading_visible": "uploading" in lowered,
                "upload_error_visible": "unable to upload" in lowered,
            }
        except Exception as exc:
            return {"names": names, "error": f"{type(exc).__name__}: {exc}"}

    def submit_composer(self, composer: Any, *, require_button: bool = False,
                        timeout_seconds: float = 30.0) -> dict[str, Any]:
        """Prefer the visible send control; attachments do not always submit on Enter."""
        # Keep the failures: a visible enabled send control that rejects a
        # Playwright click must not be silently turned into an Enter fallback.
        # Attachment drafts often ignore Enter entirely.
        attempts: list[dict[str, Any]] = []
        enabled_control_seen = False
        deadline = time.monotonic() + timeout_seconds
        while True:
            for selector in self.send_selectors:
                button = self.page.locator(selector).last
                try:
                    if button.count() and button.is_visible() and button.is_enabled():
                        enabled_control_seen = True
                        try:
                            button.click(timeout=5000)
                            return {"method": "button", "selector": selector, "attempts": attempts}
                        except Exception as exc:
                            attempts.append({"selector": selector, "method": "button", "error": f"{type(exc).__name__}: {exc}"})
                        try:
                            button.click(force=True, timeout=5000)
                            return {"method": "button_force", "selector": selector, "attempts": attempts}
                        except Exception as exc:
                            attempts.append({"selector": selector, "method": "button_force", "error": f"{type(exc).__name__}: {exc}"})
                        try:
                            button.evaluate("element => element.click()")
                            return {"method": "button_dom", "selector": selector, "attempts": attempts}
                        except Exception as exc:
                            attempts.append({"selector": selector, "method": "button_dom", "error": f"{type(exc).__name__}: {exc}"})
                except Exception as exc:
                    attempts.append({"selector": selector, "method": "inspect", "error": f"{type(exc).__name__}: {exc}"})
            if enabled_control_seen or time.monotonic() >= deadline:
                break
            self.page.wait_for_timeout(250)
        if enabled_control_seen or require_button:
            if require_button and not enabled_control_seen:
                attempts.append({"method": "wait_for_enabled_send_button", "error": f"timeout after {timeout_seconds:g} seconds"})
            return {"method": "unavailable", "attempts": attempts}
        try:
            composer.press("Enter")
            return {"method": "enter", "attempts": attempts}
        except Exception as exc:
            attempts.append({"method": "enter", "error": f"{type(exc).__name__}: {exc}"})
            return {"method": "unavailable", "attempts": attempts}

    def clear_owned_draft(self) -> None:
        """Remove stale text/files left by an interrupted Courier run."""
        try:
            composer = self.composer()
            composer.fill("")
        except Exception:
            return
        try:
            removers = self.page.locator("button[aria-label^='Remove file']")
            while removers.count():
                removers.first.click()
                self.page.wait_for_timeout(150)
        except Exception:
            # The profile is Courier-owned; a later upload/submit still checks
            # its exact files and will fail closed if the UI is inconsistent.
            pass

    def assistant_turns(self, *, include_empty: bool = False) -> list[AssistantTurn]:
        result: list[AssistantTurn] = []
        locator = self.page.locator(self.assistant_selector)
        try: count = locator.count()
        except Exception as exc: raise BrowserError(f"assistant DOM is unavailable: {exc}") from exc
        for index in range(count):
            node = locator.nth(index)
            try:
                text = node.inner_text().strip()
                identity = node.get_attribute("data-message-id") or f"{index}:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"
                has_asset = bool(node.locator("a[download], img[src]").count())
            except Exception:
                continue
            # The regular Courier protocol needs a textual reply, while batch
            # image generation can create an assistant turn before any text or
            # inspectable image element is present.  Let that caller opt in.
            if text or has_asset or include_empty:
                result.append(AssistantTurn(identity, text, index))
        return result

    def assistant_turns_after_user(self, marker: str) -> tuple[bool, list[AssistantTurn]]:
        """Return assistant turns after the exact outbound user turn.

        This legacy recovery anchor inspects the submitted user turn, never the
        assistant reply body.  New requests use the durable pre-send cursor.
        """
        result: list[AssistantTurn] = []; user_seen = False
        locator = self.page.locator(f"{self.user_selector}, {self.assistant_selector}")
        try: count = locator.count()
        except Exception as exc: raise BrowserError(f"conversation DOM is unavailable: {exc}") from exc
        for index in range(count):
            node = locator.nth(index)
            try:
                text = node.inner_text().strip()
                role = (node.get_attribute("data-message-author-role") or "").lower()
                testid = (node.get_attribute("data-testid") or "").lower()
            except Exception:
                continue
            is_user = role == "user" or "conversation-turn-user" in testid
            is_assistant = role == "assistant" or "conversation-turn-assistant" in testid
            if is_user and marker in text:
                user_seen = True; result = []
                continue
            if user_seen and is_assistant and text:
                try: identity = node.get_attribute("data-message-id") or f"{index}:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"
                except Exception: continue
                result.append(AssistantTurn(identity, text, index))
        return user_seen, result

    def streaming(self) -> bool:
        try:
            if self.page.locator(self.stop_selector).count() and self.page.locator(self.stop_selector).first.is_visible(): return True
        except Exception:
            pass
        try:
            nodes = self.page.locator(self.assistant_selector)
            if not nodes.count(): return False
            latest = nodes.nth(nodes.count() - 1)
            return any((latest.get_attribute(attr) or "").lower() in {"true", "1"} for attr in ("data-is-streaming", "data-message-is-streaming", "aria-busy"))
        except Exception:
            return False

    def ready_for_next_turn(self) -> bool:
        """Return true once ChatGPT restored the composer after generation."""
        if self.streaming():
            return False
        try:
            self.composer()
            for selector in self.send_selectors:
                button = self.page.locator(selector).last
                if button.count() and button.is_visible():
                    return True
        except Exception:
            return False
        return False

    @staticmethod
    def attachment_manifest(files: tuple[Path, ...]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for path in files:
            try:
                result.append({"name": path.name, "bytes": path.stat().st_size})
            except OSError as exc:
                result.append({"name": path.name, "stat_error": f"{type(exc).__name__}: {exc}"})
        return result

    @staticmethod
    def _page_closed(exc: Exception) -> bool:
        detail = f"{type(exc).__name__}: {exc}".lower()
        return "targetclosed" in detail or "target page, context or browser has been closed" in detail

    def upload_health(self, files: tuple[Path, ...]) -> tuple[dict[str, Any], str | None]:
        """Collect bounded UI evidence without treating body text as payload."""
        sample: dict[str, Any] = {"attachments": self.attachment_manifest(files)}
        try:
            sample["page_url"] = self.page.url
            sample["page_title"] = self.page.title()
            body = self.page.locator("body").inner_text(timeout=3000)
            lowered = body.lower()
            sample["dom_readable"] = True
            sample["body_sha256"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
            sample["attachment_state"] = {
                "names_visible": {path.name: path.name in body for path in files},
                "uploading_visible": "uploading" in lowered,
                "upload_error_visible": "unable to upload" in lowered,
            }
            try:
                composer = self.composer()
                sample["composer"] = {"visible": True, "editable": bool(composer.is_editable())}
            except Exception as exc:
                sample["composer"] = {"visible": False, "error": f"{type(exc).__name__}: {exc}"}
            return sample, None
        except Exception as exc:
            sample.update({"dom_readable": False, "error": f"{type(exc).__name__}: {exc}"})
            return sample, "page_closed_during_upload" if self._page_closed(exc) else "browser_page_unresponsive"

    def upload(self, files: tuple[Path, ...], *, on_event: Callable[..., None] | None = None,
               timeout_seconds: float = 120.0, stall_seconds: float = 30.0,
               health_failure_limit: int = 3) -> list[dict[str, Any]]:
        if not files: return []
        manifest = self.attachment_manifest(files)
        def notify(name: str, **values: Any) -> None:
            if on_event is not None:
                on_event(name, attachments=manifest, **values)
        # ChatGPT also exposes image-only camera/photo inputs. Selecting the
        # last generic file input can therefore silently reject a document.
        # The regular attachment control has a stable id; the fallback avoids
        # inputs whose accept filter is image-only.
        selector = "#upload-files, input[type='file']:not([accept^='image/'])"
        field = self.page.locator(selector).first
        timeline: list[dict[str, Any]] = []
        started = time.monotonic()
        notify("attachment_upload_started", upload_timeout_seconds=timeout_seconds, stall_seconds=stall_seconds)
        try:
            if not field.count(): raise BrowserError("ChatGPT file input was not found")
            field.set_input_files([str(path) for path in files])
            deadline = started + timeout_seconds
            stable = 0
            health_failures = 0
            last_signature: tuple[Any, ...] | None = None
            last_progress = started
            while time.monotonic() < deadline:
                sample, unhealthy = self.upload_health(files)
                elapsed = round(time.monotonic() - started, 3)
                sample.update({"elapsed_seconds": elapsed, "observed_at_unix": round(time.time(), 3)})
                timeline.append(sample)
                if unhealthy == "page_closed_during_upload":
                    notify("page_closed_during_upload", elapsed_seconds=elapsed, last_sample=sample)
                    raise PreSubmissionError("ChatGPT page closed during attachment upload", failure_stage=unhealthy, timeline=timeline)
                if unhealthy == "browser_page_unresponsive":
                    health_failures += 1
                    if health_failures >= health_failure_limit:
                        notify("browser_page_unresponsive", elapsed_seconds=elapsed, health_failures=health_failures, last_sample=sample)
                        raise PreSubmissionError("ChatGPT page was unresponsive during attachment upload", failure_stage=unhealthy, timeline=timeline)
                    self.page.wait_for_timeout(1000)
                    continue
                health_failures = 0
                state = sample["attachment_state"]
                signature = (tuple(sorted(state["names_visible"].items())), state["uploading_visible"], state["upload_error_visible"])
                if signature != last_signature:
                    last_signature = signature
                    last_progress = time.monotonic()
                    notify("attachment_upload_progress", elapsed_seconds=elapsed, attachment_state=state)
                if state["upload_error_visible"]:
                    notify("attachment_upload_failed", elapsed_seconds=elapsed, failure_stage="attachment_upload_failed", last_sample=sample)
                    raise PreSubmissionError("ChatGPT reported that an attachment could not be uploaded", failure_stage="attachment_upload_failed", timeline=timeline)
                if all(state["names_visible"].values()) and not state["uploading_visible"]:
                    # The file name can appear immediately while the upload is
                    # still in flight. Require three one-second stable samples
                    # before attempting Send, otherwise Chat can receive only
                    # the text turn without the file.
                    stable += 1
                    if stable >= 3:
                        notify("attachment_upload_progress", elapsed_seconds=elapsed, attachment_state=state, confirmed=True)
                        return timeline
                else:
                    stable = 0
                if time.monotonic() - last_progress >= stall_seconds:
                    notify("attachment_upload_stalled", elapsed_seconds=elapsed, stall_seconds=stall_seconds, last_sample=sample)
                    raise PreSubmissionError("attachment upload showed no observable UI progress", failure_stage="attachment_upload_stalled", timeline=timeline)
                self.page.wait_for_timeout(1000)
        except PreSubmissionError:
            raise
        except Exception as exc:
            stage = "page_closed_during_upload" if self._page_closed(exc) else "attachment_upload_failed"
            notify(stage, elapsed_seconds=round(time.monotonic() - started, 3), error=f"{type(exc).__name__}: {exc}")
            raise PreSubmissionError(f"file upload failed: {exc}", failure_stage=stage, timeline=timeline) from exc
        notify("attachment_upload_failed", elapsed_seconds=round(time.monotonic() - started, 3), failure_stage="attachment_upload_timeout")
        raise PreSubmissionError("ChatGPT did not visibly confirm all uploaded files", failure_stage="attachment_upload_timeout", timeline=timeline)


class ChatSession:
    """One owned browser process, page, and Playwright connection per run."""
    def __init__(self, request: Request, *, recovery: bool = False, prepare_only: bool = False,
                 status_callback: Callable[..., None] | None = None):
        self.request = request; self.process: subprocess.Popen | None = None
        self.recovery = recovery
        self.prepare_only = prepare_only
        self.status_callback = status_callback
        self.attached_existing = False
        configured = os.environ.get("CHAT_COURIER_PROFILE") or os.environ.get("AGENT_RELAY_CHATGPT_PROFILE")
        legacy = Path(os.environ.get("LOCALAPPDATA", "")) / "CodexOrchestrator" / "profiles" / "chatgpt"
        self.profile = Path(configured) if configured else (legacy if legacy.exists() else runtime_root() / "profile")
        self.profile_directory = os.environ.get("CHAT_COURIER_PROFILE_DIRECTORY", "Default")
        self.profile = validate_profile_path(self.profile, self.profile_directory)
        self.owner = OwnerLease(request.project_id, request.request_id, profile=str(self.profile))
        self.page = None; self.browser = None; self.playwright = None

    def _status(self, name: str, **values: Any) -> None:
        if self.status_callback is not None:
            self.status_callback(name, **values)

    @staticmethod
    def _chrome() -> str:
        candidates = [os.environ.get("CHAT_COURIER_CHROME"), os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"), os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe")]
        for candidate in candidates:
            if candidate and Path(candidate).is_file(): return candidate
        raise BrowserError("Google Chrome was not found; set CHAT_COURIER_CHROME")

    def _launch(self) -> int:
        self.profile.mkdir(parents=True, exist_ok=True)
        active = self.profile / "DevToolsActivePort"
        try: active.unlink()
        except FileNotFoundError: pass
        args = [self._chrome(), f"--user-data-dir={self.profile}", f"--profile-directory={self.profile_directory}", "--remote-debugging-port=0", "--remote-allow-origins=*", "--no-first-run", "--no-default-browser-check", "--disable-session-crashed-bubble", "--hide-crash-restore-bubble", "--start-minimized", "about:blank"]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.process = subprocess.Popen(args, close_fds=True, creationflags=flags)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.process.poll() is not None: raise BrowserError(f"Chrome exited during launch: {self.process.returncode}")
            try:
                lines = active.read_text(encoding="utf-8").splitlines(); return int(lines[0])
            except (OSError, ValueError, IndexError): time.sleep(0.2)
        raise BrowserError("Chrome CDP did not become available within 30 seconds")

    def _connect(self, port: int, *, existing: bool) -> None:
        from playwright.sync_api import sync_playwright
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=15000)
        context = self.browser.contexts[0] if self.browser.contexts else None
        if context is None: raise BrowserError("Chrome CDP has no browser context")
        expected_conversation = conversation_id_from_url(self.request.chat_url)
        if expected_conversation is None: raise BrowserError("registered ChatGPT conversation URL is invalid")
        if existing:
            pages = [item for item in context.pages if conversation_id_from_url(item.url) == expected_conversation]
            if not pages: raise OwnerBusy("live Courier owns the browser but its ChatGPT page is not available yet")
            self.page = pages[0]
            self.attached_existing = True
        else:
            self.page = context.new_page()
            self.page.goto(self.request.chat_url, wait_until="domcontentloaded", timeout=120000)
            actual_conversation = conversation_id_from_url(self.page.url)
            if actual_conversation != expected_conversation:
                raise ChatConversationMismatch(
                    "ChatGPT navigation did not retain the registered conversation; "
                    f"expected={expected_conversation!r}; actual_url={self.page.url!r}"
                )

    def __enter__(self) -> "ChatSession":
        existing = read_owner() if self.recovery else None
        if existing is not None and process_alive(existing.owner_pid):
            if existing.cdp_port is None:
                raise OwnerBusy("live Courier has not published a usable CDP port yet")
            try:
                self._connect(existing.cdp_port, existing=True)
                ChatDom(self.page).wait_for_composer()
                return self
            except Exception:
                self.close()
                raise
        if self.recovery and existing is not None and not process_alive(existing.owner_pid):
            if not terminate_orphan_browser(existing):
                raise OwnerBusy("stale Courier owner has an unverified browser process; refusing to terminate it")
        self.owner.acquire("recovery_start" if self.recovery else "starting")
        port = self._launch()
        try:
            self._connect(port, existing=False)
            self.owner.update("browser_ready", cdp_port=port, browser_pid=self.process.pid if self.process else None)
            dom = ChatDom(self.page)
            dom.wait_for_composer()
            if not self.prepare_only and not self.recovery:
                dom.clear_owned_draft()
            return self
        except Exception:
            self.close(); raise

    def submit(self, text: str, files: tuple[Path, ...] = (), *, marker: str | None = None,
               include_empty_baseline: bool = False) -> set[str]:
        if self.page is None: raise BrowserError("browser session is not open")
        self.owner.update("submitting")
        dom = ChatDom(self.page)
        readiness_timeline: list[dict[str, Any]] = []
        try:
            composer = dom.wait_for_composer(timeout_seconds=60.0)
            readiness_timeline.append({"phase": "before_upload", **dom.composer_health()})
            baseline = {turn.identity for turn in dom.assistant_turns(include_empty=include_empty_baseline)}
            # Persist the receive cursor before Send.  Recovery can now locate
            # the next assistant turn without inspecting its reply contents.
            save_response_cursor(self.request, baseline)
            dom.upload(files, on_event=self._status)
            composer = dom.wait_for_composer(timeout_seconds=60.0)
            readiness_timeline.append({"phase": "before_fill", **dom.composer_health()})
            dom.fill_composer(composer, text, timeout=30000)
        except ChatComposerNotReady as exc:
            error = PreSubmissionError(
                f"ChatGPT composer was not ready before Send: {exc}",
                failure_stage="composer_not_ready", timeline=[*readiness_timeline, exc.snapshot],
            )
            self._record_pre_submission_failure(dom, files, error)
            raise error from exc
        except PreSubmissionError as exc:
            if readiness_timeline:
                exc.timeline = [*readiness_timeline, *exc.timeline]
            self._record_pre_submission_failure(dom, files, exc)
            raise
        except Exception as exc:
            # This is before any click or Enter action.  It is safe to retry
            # the same request because ChatGPT has not received a user turn.
            stage = "pre_submission_page_closed" if ChatDom._page_closed(exc) else "composer_uneditable"
            error = PreSubmissionError(
                f"ChatGPT composer fill failed before Send: {exc}", failure_stage=stage,
                timeline=[*readiness_timeline, {"phase": "fill_failed", **dom.composer_health()}],
            )
            self._record_pre_submission_failure(dom, files, error)
            raise error from exc
        try:
            click = dom.submit_composer(composer, require_button=bool(files))
        except Exception as exc:
            # A UI action may have reached ChatGPT before Playwright failed.
            # Preserve fail-closed semantics for that uncertain boundary.
            self._raise_submission_unconfirmed(
                dom, composer, marker or f"REQUEST_ID={self.request.request_id}", files,
                {"method": "submit_exception", "error": f"{type(exc).__name__}: {exc}"},
                "send_action_interrupted",
            )
        marker = marker or f"REQUEST_ID={self.request.request_id}"; deadline = time.monotonic() + 30
        if click.get("method") == "unavailable":
            self._raise_submission_unconfirmed(dom, composer, marker, files, click, "send_button_click_failed")
        # Some ChatGPT UI revisions accept a button click visually but leave
        # the draft in the composer. Give the click a short opportunity to
        # create a user turn, then use Enter once only while the draft is
        # demonstrably still present. This cannot duplicate a submitted turn:
        # a real send clears the composer before this fallback is attempted.
        click_grace = time.monotonic() + 2
        while time.monotonic() < deadline:
            if dom.submission_visible(marker, composer):
                self.owner.update("request_submitted")
                return baseline
            # A real Send click is the only submission path for attachments.
            # Pressing Enter while an upload is still settling can leave the
            # draft unchanged or create an ambiguous second attempt.
            if not files and time.monotonic() >= click_grace:
                try:
                    draft = dom.composer_text(composer)
                    if marker in draft:
                        composer.press("Enter")
                except Exception:
                    pass
                click_grace = deadline + 1
            self.page.wait_for_timeout(400)
        diagnostic = self._write_submission_diagnostic(dom, composer, marker, files, click)
        draft_has_marker = marker in str(diagnostic.get("composer_text", ""))
        attachment = diagnostic.get("attachments", {})
        if attachment.get("uploading_visible"):
            kind = "attachment_upload_still_pending"
        elif draft_has_marker:
            kind = "send_not_visibly_accepted"
        else:
            kind = "message_may_have_been_submitted_but_no_user_turn_was_visible"
        raise SubmissionUnconfirmed(
            f"{kind}; diagnostic_path={self.request.directory / 'submission_diagnostic.json'}",
            self.request.directory / "submission_diagnostic.json",
        )

    def _raise_submission_unconfirmed(self, dom: ChatDom, composer: Any, marker: str,
                                      files: tuple[Path, ...], click: dict[str, Any], kind: str) -> None:
        self._write_submission_diagnostic(dom, composer, marker, files, click)
        raise SubmissionUnconfirmed(
            f"{kind}; diagnostic_path={self.request.directory / 'submission_diagnostic.json'}",
            self.request.directory / "submission_diagnostic.json",
        )

    def _record_pre_submission_failure(self, dom: ChatDom, files: tuple[Path, ...],
                                       error: PreSubmissionError) -> None:
        """Write useful local evidence while the Courier-owned page still exists."""
        self._write_transport_diagnostic(dom, files, error)
        error.diagnostic_path = self.request.directory / "transport_diagnostic.json"

    def _write_transport_diagnostic(self, dom: ChatDom, files: tuple[Path, ...],
                                    error: PreSubmissionError) -> dict[str, Any]:
        """Persist local transport evidence without retaining page/body payload text."""
        last_sample: dict[str, Any] = {}
        try:
            last_sample, _ = dom.upload_health(files)
        except Exception as exc:
            last_sample = {"diagnostic_error": f"{type(exc).__name__}: {exc}"}
        diagnostic: dict[str, Any] = {
            "version": 1,
            "project_id": self.request.project_id,
            "request_id": self.request.request_id,
            "failure_stage": error.failure_stage,
            "detail": str(error),
            "next_action": "agent_decision_required",
            "safe_to_retry_same_request": True,
            "attachments": dom.attachment_manifest(files),
            "timeline": error.timeline,
            "last_sample": last_sample,
        }
        path = self.request.directory / "transport_diagnostic.json"
        atomic_json(path, diagnostic)
        if self.page is not None:
            try:
                screenshot = self.request.directory / "transport_diagnostic.png"
                self.page.screenshot(path=str(screenshot), full_page=False, timeout=10000)
                diagnostic["screenshot_path"] = str(screenshot)
            except Exception as exc:
                diagnostic["screenshot_error"] = f"{type(exc).__name__}: {exc}"
            atomic_json(path, diagnostic)
        return diagnostic

    def _write_submission_diagnostic(self, dom: ChatDom, composer: Any, marker: str,
                                     files: tuple[Path, ...], click: dict[str, Any]) -> dict[str, Any]:
        """Persist bounded local evidence before a failed submission closes the page."""
        body_text = ""
        user_turns: list[str] = []
        title = "<unavailable>"
        try:
            body_text = self.page.locator("body").inner_text() if self.page is not None else ""
            user_turns = self.page.locator(ChatDom.user_selector).all_inner_texts() if self.page is not None else []
            title = self.page.title() if self.page is not None else "<unavailable>"
        except Exception:
            pass
        diagnostic: dict[str, Any] = {
            "version": 1,
            "project_id": self.request.project_id,
            "request_id": self.request.request_id,
            "marker": marker,
            "page_url": self.page.url if self.page is not None else "<unavailable>",
            "page_title": title,
            "click": click,
            "composer_text": dom.composer_text(composer),
            "composer_contains_marker": marker in dom.composer_text(composer),
            "body_contains_marker": marker in body_text,
            "user_turn_count": len(user_turns),
            "user_turns_with_marker": [text for text in user_turns if marker in text],
            "send_controls": dom.send_controls(),
            "attachments": dom.attachment_evidence(files),
        }
        path = self.request.directory / "submission_diagnostic.json"
        atomic_json(path, diagnostic)
        if self.page is not None:
            try:
                self.page.screenshot(path=str(self.request.directory / "submission_diagnostic.png"), full_page=False, timeout=10000)
                diagnostic["screenshot_path"] = str(self.request.directory / "submission_diagnostic.png")
                atomic_json(path, diagnostic)
            except Exception as exc:
                diagnostic["screenshot_error"] = f"{type(exc).__name__}: {exc}"
                atomic_json(path, diagnostic)
        return diagnostic

    def wait_for_reply(self, baseline: set[str] | None, deadline: float, *, after_user_marker: str | None = None) -> AssistantTurn | None:
        if self.page is None: raise BrowserError("browser session is not open")
        dom = ChatDom(self.page); previous: tuple[str, str] | None = None; stable = 0
        last_snapshot: dict[str, Any] = {}
        while time.monotonic() < deadline:
            self.owner.update("waiting_for_response")
            all_turns = dom.assistant_turns()
            if baseline is not None:
                turns = [turn for turn in all_turns if turn.identity not in baseline]
                anchor_found = True
            elif after_user_marker is not None:
                anchor_found, turns = dom.assistant_turns_after_user(after_user_marker)
            else:
                raise BrowserError("reply wait requires a durable cursor or an outbound user-turn anchor")
            is_streaming = dom.streaming()
            last_snapshot = {
                "assistant_turn_count": len(all_turns), "candidate_count": len(turns),
                "anchor_found": anchor_found, "streaming": is_streaming,
                "composer_ready": dom.ready_for_next_turn(),
            }
            if turns and not is_streaming:
                latest = turns[-1]; sample = (latest.identity, latest.text)
                stable = stable + 1 if sample == previous else 1; previous = sample
                if stable >= 3: return latest
            else: previous = None; stable = 0
            self.page.wait_for_timeout(1000)
        atomic_json(self.request.directory / "response-diagnostic.json", {
            "version": 1, "project_id": self.request.project_id, "request_id": self.request.request_id,
            "failure_stage": "reply_not_detected", "captured_at": time.time(), **last_snapshot,
        })
        return None

    def close(self) -> None:
        try:
            if self.page is not None and not self.attached_existing: self.page.close()
        except Exception: pass
        try:
            if self.browser is not None and not self.attached_existing: self.browser.close()
        except Exception: pass
        try:
            if self.playwright is not None: self.playwright.stop()
        except Exception: pass
        if self.process is not None:
            try: self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try: self.process.wait(timeout=5)
                except subprocess.TimeoutExpired: self.process.kill(); self.process.wait(timeout=5)
        self.page = self.browser = self.playwright = self.process = None
        if not self.attached_existing:
            self.owner.release()
        else:
            try:
                if self.playwright is not None: self.playwright.stop()
            except Exception: pass

    def __exit__(self, *_): self.close()
