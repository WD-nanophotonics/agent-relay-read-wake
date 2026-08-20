from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from typing import Any

from .model import Request, runtime_root
from .protocol import Reply, parse_reply


class BrowserError(RuntimeError):
    pass


@dataclass(frozen=True)
class AssistantTurn:
    identity: str
    text: str


class ChatDom:
    """All ChatGPT selector assumptions live in this adapter."""
    user_selector = "[data-message-author-role='user'], [data-testid='conversation-turn-user']"
    assistant_selector = "[data-message-author-role='assistant'], [data-testid='conversation-turn-assistant']"
    composer_selectors = ("#prompt-textarea", "textarea[placeholder*='Message']", "div[contenteditable='true'][role='textbox']")
    stop_selector = "button[aria-label*='Stop'], button:has-text('Stop generating')"

    def __init__(self, page: Any): self.page = page

    def composer(self) -> Any:
        for selector in self.composer_selectors:
            locator = self.page.locator(selector).last
            try:
                if locator.count() and locator.is_visible(): return locator
            except Exception:
                continue
        raise BrowserError("ChatGPT composer was not found")

    def wait_for_composer(self, timeout_seconds: float = 30.0) -> Any:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                return self.composer()
            except BrowserError:
                self.page.wait_for_timeout(500)
        try:
            title = self.page.title()
        except Exception:
            title = "<unavailable>"
        raise BrowserError(f"ChatGPT composer was not ready after {int(timeout_seconds)} seconds; url={self.page.url!r}; title={title!r}")

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

    def submit_composer(self, composer: Any) -> None:
        """Prefer the visible send control; attachments do not always submit on Enter."""
        for selector in ("button[data-testid='send-button']", "button[aria-label*='Send']"):
            button = self.page.locator(selector).last
            try:
                if button.count() and button.is_visible() and button.is_enabled():
                    button.click()
                    return
            except Exception:
                continue
        composer.press("Enter")

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

    def assistant_turns(self) -> list[AssistantTurn]:
        result: list[AssistantTurn] = []
        locator = self.page.locator(self.assistant_selector)
        try: count = locator.count()
        except Exception as exc: raise BrowserError(f"assistant DOM is unavailable: {exc}") from exc
        for index in range(count):
            node = locator.nth(index)
            try:
                text = node.inner_text().strip()
                identity = node.get_attribute("data-message-id") or node.get_attribute("data-testid") or f"{index}:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"
            except Exception:
                continue
            if text: result.append(AssistantTurn(identity, text))
        return result

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

    def upload(self, files: tuple[Path, ...]) -> None:
        if not files: return
        # ChatGPT also exposes image-only camera/photo inputs. Selecting the
        # last generic file input can therefore silently reject a document.
        # The regular attachment control has a stable id; the fallback avoids
        # inputs whose accept filter is image-only.
        selector = "#upload-files, input[type='file']:not([accept^='image/'])"
        field = self.page.locator(selector).first
        try:
            if not field.count(): raise BrowserError("ChatGPT file input was not found")
            field.set_input_files([str(path) for path in files])
            deadline = time.monotonic() + 60
            names = [path.name for path in files]
            stable = 0
            while time.monotonic() < deadline:
                body = self.page.locator("body").inner_text()
                if "unable to upload" in body.lower():
                    raise BrowserError("ChatGPT reported that an attachment could not be uploaded")
                uploading = "uploading" in body.lower()
                if all(name in body for name in names) and not uploading:
                    # The file name can appear immediately while the upload is
                    # still in flight. Require three one-second stable samples
                    # before pressing Enter, otherwise Chat can receive only
                    # the text turn without the file.
                    stable += 1
                    if stable >= 3: return
                else:
                    stable = 0
                self.page.wait_for_timeout(1000)
        except BrowserError: raise
        except Exception as exc: raise BrowserError(f"file upload failed: {exc}") from exc
        raise BrowserError("ChatGPT did not visibly confirm all uploaded files")


class ProfileLock:
    def __init__(self, path: Path): self.path = path; self.handle: int | None = None
    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try: self.handle = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                owner = int(self.path.read_text(encoding="ascii").strip())
                os.kill(owner, 0)
            except (OSError, ValueError):
                # A process crash can leave only this small coordination file.
                # It is safe to remove after proving its recorded owner is gone.
                self.path.unlink(missing_ok=True)
                self.handle = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            else:
                raise BrowserError("another ChatCourier run owns the dedicated browser profile")
        os.write(self.handle, str(os.getpid()).encode("ascii")); return self
    def __exit__(self, *_):
        if self.handle is not None: os.close(self.handle)
        try: self.path.unlink()
        except FileNotFoundError: pass


