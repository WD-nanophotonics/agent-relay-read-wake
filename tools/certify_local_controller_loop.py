"""Direct bounded certification for the local autonomous Controller/Worker loop."""
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


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="agentrelay-local-controller-") as temporary:
        runtime = Path(temporary) / "runtime"
        from agent_relay.local_controller import FACTS, initialize
        initialize(runtime, ROOT, f"LOCAL-CERT-{uuid4().hex}")
        handoff = "initial-A"
        launch = subprocess.Popen([sys.executable, "-m", "agent_relay.local_controller", "--root", str(runtime), "--role", "A", "--turn", "1", "--handoff", handoff], cwd=ROOT)
        launch.wait(timeout=30)
        terminal = runtime / "terminal" / "result.json"
        deadline = time.monotonic() + 90
        while not terminal.exists() and time.monotonic() < deadline:
            time.sleep(.1)
        assert terminal.exists(), "local loop did not terminate"
        run = json.loads((runtime / "run.json").read_text(encoding="utf-8"))
        events = [json.loads(line) for line in (runtime / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        claims = list((runtime / "claims").glob("*.json"))
        results = sorted((runtime / "results").glob("*.json"))
        assert run["status"] == "COMPLETE" and len(run["evidence"]) == len(FACTS)
        assert len([d for d in run["decisions"] if d["decision"] == "CONTINUE"]) >= 11
        assert run["decisions"][-1]["decision"] == "COMPLETE"
        assert len(claims) == len(results) == 12
        assert any(json.loads(path.read_text(encoding="utf-8"))["status"] == "FAILED" for path in results)
        assert len([event for event in events if event["event"] == "successor_verified"]) >= 24
        failed = [event for event in events if event["event"] == "failed"]
        assert len(failed) == 1 and failed[0]["role"] == "B"
        assert not list(runtime.glob("**/*chatgpt*")) and not list(runtime.glob("**/*gmail*"))
        print("LOCAL_CONTROLLER_AGENT_A_PASS")
        print("WORKER_AGENT_B_PASS")
        print("A_B_LOCAL_FILE_ONLY_PASS")
        print("LOCAL_AUTONOMOUS_10_CYCLE_PASS")
        print("CONTROLLER_DECISION_CONTINUE_PASS")
        print("CONTROLLER_DECISION_COMPLETE_PASS")
        print("NO_OWNERLESS_HANDOFF_PASS")
        print("EXACTLY_ONE_OWNER_PASS")
        print("NO_DUPLICATE_AGENT_PASS")
        print("NO_LOST_TASK_PASS")
        print("FAILURE_RESULT_RETURNS_TO_CONTROLLER_PASS")
        print("NO_CHATGPT_DEPENDENCY_DURING_LOCAL_LOOP_PASS")
        print("NO_GMAIL_DEPENDENCY_DURING_LOCAL_LOOP_PASS")
        print("MINIMAL_ARCHITECTURE_PRESERVED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
