"""Bounded Phase 2I persistent Supervisor certification for STEP-0009 -> STEP-0010."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_relay.config import EXPECTED_CHAT_URL, app_home, load_config
from agent_relay.gmail import GoogleGmailGateway
from agent_relay.handoff import build_actionable_report
from agent_relay.supervisor import Supervisor, SupervisorState
from agent_relay.wake import CodexAppServerWakeAdapter, CodexTarget, LeaseKind


STEP9_MESSAGE_ID = "1a00b4564b19ef0f"


def main() -> int:
    config = load_config(app_home())
    target = CodexTarget(config.target_type, config.target_id, config.target_label, config.repo_path)
    adapter = CodexAppServerWakeAdapter(target, config.local_project_storage / "logs", config.codex_command, config.local_project_storage, config.dev_session_id)
    relay = Supervisor(config, GoogleGmailGateway(config.gmail_auth_home), adapter)
    control = config.local_project_storage / "diagnostics" / "phase2i-control.json"
    evidence = config.local_project_storage / "diagnostics" / "phase2i-evidence.json"
    baseline = "7bd28c3e5ea52bb337f67c8266eeba36df98e687"
    persistent_before = relay.snapshot()
    try:
        relay.start()
        relay.poll_once()
        snap = relay.snapshot()
        active = snap.get("active_lease") or {}
        if snap.get("state") != SupervisorState.AGENT_RUNNING or not active or active.get("step") != 9:
            raise RuntimeError(f"STEP-0009 was not automatically woken: {snap.get('state')}")
        remote_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=config.repo_path, text=True).strip()
        report = build_actionable_report(run_id="RUN-20260816-PHASE2", step=9, project_id=config.project_id, channel_id=config.channel_id, lease_id=active["lease_id"], worker_id=active["worker_id"], handoff_token=active["handoff_token"], repository="WD-nanophotonics/agent-relay-read-wake", branch="main", baseline_sha=baseline, remote_head=remote_head, tests="39 passed", summary="Added persistent Supervisor run mode and deterministic return-path handoff protocol.", blockers="KNOWN_NON_BLOCKING_UX_DEFECT: Chrome foreground takeover", next_boundary="Phase 2J")
        control.parent.mkdir(parents=True, exist_ok=True)
        control.write_text(json.dumps({"lease_id": active["lease_id"], "worker_id": active["worker_id"], "completion_token": active["completion_token"], "handoff_token": active["handoff_token"], "chat_url": EXPECTED_CHAT_URL, "report": report, "turn_id": active.get("turn_id")}, indent=2), encoding="utf-8")
        print(json.dumps({"step9_state": snap["state"], "lease_id": active["lease_id"], "worker_id": active["worker_id"], "turn_id": active.get("turn_id"), "control_path": str(control), "persistent_wait": True}, ensure_ascii=False), flush=True)
        deadline = time.monotonic() + 600
        step9_completed = False
        step9_wait_state = None
        before_step10_ids = set(snap.get("consumed_message_ids", []))
        while time.monotonic() < deadline:
            time.sleep(5)
            if not step9_completed:
                relay.consume_completion_record()
                current = relay.snapshot()
                if current.get("state") == SupervisorState.WAITING_FOR_REPLY and current.get("active_lease") is None:
                    step9_completed = True
                    step9_wait_state = current
                    before_step10_ids = set(current.get("consumed_message_ids", []))
                    print(json.dumps({"step9_completed": True, "state": current["state"], "persistent_wait": True}, ensure_ascii=False), flush=True)
                continue
            relay.poll_once()
            current = relay.snapshot()
            new_ids = [mid for mid in current.get("consumed_message_ids", []) if mid not in before_step10_ids]
            active2 = current.get("active_lease") or {}
            if active2 and active2.get("step") == 10 and current.get("state") == SupervisorState.AGENT_RUNNING:
                result = {"step9_state_before_return": step9_wait_state, "step10_message_id": new_ids[-1] if new_ids else current.get("last", {}).get("gmail_message_id"), "step10_state": current["state"], "step10_lease_id": active2.get("lease_id"), "step10_turn_id": active2.get("turn_id"), "step10_worker_id": active2.get("worker_id"), "step10_staged_path": active2.get("staged_instruction_path"), "persistent_wait": True, "second_automatic_wake": True}
                evidence.write_text(json.dumps(result, indent=2), encoding="utf-8")
                print(json.dumps(result, ensure_ascii=False), flush=True)
                # Certification ends at the second turn start. Resolve the
                # deliberately unfinished tiny lease through the audited path.
                relay.fail_active_lease("Phase 2I certification boundary after STEP-0010 turn start")
                return 0
        evidence.write_text(json.dumps({"return_gmail_timeout": True, "persistent_wait": True, "state": relay.snapshot()["state"]}, indent=2), encoding="utf-8")
        print(json.dumps({"return_gmail_timeout": True, "state": relay.snapshot()["state"]}, ensure_ascii=False), flush=True)
        return 3
    except Exception as exc:
        evidence.write_text(json.dumps({"error": str(exc), "state": relay.snapshot().get("state")}, indent=2), encoding="utf-8")
        print(json.dumps({"error": str(exc), "state": relay.snapshot().get("state")}, ensure_ascii=False), flush=True)
        return 4
    finally:
        relay.stop()


if __name__ == "__main__":
    raise SystemExit(main())
