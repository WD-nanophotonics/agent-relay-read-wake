"""Real new-run black-box continuation attempt; never touches the frozen run."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
MECHANICS = Path(os.environ.get("RELAY_CERT_REPOSITORY", Path.cwd()))
CHAT_URL = os.environ.get("RELAY_CERT_CHAT_URL", "")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def main() -> int:
    run_id = f"RUN-CONTROL-HARDENING-{uuid4().hex.upper()}"
    root = ROOT / ".agentrelay-live-cert" / f"two-turn-{uuid4().hex}"
    home, storage = root / "home", root / "storage"
    home.mkdir(parents=True); storage.mkdir(parents=True)
    gmail_home = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "GmailCourier"
    (home / "agentrelay.toml").write_text(f'''[project]
project_id = "control-plane-cert"
display_name = "Control Plane Certification"
channel_id = "AR-CONTROL-PLANE-CERT"
repo_path = "{MECHANICS.as_posix()}"
local_project_storage = "{storage.as_posix()}"
target_type = "codex-cli"
target_id = ""
target_label = "bounded control-plane cert"
chat_url = "{CHAT_URL}"
poll_interval = 10
enabled = true
gmail_auth_home = "{gmail_home.as_posix()}"
codex_command = "codex"
handoff_command = ""
''', encoding="utf-8")
    nonce = f"CONTROL-TURN-A-{uuid4()}"
    body = ("AGENTRELAY/1\n\nCHANNEL: AR-CONTROL-PLANE-CERT\n" +
            f"RUN: {run_id}\nSTEP: 0001\nPARENT: 0000\nDISPOSITION: WAKE\nPROJECT: control-plane-cert\n\n" +
            f"Inspect this Mechanics repository. Do not modify source, do not run tests, do not commit, and do not push. Return the exact cwd, branch, HEAD, clean/dirty status, and nonce {nonce}. This staged task contains no ChatGPT, Gmail, handoff, watchdog, or continuation instructions.")
    from agent_relay.gmail import GmailMessage
    from agent_relay.protocol import parse_envelope
    from agent_relay.storage import stage_instruction
    staged = stage_instruction(storage, GmailMessage("control-turn-a", "local", None, body, ()), parse_envelope(body))
    env = os.environ.copy(); env["AGENT_RELAY_HOME"] = str(home)
    launch = subprocess.run([sys.executable, "-m", "agent_relay.cli", "run-agent", "--staged", str(staged), "--run", run_id, "--step", "1", "--parent", "0"], cwd=ROOT, env=env, capture_output=True, text=True, check=False)
    print(f"RUN_ID={run_id}", flush=True)
    print(f"LIVE_ROOT={root}", flush=True)
    if launch.returncode:
        print("TURN_1_LAUNCH_FAILED", launch.stdout, launch.stderr, flush=True); return 1
    info = json.loads(launch.stdout); worker_id = info["worker"]["worker_id"]
    obligation_path = storage / "handoff_obligations" / f"{worker_id}.json"
    deadline = time.monotonic() + 360
    while time.monotonic() < deadline:
        if obligation_path.exists():
            value = json.loads(obligation_path.read_text(encoding="utf-8"))
            print(f"TURN_1_OBLIGATION={value.get('state')}", flush=True)
            if value.get("state") == "VERIFIED":
                break
        time.sleep(2)
    else:
        print("TURN_1_HANDOFF_NOT_VERIFIED", flush=True); return 2
    watchdog = storage / "watchdogs" / f"{run_id}-after-0001.json"
    deadline = time.monotonic() + 330
    while time.monotonic() < deadline:
        if watchdog.exists():
            status = json.loads(watchdog.read_text(encoding="utf-8"))
            print(f"WATCHDOG_STATUS={status.get('status')} POLL={status.get('poll_number')} REMAINING={status.get('service_window_remaining_seconds')}", flush=True)
            if status.get("status") in {"NO_WAKE_FOUND", "FINISHED", "FAILED", "STOPPED"}:
                print("TURN_2_NOT_OBSERVED", flush=True); return 3
            if status.get("status") in {"GMAIL_WAKE_FOUND", "WORKER_PROCESS_CREATED", "WORKER_CLAIMED", "CODEX_STARTING", "CODEX_RUNNING", "WAKE_CHAIN_CONFIRMED"}:
                print("TURN_2_WAKE_OBSERVED", flush=True); return 0
        time.sleep(10)
    print("TURN_2_WINDOW_EXPIRED", flush=True); return 4


if __name__ == "__main__":
    raise SystemExit(main())
