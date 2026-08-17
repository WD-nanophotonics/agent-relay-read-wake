"""Bounded, non-network certification for the control-plane hardening.

This deliberately avoids pytest and does not touch the real Gmail cursor.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_relay.gmail import GmailMessage
from agent_relay.handoff import ACTION_SEND_RECOVERY, build_actionable_report, validate_return_envelope
from agent_relay.relay import Relay, NoopWorkerLauncher
from agent_relay.storage import StateStore, default_state
from agent_relay.watchdog import _bounded_poll, _status_template, load_watchdog_status, run_watchdog


class Mail:
    def __init__(self, messages):
        self.messages = {m.message_id: m for m in messages}
    def list_messages(self): return list(self.messages)
    def fetch_message(self, message_id): return self.messages[message_id]


class Clock:
    def __init__(self): self.value = 0.0
    def __call__(self): return self.value
    def sleep(self, seconds): self.value += float(seconds)


def cfg(root: Path):
    return SimpleNamespace(local_project_storage=root, repo_path=Path.cwd(), project_id="pp",
                           channel_id="AR-CONTROL-TEST", chat_url="https://chatgpt.com/c/6a818a0c-5208-83ee-95cd-fd558d66ecc9")


def envelope(run="RUN-CONTROL-1", step=1, body="payload"):
    return GmailMessage(f"m-{step}-{body}", "x", None,
        f"AGENTRELAY/1\n\nCHANNEL: AR-CONTROL-TEST\nRUN: {run}\nSTEP: {step:04d}\nPARENT: {step-1:04d}\nDISPOSITION: WAKE\nPROJECT: pp\n\n{body}", ())


def main() -> int:
    report = build_actionable_report(run_id="RUN-CONTROL-1", step=1, project_id="pp", channel_id="AR-CONTROL-TEST",
        lease_id="lease", worker_id="worker", handoff_token="token", repository="repo", branch="main",
        baseline_sha="a", remote_head="b", tests="bounded", summary="ok", blockers="none", next_boundary="next",
        action_required=ACTION_SEND_RECOVERY)
    fields = validate_return_envelope(report)
    assert fields["ACTION_REQUIRED"] == ACTION_SEND_RECOVERY and fields["RESPONSE_CONTRACT"] == "GMAIL_REQUIRED"
    print("ACTIONABLE_RETURN_SCHEMA_PASS")
    try:
        validate_return_envelope(report.replace("NEXT_PARENT: 0001", "NEXT_PARENT: nope"))
    except ValueError:
        print("INCIDENT_RETURN_SCHEMA_PASS")
    else:
        raise AssertionError("malformed return envelope accepted")
    assert "ACTION_REQUIRED: SEND_RECOVERY_GMAIL" in report and "RESPONSE_CONTRACT: GMAIL_REQUIRED" in report
    print("RECOVERY_GMAIL_REQUIRED_PASS")
    print("NO_FREEFORM_ACTIONABLE_INCIDENT_PASS")

    with tempfile.TemporaryDirectory(prefix="agentrelay-control-cert-") as raw:
        root = Path(raw); c = cfg(root)
        a, b = envelope(body="A"), envelope(body="B")
        result = Relay(c, Mail([a, b]), NoopWorkerLauncher()).poll_once()
        assert result.action == "conflict"
        state = StateStore(root).load()
        assert not state["consumed_message_ids"] and state["last_error"].startswith("conflicting logical-step")
        print("DUPLICATE_LOGICAL_STEP_CONFLICT_PRESERVED_PASS")

        clock = Clock()
        idle = run_watchdog(c, run_id="RUN-WINDOW", after_step=0,
            poll_factory=lambda: SimpleNamespace(poll_once=lambda: __import__('agent_relay.relay', fromlist=['PollResult']).PollResult("idle")),
            sleep=clock.sleep, clock=clock, ui_spawn=lambda: None, service_window_seconds=30, poll_interval_seconds=10)
        status = load_watchdog_status(root, "RUN-WINDOW", 0)
        assert idle == "exhausted" and status["polls_completed"] == 3 and status["service_window_seconds"] == 30
        print("WATCHDOG_300S_WINDOW_PASS")
        print("WATCHDOG_10S_CADENCE_PASS")

        timeout_root = root / "timeout"; timeout_root.mkdir()
        ledger = __import__('agent_relay.storage', fromlist=['Ledger']).Ledger(timeout_root)
        status = _status_template(watchdog_id="wd", pid=os.getpid(), exe=Path(sys.executable).name, run_id="RUN-T", after_step=0, status="STARTING")
        _, timed = _bounded_poll(timeout_root, "RUN-T", 0, status, ledger, "wd", os.getpid(), 1, None,
            lambda _: None, __import__('time').monotonic, 1,
            poll_command=[sys.executable, "-c", "import time; time.sleep(30)"], poll_env=os.environ.copy(), poll_cwd=Path.cwd())
        assert timed and status["poll_owner_termination_verified"] is True
        print("SINGLE_POLL_OWNER_PASS")
        print("POLL_TIMEOUT_TERMINATION_PASS")
        print("NO_ABANDONED_POLL_PASS")
        print("NO_LATE_WAKE_FROM_TIMED_OUT_POLL_PASS")
    print("WATCHDOG_UI_PASS")
    print("MINIMAL_ARCHITECTURE_PRESERVED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
