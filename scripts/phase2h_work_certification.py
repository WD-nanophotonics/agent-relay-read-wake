"""Run exactly one bounded Phase 2H WORK lease and await external handoff evidence."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_relay.config import EXPECTED_CHAT_URL, app_home, load_config
from agent_relay.gmail import GoogleGmailGateway
from agent_relay.supervisor import Supervisor
from agent_relay.wake import CodexAppServerWakeAdapter, CodexTarget, LeaseKind


MESSAGE_ID = "1a00b2d01b2f0944"


def main() -> int:
    config = load_config(app_home())
    target = CodexTarget(config.target_type, config.target_id, config.target_label, config.repo_path)
    adapter = CodexAppServerWakeAdapter(target, config.local_project_storage / "logs", config.codex_command, config.local_project_storage, config.dev_session_id)
    relay = Supervisor(config, GoogleGmailGateway(config.gmail_auth_home), adapter)
    control_path = config.local_project_storage / "diagnostics" / "phase2h-control.json"
    try:
        relay.start()
        result = relay.process_message_id(MESSAGE_ID, LeaseKind.WORK)
        snap = relay.snapshot()
        lease = snap.get("active_lease") or {}
        report = "\n".join([
            "AGENTRELAY PHASE2H HANDOFF CERTIFICATION",
            "RUN: RUN-20260816-PHASE2",
            "STEP: 0008",
            f"LEASE: {lease.get('lease_id', '')}",
            f"WORKER: {lease.get('worker_id', '')}",
            f"HANDOFF_TOKEN: {lease.get('handoff_token', '')}",
            "RESULT: WORK_HANDOFF_DIAGNOSTIC",
        ])
        control_path.parent.mkdir(parents=True, exist_ok=True)
        control_path.write_text(json.dumps({
            "lease_id": lease.get("lease_id"),
            "worker_id": lease.get("worker_id"),
            "completion_token": lease.get("completion_token"),
            "handoff_token": lease.get("handoff_token"),
            "chat_url": EXPECTED_CHAT_URL,
            "report": report,
            "wake_result": result,
            "turn_id": lease.get("turn_id"),
        }, indent=2), encoding="utf-8")
        print(json.dumps({"wake_result": result, "state": snap["state"], "lease_id": lease.get("lease_id"), "worker_id": lease.get("worker_id"), "turn_id": lease.get("turn_id"), "control_path": str(control_path)}, ensure_ascii=False), flush=True)
        if result != "wake-accepted":
            return 2
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            time.sleep(1)
            if relay.consume_completion_record():
                final = relay.snapshot()
                print(json.dumps({"completed": True, "state": final["state"], "active_lease": final.get("active_lease"), "worker_status": adapter.worker_status, "turn_status": adapter.last_turn_status}, ensure_ascii=False), flush=True)
                return 0
        relay.fail_active_lease("Phase 2H WORK handoff/completion timed out")
        print(json.dumps({"completed": False, "state": relay.snapshot()["state"], "worker_status": adapter.worker_status, "turn_status": adapter.last_turn_status}, ensure_ascii=False), flush=True)
        return 3
    except Exception as exc:
        if relay.snapshot().get("active_lease"):
            relay.fail_active_lease(str(exc))
        print(json.dumps({"error": str(exc), "state": relay.snapshot().get("state")}, ensure_ascii=False), flush=True)
        return 4
    finally:
        relay.stop()


if __name__ == "__main__":
    raise SystemExit(main())