class ChatSession:
    """One owned browser process, page, and Playwright connection per run."""
    def __init__(self, request: Request):
        self.request = request; self.process: subprocess.Popen | None = None
        configured = os.environ.get("CHAT_COURIER_PROFILE") or os.environ.get("AGENT_RELAY_CHATGPT_PROFILE")
        legacy = Path(os.environ.get("LOCALAPPDATA", "")) / "CodexOrchestrator" / "profiles" / "chatgpt"
        self.profile = Path(configured) if configured else (legacy if legacy.exists() else runtime_root() / "profile")
        self.lock = ProfileLock(runtime_root() / "browser.lock")
        self.page = None; self.browser = None; self.playwright = None

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
        args = [self._chrome(), f"--user-data-dir={self.profile}", "--remote-debugging-port=0", "--remote-allow-origins=*", "--no-first-run", "--no-default-browser-check", "--disable-session-crashed-bubble", "--hide-crash-restore-bubble", "--start-minimized", "about:blank"]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.process = subprocess.Popen(args, close_fds=True, creationflags=flags)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.process.poll() is not None: raise BrowserError(f"Chrome exited during launch: {self.process.returncode}")
            try:
                lines = active.read_text(encoding="utf-8").splitlines(); return int(lines[0])
            except (OSError, ValueError, IndexError): time.sleep(0.2)
        raise BrowserError("Chrome CDP did not become available within 30 seconds")

    def __enter__(self) -> "ChatSession":
        self.lock.__enter__(); port = self._launch()
        try:
            from playwright.sync_api import sync_playwright
            self.playwright = sync_playwright().start()
            self.browser = self.playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=15000)
            context = self.browser.contexts[0] if self.browser.contexts else None
            if context is None: raise BrowserError("Chrome CDP has no browser context")
            self.page = context.new_page(); self.page.goto(self.request.chat_url, wait_until="domcontentloaded", timeout=120000)
            if "chatgpt.com" not in self.page.url: raise BrowserError("ChatGPT navigation did not retain the configured conversation")
            dom = ChatDom(self.page)
            dom.wait_for_composer()
            dom.clear_owned_draft()
            return self
        except Exception:
            self.close(); raise

    def submit(self, text: str, files: tuple[Path, ...] = ()) -> set[str]:
        if self.page is None: raise BrowserError("browser session is not open")
        dom = ChatDom(self.page); baseline = {turn.identity for turn in dom.assistant_turns()}; dom.upload(files)
        composer = dom.composer()
        try:
            composer.fill(text, timeout=30000)
            dom.submit_composer(composer)
        except Exception as exc: raise BrowserError(f"ChatGPT input or submit failed: {exc}") from exc
        marker = f"REQUEST_ID={self.request.request_id}"; deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if dom.submission_visible(marker, composer): return baseline
            self.page.wait_for_timeout(400)
        try: composer.fill("", timeout=3000)
        except Exception: pass
        try:
            body_has_marker = marker in self.page.locator("body").inner_text()
            composer_has_marker = marker in (composer.input_value() if composer.evaluate("el => el.tagName") == "TEXTAREA" else composer.inner_text())
            send_count = self.page.locator("button[data-testid='send-button']").count()
        except Exception:
            body_has_marker = composer_has_marker = False; send_count = -1
        raise BrowserError(f"submitted text was not visibly confirmed; body_has_marker={body_has_marker}; composer_has_marker={composer_has_marker}; send_button_count={send_count}")

    def wait_for_reply(self, baseline: set[str], deadline: float, *, required_text: str | None = None) -> AssistantTurn | None:
        if self.page is None: raise BrowserError("browser session is not open")
        dom = ChatDom(self.page); previous: tuple[str, str] | None = None; stable = 0
        while time.monotonic() < deadline:
            turns = [turn for turn in dom.assistant_turns() if turn.identity not in baseline and (required_text is None or required_text in turn.text)]
            if turns and not dom.streaming():
                latest = turns[-1]; sample = (latest.identity, latest.text)
                stable = stable + 1 if sample == previous else 1; previous = sample
                if stable >= 3: return latest
            else: previous = None; stable = 0
            self.page.wait_for_timeout(1000)
        return None

    def close(self) -> None:
        try:
            if self.page is not None: self.page.close()
        except Exception: pass
        try:
            if self.browser is not None: self.browser.close()
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
        self.lock.__exit__(None, None, None)

    def __exit__(self, *_): self.close()
