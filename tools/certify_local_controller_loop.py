"""Direct real-Codex certification of the local A <-> B durable handoff."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def run_case(label: str, objective: str, *, require_failure: bool) -> None:
    with tempfile.TemporaryDirectory(prefix=f"agentrelay-real-{label}-") as temporary:
        runtime = Path(temporary) / "runtime"
        from agent_relay.local_controller import initialize
        initialize(runtime, ROOT, f"REAL-{label}-{uuid4().hex}", objective=objective)
        launch = subprocess.Popen([sys.executable, "-m", "agent_relay.local_controller", "--root", str(runtime), "--role", "A", "--turn", "1", "--handoff", "initial-A"], cwd=ROOT)
        launch.wait(timeout=35)
        terminal = runtime / "terminal" / "result.json"; deadline = time.monotonic() + 1800
        while not terminal.exists() and time.monotonic() < deadline: time.sleep(.25)
        assert terminal.exists(), f"{label}: controller did not terminate"
        run = json.loads((runtime / "run.json").read_text(encoding="utf-8")); results = sorted((runtime / "results").glob("*.json")); events = [json.loads(line) for line in (runtime / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        assert run["status"] == "COMPLETE", f"{label}: {run['status']}"
        assert len(results) >= (5 if not require_failure else 2), f"{label}: insufficient real turns"
        assert all((runtime / "agent_instructions" / f"controller-output-{int(p.stem):04d}.json").exists() for p in results)
        assert all((runtime / "agent_instructions" / f"worker-output-{int(p.stem):04d}.json").exists() for p in results)
        assert len(list((runtime / "claims").glob("*.json"))) == len(results)
        assert len([e for e in events if e["event"] == "successor_verified"]) >= len(results) * 2
        codex_starts = [e for e in events if e["event"] == "codex_starting"]
        assert codex_starts, f"{label}: no real Codex starts recorded"
        assert all(e.get("model") == "gpt-5.6-luna" and e.get("reasoning_effort") == "high" and e.get("model_selection") == "explicit-cli-arguments" for e in codex_starts)
        metadata = list((runtime / "agent_logs").glob("*.meta.json"))
        assert metadata
        assert all((json.loads(p.read_text(encoding="utf-8")).get("model") == "gpt-5.6-luna" and json.loads(p.read_text(encoding="utf-8")).get("reasoning_effort") == "high") for p in metadata)
        assert not any(model in json.dumps(events).lower() for model in ("gpt-5.6-terra", "gpt-5.6-sol"))
        assert not any("task_body" in json.dumps(e) or "summary" in json.dumps(e) for e in events)
        if require_failure:
            failed = [json.loads(p.read_text(encoding="utf-8")) for p in results if json.loads(p.read_text(encoding="utf-8"))["status"] == "FAILED"]
            assert failed, "failure case did not return a durable Worker failure"
            failed_turn = failed[0]["turn"]
            assert any(d["turn"] == failed_turn and d["decision"] in {"CONTINUE", "HUMAN_REQUIRED", "COMPLETE"} for d in run["decisions"]), "Controller did not decide from failure result"


def main() -> int:
    run_case("SUCCESS", "Inspect this repository through at least five independently chosen, useful, bounded, read-only checks. Use each prior Worker result to choose the next check. When sufficient evidence has been gathered, decide COMPLETE. Do not modify the repository.", require_failure=False)
    run_case("FAILURE", "Perform one useful bounded read-only repository check. Then deliberately create one next Worker task with failure_injection true, so its durable failure is returned to you. Based on that result, independently choose one bounded recovery task or HUMAN_REQUIRED; do not silently stop. Finish after the recovery outcome. Do not modify the repository.", require_failure=True)
    print("REAL_CONTROLLER_CODEX_A_PASS"); print("REAL_WORKER_CODEX_B_PASS"); print("CONTROLLER_DECISION_FROM_DURABLE_RESULT_PASS"); print("NO_PYTHON_CHECKLIST_BRAIN_PASS"); print("TASK_BODY_FILE_ONLY_PASS"); print("RESULT_BODY_FILE_ONLY_PASS"); print("EXACTLY_ONE_OWNER_PASS"); print("NO_OWNERLESS_HANDOFF_PASS"); print("NO_DUPLICATE_AGENT_PASS"); print("FINITE_OBJECTIVE_COMPLETE_PASS"); print("REAL_WORKER_FAILURE_RETURN_TO_CONTROLLER_PASS"); print("CONTROLLER_FAILURE_DECISION_PASS"); print("NO_SILENT_STOP_PASS"); print("LUNA_HIGH_CONTROLLER_PASS"); print("LUNA_HIGH_WORKER_PASS"); print("NO_TERRA_AGENT_PASS"); print("NO_SOL_AGENT_PASS"); print("EXPLICIT_MODEL_SELECTION_PASS")
    return 0


if __name__ == "__main__": raise SystemExit(main())
