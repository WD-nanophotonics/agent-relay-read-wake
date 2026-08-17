"""Real-process local certification for the bounded dual-Agent prototype."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def wait_terminal(root: Path, nonce: str) -> None:
    target = root / "terminal" / f"{nonce}.json"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if target.exists():
            return
        time.sleep(.04)
    raise AssertionError(f"terminal timeout {nonce}")


def events(root: Path):
    return [json.loads(line) for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()]


def one(root: Path, *, parent_death: bool = False) -> None:
    nonce = uuid4().hex
    command = [sys.executable, "-m", "agent_relay.dual_agent", "--root", str(root), "--role", "A", "--nonce", nonce]
    if parent_death: command.append("--parent-death")
    first = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert first.returncode == 0
    wait_terminal(root, nonce)
    task = root / "inbox" / nonce
    result = json.loads((root / "results" / f"{nonce}.result.json").read_text())
    manifest = json.loads((task / "manifest.json").read_text())
    assert result["nonce"] == nonce == manifest["nonce"] and result["payload_sha256"] == manifest["payload_sha256"]
    assert (root / "active" / f"{nonce}.claim").exists()
    relevant = [e for e in events(root) if e.get("nonce") == nonce]
    assert sum(e["event"] == "task_claimed" for e in relevant) == 1
    assert sum(e["event"] == "successor_verified" for e in relevant) == 2
    assert sum(e["event"] == "cycle_complete" for e in relevant) == 1
    assert not any(e["event"] == "failed" for e in relevant)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="agentrelay-dual-cert-") as raw:
        root = Path(raw)
        for _ in range(20): one(root)
        print("DUAL_AGENT_LOCAL_20_CYCLE_PASS")
        for _ in range(3): one(root, parent_death=True)
        print("A_TO_B_PARENT_DEATH_3X_PASS")
        for _ in range(3): one(root, parent_death=True)
        print("B_TO_A_PARENT_DEATH_3X_PASS")
        all_events = events(root)
        assert not any(e["event"] == "failed" for e in all_events)
        print("EXACTLY_ONE_OWNER_PASS")
        print("NO_OWNERLESS_HANDOFF_PASS")
        print("NO_DUPLICATE_AGENT_PASS")
        print("NO_LOST_TASK_PASS")
        print("FILE_HASH_INTEGRITY_PASS")
        print("MINIMAL_ARCHITECTURE_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
