from __future__ import annotations

import json
import logging
import os
import ctypes
from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess
import time
import urllib.request
from urllib.error import URLError

from .config import DEFAULT_WORKFLOW_WINDOW_SECONDS, chat_urls_match, is_chat_url
from .handoff import (HandoffSubmission, SUBMISSION_CONFIGURATION_ERROR,
                      SUBMISSION_OK, classify_submission_failure)

LOGGER = logging.getLogger("agent_relay.chatgpt_sender")


class BrowserChatGPTSender:
    """One-shot configured-URL ChatGPT sender using bounded local Chrome CDP."""

    def __init__(self, config):
        self.url = str(config.chat_url)
        self.profile_dir = Path(os.path.expandvars(os.environ.get("AGENT_RELAY_CHATGPT_PROFILE", "%LOCALAPPDATA%/CodexOrchestrator/profiles/chatgpt"))).expanduser()
        configured_port = int(os.environ.get("AGENT_RELAY_CHATGPT_DEBUG_PORT", "9222"))
        configured_ports = [configured_port, 9222, 9333, 9347]
        self.debug_ports = tuple(dict.fromkeys(configured_ports))
        self.debug_port = configured_port
        self.chrome_path = os.environ.get("AGENT_RELAY_CHROME", "") or self._find_chrome()
        self.timeout = 120
        self.post_submit_delay = int(getattr(config, "post_submit_delay", DEFAULT_WORKFLOW_WINDOW_SECONDS))
        self.close_after_submit = bool(getattr(config, "close_after_submit", False))
        self.window_width = int(getattr(config, "window_width", 640))
        self.window_height = int(getattr(config, "window_height", 480))
        self.owned_process: subprocess.Popen | None = None
        self.require_fixed_chat_url = bool(getattr(config, "require_fixed_chat_url", True))
        self.launch_evidence: dict = {"url": self.url, "profile_dir": str(self.profile_dir), "candidate_ports": list(self.debug_ports), "chrome_path": self.chrome_path}

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

    @staticmethod
    def _read_json(url: str) -> dict | list:
        with urllib.request.urlopen(url, timeout=2) as response:
            return json.loads(response.read().decode("utf-8"))

    def _cdp_at(self, port: int, path: str = "/json/version") -> dict | list:
        return self._read_json(f"http://127.0.0.1:{port}{path}")

    def _cdp(self, path: str = "/json/version") -> dict:
        value = self._cdp_at(self.debug_port, path)
        if not isinstance(value, dict):
            raise ValueError(f"CDP endpoint returned non-object for {path}")
        return value

    def _matching_chrome_process(self, port: int) -> dict | None:
        """Return a Windows Chrome process that advertises this profile and port."""
        if os.name != "nt":
            return None
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not powershell:
            return None
        profile = str(self.profile_dir).replace("/", "\\").lower()
        profile_literal = "'" + profile.replace("'", "''") + "'"
        script = (
            "$profile = " + profile_literal + "; "
            "$port = " + str(port) + "; "
            "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
            "Where-Object { $_.CommandLine -and $_.CommandLine.ToLower().Contains($profile) -and "
            "$_.CommandLine.ToLower().Contains('--remote-debugging-port=' + $port) } | "
            "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
        )
        try:
            result = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=5, check=False)
            if result.returncode or not result.stdout.strip():
                return None
            rows = json.loads(result.stdout)
            if isinstance(rows, dict):
                rows = [rows]
            return rows[0] if isinstance(rows, list) and rows else None
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            return None

    def _find_existing_target(self) -> tuple[int, dict, dict | None] | None:
        """Find a verified endpoint that already owns this profile and port."""
        probes = []
        for port in self.debug_ports:
            try:
                version = self._cdp_at(port, "/json/version")
                pages = self._cdp_at(port, "/json/list")
                if not isinstance(version, dict) or not isinstance(pages, list):
                    continue
                process = self._matching_chrome_process(port)
                probes.append({"port": port, "browser": version.get("Browser"), "page_count": len(pages), "profile_process": process})
                for page in pages:
                    if isinstance(page, dict) and chat_urls_match(str(page.get("url", "")), self.url):
                        return port, version, page
                if process is not None:
                    return port, version, None
            except (OSError, URLError, json.JSONDecodeError, ValueError):
                continue
        self.launch_evidence["existing_cdp_probes"] = probes
        return None

    def _profile_lock_files(self) -> list[str]:
        if not self.profile_dir.exists():
            return []
        names = ("lockfile", "SingletonLock", "SingletonCookie", "SingletonSocket")
        return [str(self.profile_dir / name) for name in names if (self.profile_dir / name).exists()]

    def _write_launch_diagnostic(self) -> str | None:
        """Persist launch evidence without putting browser stderr in the terminal."""
        try:
            home = Path(os.path.expandvars(os.environ.get("GMAIL_COURIER_HOME", "%LOCALAPPDATA%/GmailCourier"))).expanduser()
            target = home / "logs"
            target.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            path = target / f"chatgpt-launch-{stamp}-{os.getpid()}.json"
            path.write_text(json.dumps(self.launch_evidence, indent=2, sort_keys=True), encoding="utf-8")
            return str(path)
        except OSError as exc:
            LOGGER.warning("could not write ChatGPT launch diagnostic: %s", exc)
            return None

    @staticmethod
    def _valid_chat_url(url: str) -> bool:
        return is_chat_url(url)

    @staticmethod
    def _popen_options() -> dict:
        """Start Chrome without creating or activating a foreground window."""
        options = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
        }
        if os.name == "nt":
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            # SW_SHOWMINNOACTIVE leaves a taskbar button but does not activate
            # the new Chrome window over the user's current application.
            startup.wShowWindow = 7
            options["startupinfo"] = startup
            options["creationflags"] = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            )
        return options

    def _show_small_window_without_activation(self) -> None:
        """Restore a small Chrome window without putting it in the foreground."""
        if os.name != "nt":
            return
        user32 = ctypes.windll.user32
        handles = []
        process_id = self.owned_process.pid if self.owned_process is not None else None
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def collect(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            if process_id is not None:
                found_pid = ctypes.c_ulong()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(found_pid))
                if found_pid.value == process_id:
                    handles.append(hwnd)
            return True

        callback = callback_type(collect)
        user32.EnumWindows(callback, 0)
        for hwnd in handles:
            # SW_SHOWNOACTIVATE=4, SWP_NOZORDER=0x0004,
            # SWP_NOACTIVATE=0x0010.
            user32.ShowWindow(hwnd, 4)
            user32.SetWindowPos(hwnd, 0, 20, 20, self.window_width, self.window_height, 0x0004 | 0x0010)

    def _launch(self) -> None:
        existing = self._find_existing_target()
        if existing is not None:
            self.debug_port = existing[0]
            self.launch_evidence.update({"mode": "attached-existing", "selected_port": self.debug_port, "browser": existing[1].get("Browser"), "target_url": existing[2].get("url", "") if existing[2] else "", "target_page_preexisting": existing[2] is not None})
            self._write_launch_diagnostic()
            return
        if not self.chrome_path:
            self.launch_evidence.update({"mode": "failed", "failure": "chrome-executable-not-found"})
            diagnostic = self._write_launch_diagnostic()
            raise RuntimeError("Chrome executable not found")
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        lock_files = self._profile_lock_files()
        if lock_files:
            self.launch_evidence.update({"mode": "failed", "failure": "profile-in-use-without-matching-cdp", "profile_lock_files": lock_files})
            diagnostic = self._write_launch_diagnostic()
            suffix = f"; diagnostic={diagnostic}" if diagnostic else ""
            raise RuntimeError(f"Chrome profile is already in use and no matching CDP target was found: {self.profile_dir}{suffix}")
        selected_port = None
        for port in self.debug_ports:
            try:
                self._cdp_at(port)
            except (OSError, URLError, json.JSONDecodeError, ValueError):
                selected_port = port
                break
        if selected_port is None:
            self.launch_evidence.update({"mode": "failed", "failure": "all-cdp-ports-occupied"})
            diagnostic = self._write_launch_diagnostic()
            suffix = f"; diagnostic={diagnostic}" if diagnostic else ""
            raise RuntimeError(f"all configured Chrome CDP ports are occupied{suffix}")
        self.debug_port = selected_port
        args = [self.chrome_path, f"--user-data-dir={self.profile_dir}", "--remote-debugging-address=127.0.0.1", f"--remote-debugging-port={self.debug_port}", "--no-first-run", "--no-default-browser-check", "--start-minimized", f"--window-size={self.window_width},{self.window_height}", self.url]
        self.launch_evidence.update({"mode": "launch-new", "selected_port": self.debug_port, "command": args, "profile_lock_files_before_launch": lock_files})
        self.owned_process = subprocess.Popen(args, **self._popen_options())
        self.launch_evidence["pid"] = self.owned_process.pid
        self._write_launch_diagnostic()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            returncode = self.owned_process.poll()
            if returncode is not None:
                self.launch_evidence.update({"mode": "failed", "failure": "chrome-exited-before-cdp", "returncode": returncode})
                diagnostic = self._write_launch_diagnostic()
                suffix = f"; diagnostic={diagnostic}" if diagnostic else ""
                raise RuntimeError(f"Chrome exited before CDP became available (returncode={returncode}){suffix}")
            try:
                self._cdp()
                self._show_small_window_without_activation()
                self.launch_evidence["cdp_ready"] = True
                self._write_launch_diagnostic()
                return
            except (OSError, URLError, json.JSONDecodeError, ValueError):
                time.sleep(0.25)
        self.launch_evidence.update({"mode": "failed", "failure": "cdp-timeout", "returncode": self.owned_process.poll(), "profile_lock_files_after_wait": self._profile_lock_files()})
        diagnostic = self._write_launch_diagnostic()
        suffix = f"; diagnostic={diagnostic}" if diagnostic else ""
        raise RuntimeError(f"Chrome CDP did not become available on port {self.debug_port}{suffix}")

    def submit(self, report: str, *, on_submitted=None, stop_event=None) -> HandoffSubmission:
        try:
            from gmail_courier.protocol import validate_chat_payload
            report = validate_chat_payload(report)
        except (TypeError, ValueError) as exc:
            return HandoffSubmission(False, str(exc), category=SUBMISSION_CONFIGURATION_ERROR)
        if not is_chat_url(self.url):
            return HandoffSubmission(False, "ChatGPT URL must be HTTPS on chatgpt.com and contain /c/<conversation-id>", category=SUBMISSION_CONFIGURATION_ERROR)
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return HandoffSubmission(False, "Playwright is not installed", category=SUBMISSION_CONFIGURATION_ERROR)
        try:
            self._launch()
            with sync_playwright() as playwright:
                browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{self.debug_port}")
                if not browser.contexts:
                    raise RuntimeError("Chrome CDP has no browser context")
                context = browser.contexts[0]
                page = next((item for item in context.pages if chat_urls_match(item.url, self.url)), None)
                if page is None:
                    page = context.new_page()
                    page.goto(self.url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
                # Only resize a Chrome process that this invocation launched.
                # Never guess from window titles: Codex and Chrome titles can
                # overlap, and touching an attached window can resize the
                # user's foreground application by mistake.
                if self.owned_process is not None:
                    self._show_small_window_without_activation()
                deadline = time.monotonic() + self.timeout
                composer = None
                while time.monotonic() < deadline:
                    boxes = page.get_by_role("textbox")
                    for index in range(boxes.count() - 1, -1, -1):
                        candidate = boxes.nth(index)
                        if not (candidate.is_visible() and candidate.is_enabled()):
                            continue
                        try:
                            if candidate.is_editable(timeout=250):
                                composer = candidate
                                break
                        except Exception:
                            continue
                    if composer is not None:
                        break
                    page.wait_for_timeout(250)
                if composer is None:
                    raise RuntimeError("configured ChatGPT composer is not enabled")
                composer.fill(report, timeout=self.timeout * 1000)
                composer.press("Enter")
                # A generic protocol marker can already exist in the thread and
                # would produce a false positive. Require this submission's
                # unique handoff token to become visible after pressing Enter.
                token = next((line.split(":", 1)[1].strip() for line in report.splitlines() if line.startswith("HANDOFF_TOKEN:")), "")
                # Direct CLI calls may carry
                # ordinary text rather than an AgentRelay envelope. In that
                # case verify the submitted text itself instead of waiting for
                # a protocol marker that will never exist.
                marker = f"HANDOFF_TOKEN: {token}" if token else report.strip()
                if not marker:
                    raise RuntimeError("cannot verify an empty ChatGPT submission")
                deadline = time.monotonic() + self.timeout
                while time.monotonic() < deadline:
                    # The submitted user turn is visible in the real conversation;
                    # this is deliberately stronger than a local stdout marker.
                    if marker in page.locator("main").inner_text(timeout=3000):
                        if on_submitted is not None:
                            on_submitted()
                        # Leave the submitted turn visible long enough for the
                        # user-facing browser window and remote UI to settle,
                        # but allow a verified external receipt to close it
                        # early. The stop event is controlled by the caller's
                        # bounded workflow, never by an untrusted page.
                        deadline = time.monotonic() + self.post_submit_delay
                        while time.monotonic() < deadline:
                            if stop_event is not None and stop_event.is_set():
                                break
                            time.sleep(min(0.25, max(0.01, deadline - time.monotonic())))
                        if self.close_after_submit or (stop_event is not None and stop_event.is_set()):
                            page.close()
                        return HandoffSubmission(True, "real ChatGPT conversation contains submitted handoff", verified=True, category=SUBMISSION_OK)
                    page.wait_for_timeout(500)
                raise RuntimeError("configured ChatGPT conversation did not visibly receive handoff")
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            return HandoffSubmission(False, detail, category=classify_submission_failure(detail, exc))
        finally:
            if self.owned_process is not None:
                try:
                    self.owned_process.terminate()
                    self.owned_process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                self.owned_process = None

    def verify_token(self, token: str) -> bool:
        """Check an existing configured-conversation token without sending another turn."""
        if not self._valid_chat_url(self.url) or not token:
            return False
        try:
            from playwright.sync_api import sync_playwright
            self._launch()
            with sync_playwright() as playwright:
                browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{self.debug_port}")
                if not browser.contexts:
                    return False
                context = browser.contexts[0]
                page = next((item for item in context.pages if chat_urls_match(item.url, self.url)), None)
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
        require_fixed_chat_url = False
    report = __import__("sys").stdin.read()
    try:
        from gmail_courier.protocol import validate_chat_payload
        report = validate_chat_payload(report)
    except (TypeError, ValueError) as exc:
        print(f"configuration_error: {exc}", file=__import__("sys").stderr)
        return 1
    result = BrowserChatGPTSender(Config()).submit(report)
    if result.ok and result.verified:
        print("SUBMITTED")
        return 0
    print(result.detail, file=__import__("sys").stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
