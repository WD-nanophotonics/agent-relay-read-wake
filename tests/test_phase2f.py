from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from agent_relay.app_server import AppServerController, AppServerError, AppServerThread
from agent_relay.config import RelayConfig
from agent_relay.gmail import GmailMessage
from agent_relay.supervisor import Supervisor, SupervisorState
from agent_relay.wake import CodexAppServerWakeAdapter, CodexTarget, MockWakeAdapter, WakeResult


class FakeGmail:
    def list_messages(self): return []
    def fetch_message(self, _message_id): raise KeyError(_message_id)
    def test_connection(self): return None


def cfg(root: Path) -> RelayConfig:
    return RelayConfig("gmail-courier", "Gmail Courier", "AR-GMAILCOURIER-A1R7P", root, root / "storage", "codex-app-server", "worker", "Dedicated", "https://chatgpt.com/c/6a818a0c-5208-83ee-95cd-fd558d66ecc9", 20, True, root / "gmail", "codex.cmd", "dev")


class FakeController:
    instances = []

    def __init__(self, command, repo_path, log_path, worker_id, dev_session_id):
        self.command, self.repo_path, self.log_path = command, repo_path, log_path
        self.worker_id, self.dev_session_id = worker_id, dev_session_id
        self.pid, self.initialized, self.stopped = 4321, False, False
        self.turns = []
        self.__class__.instances.append(self)

    @property
    def alive(self): return self.initialized and not self.stopped

    def start(self): self.initialized = True; return {"pid": self.pid}
    def stop(self): self.stopped = True; self.initialized = False
    def find_worker(self, worker_id=None): return AppServerThread(worker_id or self.worker_id, "idle", {})
    def start_turn(self, worker_id, instruction, roots):
        self.turns.append((worker_id, instruction, roots))
        from agent_relay.app_server import AppServerTurn
        return AppServerTurn("turn-1", worker_id, "started", {})
    def poll_notifications(self): return [{"method": "turn/completed", "params": {"threadId": self.worker_id, "turn": {"id": "turn-1", "status": "completed"}}}]


def test_app_server_adapter_owns_one_backend_and_one_turn(monkeypatch, tmp_path):
    monkeypatch.setattr("agent_relay.wake.AppServerController", FakeController)
    adapter = CodexAppServerWakeAdapter(CodexTarget("codex-app-server", "worker", "Dedicated", tmp_path), tmp_path / "logs", local_project_storage=tmp_path / "storage", dev_session_id="dev")
    started = adapter.start_backend()
    assert started.accepted and adapter.controller.pid == 4321
    assert adapter.validate_target(adapter.target).accepted
    lease = __import__("agent_relay.wake", fromlist=["WakeLease"]).WakeLease.create("gmail-courier", "RUN", 1, tmp_path / "instruction", worker_id="worker")
    result = adapter.wake(lease, "instruction")
    assert result.accepted and result.turn_id == "turn-1"
    assert adapter.turn_completed(lease)
    adapter.stop_backend()
    assert adapter.controller is None and FakeController.instances[-1].stopped


def test_app_server_never_uses_cli_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr("agent_relay.wake.AppServerController", FakeController)
    target = CodexTarget("codex-app-server", "worker", "Dedicated", tmp_path)
    adapter = CodexAppServerWakeAdapter(target, tmp_path / "logs", command="definitely-not-invoked", dev_session_id="dev")
    assert adapter.start_backend().accepted


def test_supervisor_calls_backend_lifecycle_and_records_turn(tmp_path):
    class Adapter(MockWakeAdapter):
        worker_id = "owned-worker"
        def __init__(self): super().__init__(); self.started = 0; self.stopped = 0
        def start_backend(self): self.started += 1; return WakeResult(True, "started", process_id=9)
        def stop_backend(self): self.stopped += 1
        def validate_target(self, _target): return WakeResult(True, "ok")

    adapter = Adapter()
    relay = Supervisor(replace(cfg(tmp_path), target_type="mock"), FakeGmail(), adapter)
    relay.start()
    assert adapter.started == 1 and relay.snapshot()["last"]["backend_process_id"] == 9
    relay.stop()
    assert adapter.stopped == 1 and relay.snapshot()["state"] == SupervisorState.STOPPED


def test_app_server_reader_rejects_malformed_json_and_eof(tmp_path):
    class Stdout:
        def __iter__(self): return iter(["not-json\n"])
    class Process:
        stdout = Stdout()
    controller = AppServerController("codex.cmd", tmp_path, tmp_path / "x.log", "worker")
    controller.process = Process()
    controller._read_loop()
    with pytest.raises(AppServerError, match="malformed"):
        controller.poll_notifications()
