from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import urllib.request
from urllib.error import URLError

from .config import EXPECTED_CHAT_URL
from .handoff import HandoffSubmission


class BrowserChatGPTSender:
    """One-shot fixed-URL ChatGPT sender using a bounded local Chrome CDP."""

    def __init__(self, config):
        self.url = str(config.chat_url)
        self.profile_dir = Path(os.path.expandvars(os.environ.get("AGENT_RELAY_CHATGPT_PROFILE", "%LOCALAPPDATA%/CodexOrchestrator/profiles/chatgpt"))).expanduser()
        self.debug_port = int(os.environ.get("AGENT_RELAY_CHATGPT_DEBUG_PORT", "9333"))
        self.chrome_path = os.environ.get("AGENT_RELAY_CHROME", "") or self._find_chrome()
        self.timeout = 120
        self.post_submit_delay = 10
        self.owned_process: subprocess.Popen | None = None

    @staticmethod
    def _find_chrome() -> str:
        candidates = [
            os.environ.get("PROGRAMFILES", "") + r"\Google\Chrome\Application\chrome.exe",
            os.environ.get("PROGRAMFILES(X86)", "") + r"\Google\Chrome\Application\chrome.exe",
            os.environ.get("LOCALAPPDATA", "") + r"\Google\Chrome\Application\chrome.exe",
        ]
        for candidate in candidates:
            if candidate and Path(candidate).exists():
                return candidate
        return shutil.which("chrome.exe") or shutil.which("chrome") or ""

    def _cdp(self, path: str = "/json/version") -> dict:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.debug_port}{path}", timeout=3) as response:
            return json.loads(response.read().decode("utf-8"))

    def _launch(self) -> None:
        try:
            self._cdp()
            return
        except (OSError, URLError, json.JSONDecodeError):
            pass
        if not self.chrome_path:
            raise RuntimeError("Chrome executable not found")
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        args = [self.chrome_path, f"--user-data-dir={self.profile_dir}", "--remote-debugging-address=127.0.0.1", f"--remote-debugging-port={self.debug_port}", "--no-first-run", "--no-default-browser-check", "--start-minimized", self.url]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.owned_process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                self._cdp()
                return
            except (OSError, URLError, json.JSONDecodeError):
                time.sleep(0.25)
        raise RuntimeError("Chrome CDP did not become available")

    def submit(self, report: str) -> HandoffSubmission:
        if self.url != EXPECTED_CHAT_URL:
            return HandoffSubmission(False, "fixed ChatGPT URL mismatch")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return HandoffSubmission(False, "Playwright is not installed")
        try:
            self._launch()
            with sync_playwright() as playwright:
                browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{self.debug_port}")
                if not browser.contexts:
                    raise RuntimeError("Chrome CDP has no browser context")
                context = browser.contexts[0]
                page = next((item for item in context.pages if item.url.rstrip("/") == self.url.rstrip("/")), None)
                if page is None:
                    page = context.new_page()
                    page.goto(self.url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
                deadline = time.monotonic() + self.timeout
                composer = None
                while time.monotonic() < deadline:
                    boxes = page.get_by_role("textbox")
                    for index in range(boxes.count() - 1, -1, -1):
                        candidate = boxes.nth(index)
                        if candidate.is_visible() and candidate.is_enabled():
                            composer = candidate
                            break
                    if composer is not None:
                        break
                    page.wait_for_timeout(250)
                if composer is None:
                    raise RuntimeError("fixed ChatGPT composer is not enabled")
                composer.fill(report)
                composer.press("Enter")
                # A generic protocol marker can already exist in the thread and
                # would produce a false positive. Require this submission's
                # unique handoff token to become visible after pressing Enter.
                token = next((line.split(":", 1)[1].strip() for line in report.splitlines() if line.startswith("HANDOFF_TOKEN:")), "")
                marker = f"HANDOFF_TOKEN: {token}" if token else "AGENTRELAY_CHATGPT_HANDOFF/1"
                deadline = time.monotonic() + self.timeout
                while time.monotonic() < deadline:
                    # The submitted user turn is visible in the real conversation;
                    # this is deliberately stronger than a local stdout marker.
                    if marker in page.locator("main").inner_text(timeout=3000):
                        # Leave the submitted turn visible long enough for the
                        # user-facing browser window and remote UI to settle.
                        time.sleep(self.post_submit_delay)
                        return HandoffSubmission(True, "real ChatGPT conversation contains submitted handoff", verified=True)
                    page.wait_for_timeout(500)
                raise RuntimeError("fixed ChatGPT conversation did not visibly receive handoff")
        except Exception as exc:
            return HandoffSubmission(False, f"{type(exc).__name__}: {exc}")
        finally:
            if self.owned_process is not None:
                try:
                    self.owned_process.terminate()
                    self.owned_process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                self.owned_process = None

    def verify_token(self, token: str) -> bool:
        """Check an existing fixed-thread token without sending another turn."""
        if self.url != EXPECTED_CHAT_URL or not token:
            return False
        try:
            from playwright.sync_api import sync_playwright
            self._launch()
            with sync_playwright() as playwright:
                browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{self.debug_port}")
                if not browser.contexts:
                    return False
                context = browser.contexts[0]
                page = next((item for item in context.pages if item.url.rstrip("/") == self.url.rstrip("/")), None)
                if page is None:
                    return False
                return f"HANDOFF_TOKEN: {token}" in page.locator("main").inner_text(timeout=3000)
        except Exception:
            return False
        finally:
            if self.owned_process is not None:
                try:
                    self.owned_process.terminate()
                    self.owned_process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                self.owned_process = None


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="agent-relay-chatgpt-send")
    parser.add_argument("--url", required=True)
    args = parser.parse_args(argv)
    class Config:
        chat_url = args.url
    report = __import__("sys").stdin.read()
    result = BrowserChatGPTSender(Config()).submit(report)
    if result.ok and result.verified:
        print("SUBMITTED")
        return 0
    print(result.detail, file=__import__("sys").stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
