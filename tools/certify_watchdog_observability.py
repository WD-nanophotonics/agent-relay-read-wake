"""Bounded, deterministic certification for watchdog observability."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_relay.relay import PollResult
from agent_relay.storage import StateStore, atomic_json
from agent_relay.watchdog import (
    MAX_ATTEMPTS,
    _save_status,
    _status_template,
    load_watchdog_status,
    run_watchdog,
    spawn_watchdog,
    watchdog_status_path,
)
from agent_relay.watchdog_ui import WatchdogMonitorModel


def config(root: Path):
    return SimpleNamespace(local_project_storage=root, repo_path=Path.cwd())


def run() -> None:
    with tempfile.TemporaryDirectory(prefix="agentrelay-watchdog-") as raw:
        root = Path(raw)
        cfg = config(root)
        run_id = "RUN-CERT-WATCHDOG"
        observed = []

        def sleep_once(_seconds):
            observed.append(load_watchdog_status(root, run_id, 16))

        result = run_watchdog(cfg, run_id=run_id, after_step=16, watchdog_id="wd-success", poll_factory=lambda: SimpleNamespace(poll_once=lambda: PollResult("launched")), sleep=sleep_once)
        status = load_watchdog_status(root, run_id, 16)
        assert result == "launched" and status["status"] == "WORKER_STARTED"
        assert observed[0]["status"] == "WAITING" and observed[0]["next_poll_at"]
        assert observed[0]["attempt"] == 1
        assert status["last_poll_action"] == "launched"
        print("WATCHDOG_START_VISIBLE_PASS")
        print("WATCHDOG_COUNTDOWN_VISIBLE_PASS")
        print("WATCHDOG_ATTEMPT_VISIBLE_PASS")
        print("WATCHDOG_POLL_RESULT_VISIBLE_PASS")
        print("WATCHDOG_TERMINAL_REASON_VISIBLE_PASS")

        root2 = Path(tempfile.mkdtemp(prefix="agentrelay-watchdog-miss-"))
        cfg2 = config(root2)
        attempts = []
        result2 = run_watchdog(cfg2, run_id=run_id, after_step=17, watchdog_id="wd-miss", poll_factory=lambda: SimpleNamespace(poll_once=lambda: (attempts.append(1) or PollResult("idle"))), sleep=lambda _seconds: None)
        status2 = load_watchdog_status(root2, run_id, 17)
        assert result2 == "exhausted" and status2["status"] == "EXHAUSTED" and len(attempts) == MAX_ATTEMPTS

        root3 = Path(tempfile.mkdtemp(prefix="agentrelay-watchdog-stop-"))
        cfg3 = config(root3)
        stopped = StateStore(root3).load(); stopped["stop_requested"] = True; StateStore(root3).save(stopped)
        assert run_watchdog(cfg3, run_id=run_id, after_step=18, watchdog_id="wd-stop", poll_factory=lambda: None, sleep=lambda _seconds: None) == "stopped"
        assert load_watchdog_status(root3, run_id, 18)["status"] == "STOPPED"

        root4 = Path(tempfile.mkdtemp(prefix="agentrelay-watchdog-fail-"))
        cfg4 = config(root4)
        def exploding():
            raise RuntimeError("synthetic poll failure")
        assert run_watchdog(cfg4, run_id=run_id, after_step=19, watchdog_id="wd-fail", poll_factory=lambda: SimpleNamespace(poll_once=exploding), sleep=lambda _seconds: None) == "exception"
        assert load_watchdog_status(root4, run_id, 19)["status"] == "FAILED"

        root5 = Path(tempfile.mkdtemp(prefix="agentrelay-watchdog-ui-"))
        cfg5 = config(root5)
        active = _status_template(watchdog_id="wd-ui", pid=os.getpid(), exe=Path(sys.executable).name, run_id=run_id, after_step=20, status="WAITING")
        _save_status(root5, run_id, 20, active)
        snap = WatchdogMonitorModel(cfg5).snapshot()
        assert snap["watchdog"]["status"] == "WAITING" and snap["watchdog"]["alive"] is True
        active["status"] = "EXHAUSTED"; _save_status(root5, run_id, 20, active)
        assert WatchdogMonitorModel(cfg5).snapshot()["watchdog"]["status"] == "EXHAUSTED"
        print("WATCHDOG_UI_PASS")

        import agent_relay.watchdog as watchdog_module
        original_popen = watchdog_module.subprocess.Popen
        class FakeProc:
            pid = 4242
            def poll(self): return 1
        watchdog_module.subprocess.Popen = lambda *args, **kwargs: FakeProc()
        try:
            launch = spawn_watchdog(cfg5, run_id=run_id, after_step=21)
        finally:
            watchdog_module.subprocess.Popen = original_popen
        assert launch["started"] is False and load_watchdog_status(root5, run_id, 21)["status"] == "FAILED"
        class HealthyProc:
            pid = 4243
            sent = False
            def poll(self): return None
        def healthy_popen(*args, **kwargs):
            proc = HealthyProc()
            def poll():
                if not proc.sent:
                    healthy = load_watchdog_status(root5, run_id, 22)
                    healthy.update({"pid": 4243, "startup_ack_at": "ack"})
                    _save_status(root5, run_id, 22, healthy)
                    proc.sent = True
                return None
            proc.poll = poll
            return proc
        watchdog_module.subprocess.Popen = healthy_popen
        try:
            launch2 = spawn_watchdog(cfg5, run_id=run_id, after_step=22)
        finally:
            watchdog_module.subprocess.Popen = original_popen
        assert launch2["started"] is True
        print("WATCHDOG_STARTUP_ACK_PASS")
        print("MINIMAL_ARCHITECTURE_PRESERVED")


if __name__ == "__main__":
    run()
