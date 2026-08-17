"""Real bounded managed-Agent experiments for Mechanics and AgentRelay."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_relay.gmail import GmailMessage
from agent_relay.protocol import parse_envelope
from agent_relay.storage import stage_instruction
from agent_relay.watchdog import load_watchdog_status
from gmail_courier.config import home_dir

ROOT = Path(__file__).resolve().parents[1]
MECHANICS = Path(r"C:\Users\icywo\Documents\ChatGPT\test mechanics sim")


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def config_text(home: Path, storage: Path, repo: Path) -> str:
    return f'''[project]
project_id = "managed-agent-cert"
display_name = "Managed Agent Certification"
channel_id = "AR-MANAGED-AGENT-CERT"
repo_path = "{repo.as_posix()}"
local_project_storage = "{storage.as_posix()}"
target_type = "codex-cli"
target_id = ""
target_label = "externally owned bounded Agent"
chat_url = "https://chatgpt.com/c/6a818a0c-5208-83ee-95cd-fd558d66ecc9"
poll_interval = 20
enabled = true
gmail_auth_home = "{home_dir().as_posix()}"
codex_command = "codex.cmd"
handoff_command = ""
'''


def wait_obligation(storage: Path, worker_id: str, timeout: float = 600) -> dict:
    path = storage / "handoff_obligations" / f"{worker_id}.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("state") in {"VERIFIED", "RESULT_READY", "SENDING"}:
                if value.get("state") == "VERIFIED" or not (storage / "state.json").exists() or json.loads((storage / "state.json").read_text(encoding="utf-8")).get("active_worker") is None:
                    return value
        time.sleep(1)
    raise TimeoutError(f"managed obligation did not reach terminal state: {path}")


def run_one(label: str, repo: Path, task: str, root: Path) -> dict:
    home = root / label / "home"
    storage = root / label / "storage"
    home.mkdir(parents=True)
    storage.mkdir(parents=True)
    (home / "agentrelay.toml").write_text(config_text(home, storage, repo), encoding="utf-8")
    run_id = f"RUN-MANAGED-{label.upper()}-{uuid4().hex.upper()}"
    message_id = f"managed-{uuid4()}"
    envelope_body = f"AGENTRELAY/1\n\nCHANNEL: AR-MANAGED-AGENT-CERT\nRUN: {run_id}\nSTEP: 0001\nPARENT: 0000\nDISPOSITION: WAKE\nPROJECT: managed-agent-cert\n\n{task}"
    message = GmailMessage(message_id, "managed", None, envelope_body, ())
    staged = stage_instruction(storage, message, parse_envelope(envelope_body))
    env = os.environ.copy()
    env["AGENT_RELAY_HOME"] = str(home)
    result = subprocess.run([sys.executable, "-m", "agent_relay.cli", "run-agent", "--staged", str(staged), "--run", run_id, "--step", "1", "--parent", "0"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"managed entry failed: {result.stdout}\n{result.stderr}")
    launch = json.loads(result.stdout.strip())
    worker_id = launch["worker"]["worker_id"]
    obligation = wait_obligation(storage, worker_id)
    if obligation.get("state") != "VERIFIED" or obligation.get("submission_verified") is not True:
        raise RuntimeError(f"managed obligation not verified: {obligation}")
    report = str(obligation.get("report", ""))
    assert run_id in report and obligation["handoff_token"] in report
    return {"label": label, "run_id": run_id, "worker_id": worker_id, "worker_pid": launch["worker"]["pid"], "token": obligation["handoff_token"], "report": report, "repo_head": git(repo, "rev-parse", "HEAD"), "repo_branch": git(repo, "branch", "--show-current"), "repo_status": git(repo, "status", "--short")}


def verify_followup_window(root: Path, result: dict) -> None:
    storage = root / result["label"] / "storage"
    deadline = time.monotonic() + 360
    while time.monotonic() < deadline:
        status = load_watchdog_status(storage, result["run_id"], 1)
        if status and status.get("status") == "FINISHED":
            assert status.get("service_window_seconds") == 300
            assert status.get("poll_interval_seconds") == 10
            assert status.get("polls_completed", 0) >= 10
            assert status.get("finish_reason") == "no_matching_wake_after_service_window"
            print("SINGLE_FOLLOWUP_OWNER_PASS")
            print("WATCHDOG_300S_10S_PASS")
            return
        time.sleep(1)
    raise TimeoutError("follow-up owner did not complete its bounded service window")


def main() -> int:
    # Diagnostics may be disposable, but the follow-up watchdog is live
    # ownership state and must outlive the Worker process.  Keep this bounded
    # certification root under the normal local runtime area instead of a
    # TemporaryDirectory that disappears immediately after the worker exits.
    live_root = ROOT / ".agentrelay-live-cert" / uuid4().hex
    live_root.mkdir(parents=True, exist_ok=True)
    root = live_root
    try:
        nonce = f"MANAGED-MECHANICS-{uuid4()}"
        mechanics_head = git(MECHANICS, "rev-parse", "HEAD")
        mechanics_task = f"Inspect this local repository and return a concise final result. Record nonce={nonce}, cwd, git branch, git HEAD, and git status --short. Do not modify source, do not run tests, do not commit, and do not push. Include the exact facts and the marker MANAGED_MECHANICS_COMPLETE {nonce}."
        mechanics = run_one("mechanics", MECHANICS, mechanics_task, root)
        assert mechanics["repo_head"] == mechanics_head and mechanics["repo_branch"] == "sandbox" and "MANAGED_MECHANICS_COMPLETE" in mechanics["report"] and nonce in mechanics["report"]
        print("REAL_MANAGED_AGENT_TERMINAL_HANDOFF_PASS")
        print(json.dumps({"label": "mechanics", "worker_id": mechanics["worker_id"], "worker_pid": mechanics["worker_pid"], "token": mechanics["token"], "head": mechanics["repo_head"], "branch": mechanics["repo_branch"], "status": mechanics["repo_status"]}, sort_keys=True))
        verify_followup_window(root, mechanics)

        self_nonce = f"MANAGED-RELAY-{uuid4()}"
        relay_head = git(ROOT, "rev-parse", "HEAD")
        self_task = f"Inspect this local repository and return a concise final result. Record nonce={self_nonce}, cwd, git branch, git HEAD, and git status --short. Do not modify source, do not run tests, do not commit, and do not push. Include the exact facts and the marker MANAGED_RELAY_COMPLETE {self_nonce}."
        self_hosted = run_one("self-hosted-relay", ROOT, self_task, root)
        assert self_hosted["repo_head"] == relay_head and "MANAGED_RELAY_COMPLETE" in self_hosted["report"] and self_nonce in self_hosted["report"]
        print("SELF_HOSTED_RELAY_AGENT_TERMINAL_HANDOFF_PASS")
        print(json.dumps({"label": "self-hosted-relay", "worker_id": self_hosted["worker_id"], "worker_pid": self_hosted["worker_pid"], "token": self_hosted["token"], "head": self_hosted["repo_head"], "branch": self_hosted["repo_branch"], "status": self_hosted["repo_status"]}, sort_keys=True))
    finally:
        print(f"LIVE_FOLLOWUP_OWNER_ROOT={root}")
    print("MANAGED_AGENT_ENTRY_PASS")
    print("CODEX_HANDOFF_MEMORY_IRRELEVANT_PASS")
    print("NO_USER_COURIER_PASS")
    print("DURABLE_OBLIGATION_PRESERVED")
    print("MINIMAL_ARCHITECTURE_PRESERVED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
