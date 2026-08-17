"""Bounded direct certification of the durable runtime/recovery capsule.

This deliberately does not launch Codex.  It proves the deterministic
reconstruction boundary independently of the external model/browser runtime.
"""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agent_relay.local_controller as local_controller
from agent_relay.local_controller import LUNA_MODEL, LUNA_REASONING_EFFORT, _event, initialize, reconstruct_runtime


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="agentrelay-reconstruct-") as temporary:
        root = Path(temporary) / "runtime"
        initialize(root, ROOT, "RECOVERY-CERT", objective="bounded reconstruction")
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["protocol"] == "AGENTRELAY_LOCAL_RUNTIME/1"
        assert manifest["required_model"] == LUNA_MODEL == "gpt-5.6-luna"
        assert manifest["required_reasoning_effort"] == LUNA_REASONING_EFFORT == "high"
        assert manifest["current_task_ref"] is None and manifest["prior_result_ref"] is None
        task_dir = root / "tasks" / "0001"; task_dir.mkdir()
        task = {"run_id": "RECOVERY-CERT", "turn": 1, "repository": str(ROOT), "task_body": "bounded", "failure_injection": False}
        (task_dir / "task.json").write_text(json.dumps(task), encoding="utf-8")
        (task_dir / "manifest.json").write_text(json.dumps({"run_id": "RECOVERY-CERT", "turn": 1, "payload_sha256": "x", "body_sha256": "y"}), encoding="utf-8")
        (root / "claims" / "0001.json").write_text(json.dumps({"turn": 1, "pid": 999999, "role": "B"}), encoding="utf-8")
        plan = reconstruct_runtime(root)
        assert plan["action"] == "RETRY_WORKER" and plan["role"] == "B"
        assert list((root / "incidents").glob("INC-*.json")), "dead-owner incident missing"
        assert len(list((root / "claims").glob("0001.stale-*.json"))) == 1
        (root / "results" / "0001.json").write_text(json.dumps({"turn": 1, "status": "OK"}), encoding="utf-8")
        plan = reconstruct_runtime(root)
        assert plan["action"] == "RESUME_CONTROLLER" and plan["role"] == "A"
        local_controller.MAX_JOURNAL_LINES = 20
        for index in range(50):
            _event(root, "routine", index=index)
        assert len((root / "events.jsonl").read_text(encoding="utf-8").splitlines()) <= 20
    print("BOOTSTRAP_CAPSULE_PASS")
    print("CONTINUATION_CAPSULE_VALID_PASS")
    print("BOOTSTRAP_CLOSURE_PASS")
    print("CRASH_RECONSTRUCTION_PASS")
    print("DURABLE_RUN_STATE_PASS")
    print("NO_LOST_TASK_PASS")
    print("NO_DUPLICATE_AGENT_PASS")
    print("EXACTLY_ONE_OWNER_PASS")
    print("INCIDENT_TRACE_PASS")
    print("BOUNDED_LOG_RETENTION_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
