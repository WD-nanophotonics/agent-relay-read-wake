from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import queue
import subprocess
from threading import Event, Lock, Thread
import time
from typing import Any
from uuid import uuid4


class AppServerError(RuntimeError):
    pass


@dataclass(frozen=True)
class AppServerThread:
    thread_id: str
    status: str
    raw: dict[str, Any]
    source_kind: str = ""
    cwd: str = ""


@dataclass(frozen=True)
class AppServerTurn:
    turn_id: str
    thread_id: str
    status: str = "started"
    raw: dict[str, Any] | None = None


class AppServerController:
    """Single-owner JSONL controller for one local Codex App Server process."""

    def __init__(self, command: str, repo_path: Path, log_path: Path, worker_id: str, dev_session_id: str = ""):
        self.command = command
        self.repo_path = repo_path.resolve()
        self.log_path = log_path
        self.worker_id = worker_id
        self.dev_session_id = dev_session_id
        self.process: subprocess.Popen[str] | None = None
        self.reader: Thread | None = None
        self.messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self.write_lock = Lock()
        self.stop_event = Event()
        self.next_id = 0
        self.connection_id = str(uuid4())
        self.initialized = False
        self.last_status: str = "unknown"
        self.last_error: str | None = None
        self.last_turn_id: str | None = None
        self.notification_events: list[dict[str, Any]] = []

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.process else None

    @property
    def alive(self) -> bool:
        return bool(self.process and self.process.poll() is None and self.initialized and not self.stop_event.is_set())

    def start(self) -> dict[str, Any]:
        if self.process and self.process.poll() is None:
            if self.alive:
                return {"connection_id": self.connection_id, "pid": self.pid}
            raise AppServerError("owned App Server is already running but unhealthy")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        log = self.log_path.open("a", encoding="utf-8")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        startup = None
        if hasattr(subprocess, "STARTUPINFO"):
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
        self.stop_event.clear()
        try:
            self.process = subprocess.Popen(
                [self.command, "app-server", "--stdio"],
                cwd=self.repo_path,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=log,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                shell=False,
                creationflags=flags,
                startupinfo=startup,
            )
        except OSError as exc:
            log.close()
            raise AppServerError(f"App Server launch failed: {exc}") from exc
        finally:
            if self.process is not None:
                log.close()
        self.reader = Thread(target=self._read_loop, name="agentrelay-app-server-reader", daemon=True)
        self.reader.start()
        try:
            result = self.request("initialize", {"clientInfo": {"name": "agent-relay", "version": "2.0"}, "capabilities": {"experimentalApi": True}}, timeout=20)
            self._send_notification("initialized", {})
            self.initialized = True
            self.last_status = "healthy"
            return {"connection_id": self.connection_id, "pid": self.pid, "initialize": result}
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        self.stop_event.set()
        process = self.process
        if not process:
            return
        try:
            if process.stdin:
                process.stdin.close()
        except OSError:
            pass
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                    process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        if self.reader:
            self.reader.join(timeout=5)
        self.initialized = False
        self.last_status = "stopped"
        self.process = None

    def _read_loop(self) -> None:
        process = self.process
        if not process or not process.stdout:
            return
        try:
            for line in process.stdout:
                if self.stop_event.is_set():
                    break
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as exc:
                    self.last_error = f"malformed App Server JSON: {exc}"
                    self.messages.put({"__agentrelay_error__": self.last_error})
                    continue
                if isinstance(message, dict):
                    self.messages.put(message)
        except OSError as exc:
            self.last_error = str(exc)
            self.messages.put({"__agentrelay_error__": self.last_error})
        finally:
            if not self.stop_event.is_set():
                self.last_error = self.last_error or "App Server stdout closed unexpectedly"
                self.messages.put({"__agentrelay_eof__": self.last_error})

    def _send(self, payload: dict[str, Any]) -> None:
        if not self.process or not self.process.stdin or self.process.poll() is not None:
            raise AppServerError("App Server is not running")
        encoded = json.dumps(payload, separators=(",", ":")) + "\n"
        with self.write_lock:
            self.process.stdin.write(encoded)
            self.process.stdin.flush()

    def _send_notification(self, method: str, params: dict[str, Any]) -> None:
        self._send({"method": method, "params": params})

    def request(self, method: str, params: dict[str, Any], timeout: float = 20) -> dict[str, Any]:
        self.next_id += 1
        request_id = self.next_id
        self._send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                message = self.messages.get(timeout=max(0.05, deadline - time.monotonic()))
            except queue.Empty:
                break
            if "__agentrelay_eof__" in message:
                raise AppServerError(message["__agentrelay_eof__"])
            if "__agentrelay_error__" in message:
                raise AppServerError(message["__agentrelay_error__"])
            if message.get("id") != request_id:
                self._handle_server_message(message)
                continue
            if "error" in message:
                raise AppServerError(f"{method} failed: {message['error']}")
            return message.get("result", {})
        raise AppServerError(f"timed out waiting for App Server response: {method}")

    def _handle_server_message(self, message: dict[str, Any]) -> None:
        if message.get("method") and "id" not in message:
            self.notification_events.append(message)
        elif message.get("method") and "id" in message:
            # AgentRelay never grants interactive approval or user input.
            try:
                self._send({"id": message["id"], "error": {"code": -32000, "message": "AgentRelay does not approve interactive requests"}})
            except AppServerError:
                pass

    def poll_notifications(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = self.notification_events
        self.notification_events = []
        while True:
            try:
                message = self.messages.get_nowait()
            except queue.Empty:
                break
            if "__agentrelay_eof__" in message:
                raise AppServerError(message["__agentrelay_eof__"])
            if "__agentrelay_error__" in message:
                raise AppServerError(message["__agentrelay_error__"])
            if message.get("method"):
                self._handle_server_message(message)
        events.extend(self.notification_events)
        self.notification_events = []
        return events

    def list_threads(self, cwd: Path | None = None) -> list[AppServerThread]:
        params: dict[str, Any] = {"limit": 100, "useStateDbOnly": True, "sourceKinds": ["appServer"]}
        if cwd:
            params["cwd"] = str(cwd.resolve()).replace("\\", "/")
        result = self.request("thread/list", params)
        data = result.get("data", [])
        items = data.get("items", []) if isinstance(data, dict) else data
        return [self._thread_from_item(item) for item in items if item.get("id")]

    @staticmethod
    def _thread_from_item(item: dict[str, Any]) -> AppServerThread:
        return AppServerThread(
            str(item.get("id")),
            str(item.get("status", {}).get("type", "unknown")),
            item,
            str(item.get("threadSource", "")),
            str(item.get("cwd", "")),
        )

    def find_worker(self, worker_id: str | None = None) -> AppServerThread | None:
        wanted = worker_id or self.worker_id
        return next((item for item in self.list_threads(self.repo_path) if item.thread_id == wanted), None)

    def read_worker(self, worker_id: str | None = None) -> AppServerThread:
        wanted = worker_id or self.worker_id
        result = self.request("thread/read", {"threadId": wanted, "includeTurns": False})
        item = result.get("thread", result)
        if not item.get("id"):
            raise AppServerError(f"thread/read returned no thread: {result}")
        return self._thread_from_item(item)

    def start_worker(self) -> AppServerThread:
        result = self.request("thread/start", {"cwd": str(self.repo_path), "threadSource": "appServer", "approvalPolicy": "never", "sandbox": "workspace-write"})
        item = result.get("thread", result)
        thread_id = str(item.get("id", ""))
        if not thread_id:
            raise AppServerError(f"thread/start returned no thread id: {result}")
        self.worker_id = thread_id
        direct = self._thread_from_item(item)
        try:
            exact = self.read_worker(thread_id)
        except AppServerError:
            # The exact thread/start response is authoritative on this owned
            # connection even if a follow-up read is not yet available.
            exact = direct
        if exact.thread_id != thread_id:
            raise AppServerError("thread/read returned an unrelated worker")
        return exact

    def resume_worker(self, worker_id: str) -> AppServerThread:
        result = self.request("thread/resume", {"threadId": worker_id, "cwd": str(self.repo_path), "approvalPolicy": "never", "sandbox": "workspace-write", "excludeTurns": True})
        item = result.get("thread", result)
        status = str(item.get("status", {}).get("type", "unknown"))
        return self._thread_from_item(item)

    def start_turn(self, worker_id: str, instruction: str, writable_roots: list[Path]) -> AppServerTurn:
        result = self.request("turn/start", {
            "threadId": worker_id,
            "input": [{"type": "text", "text": instruction}],
            "approvalPolicy": "never",
            "sandboxPolicy": {"type": "workspaceWrite", "networkAccess": False, "writableRoots": [str(p.resolve()) for p in writable_roots]},
            "summary": "none",
        })
        turn = result.get("turn", result)
        turn_id = str(turn.get("id", ""))
        if not turn_id:
            raise AppServerError(f"turn/start returned no turn id: {result}")
        self.last_turn_id = turn_id
        return AppServerTurn(turn_id, worker_id, str(turn.get("status", "started")), turn)

    def wait_for_turn(self, thread_id: str, turn_id: str, timeout: float = 120) -> AppServerTurn:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                message = self.messages.get(timeout=max(0.05, deadline - time.monotonic()))
            except queue.Empty:
                break
            if "__agentrelay_eof__" in message:
                raise AppServerError(message["__agentrelay_eof__"])
            if "__agentrelay_error__" in message:
                raise AppServerError(message["__agentrelay_error__"])
            if message.get("method") == "turn/completed":
                params = message.get("params", {})
                turn = params.get("turn", {})
                if params.get("threadId") == thread_id and turn.get("id") == turn_id:
                    status = turn.get("status", "completed")
                    return AppServerTurn(turn_id, thread_id, str(status), turn)
            self._handle_server_message(message)
        raise AppServerError("timed out waiting for App Server turn completion")
