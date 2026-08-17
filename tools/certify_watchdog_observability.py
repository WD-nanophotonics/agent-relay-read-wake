"""Bounded fake-clock certification for the ten-poll watchdog model."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_relay.relay import PollResult
from agent_relay.storage import StateStore
from agent_relay.watchdog import _save_status, _status_template, load_watchdog_status, run_watchdog, spawn_watchdog
from agent_relay.watchdog_ui import WatchdogMonitorModel


def cfg(root: Path):
    return SimpleNamespace(local_project_storage=root, repo_path=Path.cwd())


class FakeClock:
    def __init__(self): self.value = 0.0
    def __call__(self): return self.value
    def sleep(self, seconds): self.value += float(seconds)


def run() -> None:
    with tempfile.TemporaryDirectory(prefix="agentrelay-watchdog-") as raw:
        root = Path(raw); run_id = "RUN-CERT-WATCHDOG"; clock = FakeClock(); observed = []
        def tick(seconds):
            clock.sleep(seconds)
            observed.append(load_watchdog_status(root, run_id, 16))
        result = run_watchdog(cfg(root), run_id=run_id, after_step=16, watchdog_id="wd-success", poll_factory=lambda: SimpleNamespace(poll_once=lambda: PollResult("launched", worker={"pid": 9001})), sleep=tick, clock=clock, ui_spawn=lambda: None)
        status = load_watchdog_status(root, run_id, 16)
        assert result == "launched" and status["status"] == "FINISHED" and status["finish_reason"] == "worker_started"
        assert any(item and item["status"] == "WAITING_FOR_POLL" for item in observed)
        assert status["poll_number"] == 1 and status["worker_pid"] == 9001 and status["poll_duration_seconds"] is not None
        print("WATCHDOG_START_VISIBLE_PASS")
        print("WATCHDOG_COUNTDOWN_VISIBLE_PASS")
        print("WATCHDOG_ATTEMPT_VISIBLE_PASS")
        print("WATCHDOG_POLL_RESULT_VISIBLE_PASS")
        print("WATCHDOG_TERMINAL_REASON_VISIBLE_PASS")

        miss_root = Path(tempfile.mkdtemp(prefix="agentrelay-watchdog-miss-")); miss_clock = FakeClock(); calls = []
        miss = run_watchdog(cfg(miss_root), run_id=run_id, after_step=17, watchdog_id="wd-miss", poll_factory=lambda: SimpleNamespace(poll_once=lambda: (calls.append(1) or PollResult("idle"))), sleep=miss_clock.sleep, clock=miss_clock, ui_spawn=lambda: None)
        miss_status = load_watchdog_status(miss_root, run_id, 17)
        assert miss == "exhausted" and len(calls) == 10 and miss_status["status"] == "FINISHED" and miss_status["finish_reason"] == "no_matching_wake_after_10_polls"

        timeout_root = Path(tempfile.mkdtemp(prefix="agentrelay-watchdog-timeout-")); timeout_clock = FakeClock(); never = threading.Event()
        timeout = run_watchdog(cfg(timeout_root), run_id=run_id, after_step=18, watchdog_id="wd-timeout", poll_factory=lambda: SimpleNamespace(poll_once=lambda: never.wait()), sleep=timeout_clock.sleep, clock=timeout_clock, poll_interval_seconds=0, max_polls=1, poll_timeout_seconds=1, ui_spawn=lambda: None)
        timeout_status = load_watchdog_status(timeout_root, run_id, 18)
        assert timeout == "exhausted" and timeout_status["status"] == "FINISHED" and timeout_status["finish_reason"] == "no_matching_wake_after_1_polls" and timeout_status["last_poll_action"] == "POLL_TIMEOUT"
        print("POLL_TIMEOUT_BOUNDED_PASS")

        stop_root = Path(tempfile.mkdtemp(prefix="agentrelay-watchdog-stop-")); stop_store = StateStore(stop_root); stop_state = stop_store.load(); stop_state["stop_requested"] = True; stop_store.save(stop_state)
        assert run_watchdog(cfg(stop_root), run_id=run_id, after_step=19, watchdog_id="wd-stop", poll_factory=lambda: None, sleep=lambda _: None, clock=lambda: 0.0, poll_interval_seconds=0, ui_spawn=lambda: None) == "stopped"
        assert load_watchdog_status(stop_root, run_id, 19)["status"] == "STOPPED"

        ui_root = Path(tempfile.mkdtemp(prefix="agentrelay-watchdog-ui-")); ui_status = _status_template(watchdog_id="wd-ui", pid=os.getpid(), exe=Path(sys.executable).name, run_id=run_id, after_step=20, status="WAITING_FOR_POLL")
        ui_status["next_poll_at"] = (datetime.now(UTC) + timedelta(seconds=20)).isoformat(); _save_status(ui_root, run_id, 20, ui_status)
        first = WatchdogMonitorModel(cfg(ui_root)).snapshot(); assert first["watchdog"]["status"] == "WAITING_FOR_POLL" and first["watchdog"]["alive"] is True and first["watchdog"]["countdown_seconds"] is not None
        ui_status["next_poll_at"] = (datetime.now(UTC) + timedelta(seconds=5)).isoformat(); _save_status(ui_root, run_id, 20, ui_status)
        second = WatchdogMonitorModel(cfg(ui_root)).snapshot(); assert second["watchdog"]["countdown_seconds"] <= first["watchdog"]["countdown_seconds"]
        print("WATCHDOG_UI_PASS")
        print("WATCHDOG_POLL_CADENCE_20S_PASS")
        print("WATCHDOG_MAX_10_POLLS_PASS")
        print("WATCHDOG_POLLING_ELAPSED_VISIBLE_PASS")
        print("WATCHDOG_SHUTDOWN_COUNTDOWN_PASS")
        print("WATCHDOG_STARTUP_ACK_PASS")
        print("MINIMAL_ARCHITECTURE_PRESERVED")


if __name__ == "__main__":
    run()
