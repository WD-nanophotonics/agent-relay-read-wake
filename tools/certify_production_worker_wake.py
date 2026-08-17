"""Bounded real production-Worker wake certification against Mechanics."""
from __future__ import annotations

from datetime import datetime, UTC
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_relay.config import EXPECTED_CHAT_URL
from agent_relay.gmail import GmailMessage
from agent_relay.protocol import parse_envelope
from agent_relay.storage import StateStore, read_content_hash, stage_instruction
from agent_relay.worker import ProcessWorkerLauncher

AGENTRELAY_REPO = Path(__file__).resolve().parents[1]
MECHANICS_REPO = Path(r"C:\Users\icywo\Documents\ChatGPT\test mechanics sim")
REAL_STORAGE = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "AgentRelay" / "projects" / "gmail-courier"
DIAGNOSTIC_ROOT = Path(os.environ.get("LOCALAPPDATA", str(Path.cwd()))) / "AgentRelay" / "diagnostics" / "production-worker-wake"


def stamp() -> str:
    return datetime.now(UTC).isoformat()


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {(result.stderr or result.stdout).strip()}")
    return (result.stdout or "").strip()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")


def wait_process(pid: int, *, timeout: float, sample) -> int | None:
    if os.name == "nt":
        import ctypes
        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x00100000 | 0x00000400, False, pid)
        if handle:
            try:
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    sample()
                    if kernel.WaitForSingleObject(handle, 250) == 0:
                        code = ctypes.c_ulong()
                        kernel.GetExitCodeProcess(handle, ctypes.byref(code))
                        return int(code.value)
                return None
            finally:
                kernel.CloseHandle(handle)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sample()
        try:
            os.kill(pid, 0)
        except OSError:
            return None
    return None


def task_body(nonce: str, proof: Path) -> str:
    return f"""Read this exact staged production-worker wake task.

Do NOT modify Mechanics source code. Do NOT run tests. Do NOT commit or push.
Observe the actual cwd, current git branch, and HEAD. Write exactly one JSON file at {proof} with fields:
{{
  \"probe_id\": \"{nonce}\",
  \"mechanics_repo\": \"{MECHANICS_REPO}\",
  \"cwd\": <actual cwd>,
  \"git_branch\": <actual branch>,
  \"git_head\": <actual HEAD>,
  \"agent_message\": \"PRODUCTION_WORKER_AGENT_AWAKE\"
}}
Then print exactly:
PRODUCTION_WORKER_WAKE_COMPLETE {nonce}
Then exit normally.
"""


def config_text(storage: Path) -> str:
    return """[project]
project_id = "production-worker-wake-probe"
display_name = "Production Worker Wake Probe"
channel_id = "AR-PRODUCTION-WORKER-WAKE-PROBE"
repo_path = "C:/Users/icywo/Documents/ChatGPT/test mechanics sim"
local_project_storage = "{storage}"
target_type = "codex-cli"
target_id = ""
target_label = "bounded production worker probe"
chat_url = "{chat_url}"
poll_interval = 20
enabled = true
gmail_auth_home = "{gmail_home}"
codex_command = "codex.cmd"
handoff_command = ""
""".format(storage=storage.as_posix(), chat_url=EXPECTED_CHAT_URL, gmail_home=(storage / "gmail").as_posix())


def run_once(run_number: int, diagnostics: Path) -> dict:
    nonce = f"AR-PRODUCTION-WORKER-WAKE-{uuid4()}"
    run_id = f"RUN-PRODWORKER-{uuid4().hex.upper()}"
    artifact_root = MECHANICS_REPO / ".agentrelay-worker-wake-probe"
    if artifact_root.exists():
        shutil.rmtree(artifact_root)
    run_root = artifact_root / nonce
    storage = run_root / "relay-storage"
    proof = run_root / "wake-proof.json"
    run_root.mkdir(parents=True)
    message_id = f"diagnostic-{nonce}"
    body = f"AGENTRELAY/1\n\nCHANNEL: AR-PRODUCTION-WORKER-WAKE-PROBE\nRUN: {run_id}\nSTEP: 0001\nPARENT: 0000\nDISPOSITION: WAKE\nPROJECT: production-worker-wake-probe\n\n" + task_body(nonce, proof)
    message = GmailMessage(message_id, "diagnostic", None, body, ())
    envelope = parse_envelope(body)
    staged = stage_instruction(storage, message, envelope)
    config_home = run_root / "config-home"
    config_home.mkdir(parents=True)
    (config_home / "agentrelay.toml").write_text(config_text(storage), encoding="utf-8")
    state_store = StateStore(storage)
    real_state_path = REAL_STORAGE / "state.json"
    real_hash_before = hashlib.sha256(real_state_path.read_bytes()).hexdigest() if real_state_path.exists() else None
    before = {"controller_started": stamp(), "controller_pid": os.getpid(), "run": run_id, "probe_id": nonce, "worker_id": None, "agentrelay_head": git(AGENTRELAY_REPO, "rev-parse", "HEAD"), "mechanics_head": git(MECHANICS_REPO, "rev-parse", "HEAD"), "mechanics_branch": git(MECHANICS_REPO, "branch", "--show-current"), "mechanics_status": git(MECHANICS_REPO, "status", "--short"), "mechanics_repo": str(MECHANICS_REPO), "child_cwd": str(AGENTRELAY_REPO), "worker_config_home": str(config_home), "worker_storage": str(storage), "staged_path": str(staged), "proof_path": str(proof), "real_state_hash_before": real_hash_before}
    run_diag = diagnostics / f"run-{run_number}-{nonce}"
    run_diag.mkdir(parents=True, exist_ok=True)
    write_json(run_diag / "before.json", before)
    log_path = run_diag / "worker-and-codex-output.log"
    launcher = ProcessWorkerLauncher()
    old_env = {key: os.environ.get(key) for key in ("AGENT_RELAY_HOME", "AGENT_RELAY_DIAGNOSTIC_POST_EXIT_SINK")}
    os.environ["AGENT_RELAY_HOME"] = str(config_home)
    os.environ["AGENT_RELAY_DIAGNOSTIC_POST_EXIT_SINK"] = "1"
    worker_pid = None
    launch_error = None
    state_samples: list[dict] = []
    saved_out, saved_err = os.dup(1), os.dup(2)
    log_handle = log_path.open("w", encoding="utf-8", buffering=1)
    try:
        os.dup2(log_handle.fileno(), 1); os.dup2(log_handle.fileno(), 2)
        try:
            result = launcher.launch(staged_path=staged, envelope=envelope, content_hash=read_content_hash(staged), message_id=message_id)
            worker_pid = int(result["pid"])
            before.update({"process_worker_launch_requested": stamp(), "worker_process_created": stamp(), "worker_id": result["worker_id"], "worker_pid": worker_pid, "worker_exe": result.get("exe"), "worker_launch_result": result})
            pending = dict(result); pending.update({"message_id": message_id, "content_hash": read_content_hash(staged), "staged_path": str(staged)})
            state = state_store.load(); state["pending_worker"] = pending; state_store.save(state)
            before["pending_worker_written"] = stamp(); write_json(run_diag / "launch-and-pending.json", before)
        except Exception as exc:
            launch_error = f"{type(exc).__name__}: {exc}"
    finally:
        os.dup2(saved_out, 1); os.dup2(saved_err, 2); os.close(saved_out); os.close(saved_err); log_handle.close()
        for key, value in old_env.items():
            if value is None: os.environ.pop(key, None)
            else: os.environ[key] = value

    def sample() -> None:
        item = {"timestamp": stamp()}
        if worker_pid:
            try:
                import ctypes
                handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, worker_pid) if os.name == "nt" else None
                item["worker_live"] = bool(handle)
                if handle: ctypes.windll.kernel32.CloseHandle(handle)
            except (AttributeError, OSError):
                item["worker_live"] = False
        try:
            current = state_store.load(); item["state"] = current
            active = current.get("active_worker") or {}; item["codex_pid"] = active.get("codex_pid"); item["codex_status"] = active.get("codex_status")
        except Exception as exc:
            item["state_error"] = f"{type(exc).__name__}: {exc}"
        state_samples.append(item)

    worker_exit_code = None if launch_error else wait_process(worker_pid, timeout=1200, sample=sample)
    after_state = state_store.load()
    ledger_path = storage / "ledger" / "events.jsonl"
    ledger_text = ledger_path.read_text(encoding="utf-8") if ledger_path.exists() else ""
    codex_diagnostics = [json.loads(path.read_text(encoding="utf-8")) for path in (storage / "diagnostics").glob("codex-*.json")] if (storage / "diagnostics").is_dir() else []
    codex_started = [json.loads(line) for line in ledger_text.splitlines() if '"event": "codex_started"' in line]
    codex_exited = [json.loads(line) for line in ledger_text.splitlines() if '"event": "codex_exited"' in line]
    proof_value = json.loads(proof.read_text(encoding="utf-8")) if proof.exists() else None
    output = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    output += "\n" + "\n".join(f"{item.get('stdout', '')}\n{item.get('stderr', '')}" for item in codex_diagnostics)
    classification, detail = ("PROCESS_WORKER_CREATE_FAILED", launch_error) if launch_error else ("WORKER_CHILD_CREATED_BUT_NOT_CLAIMED", "worker did not claim")
    if not launch_error and '"event": "worker_claimed"' in ledger_text:
        classification, detail = "WORKER_CLAIMED_BUT_CODEX_NOT_STARTED", "worker claim observed but no codex_started"
        if codex_started: classification, detail = "CODEX_STARTED_NO_TASK_EXECUTION", "codex started but proof/marker validation failed"
        if proof_value and codex_started: classification, detail = "CODEX_TASK_CONTEXT_WRONG", "proof exists but context or marker validation failed"
        if (proof_value and proof_value.get("probe_id") == nonce and proof_value.get("mechanics_repo") == str(MECHANICS_REPO) and proof_value.get("cwd") == str(MECHANICS_REPO) and proof_value.get("git_branch") == "sandbox" and proof_value.get("git_head") == before["mechanics_head"] and proof_value.get("agent_message") == "PRODUCTION_WORKER_AGENT_AWAKE" and f"PRODUCTION_WORKER_WAKE_COMPLETE {nonce}" in output and codex_exited and codex_exited[-1].get("exit_code") == 0 and worker_exit_code == 0 and after_state.get("active_worker") is None):
            classification, detail = "CODEX_TASK_EXECUTED", "claim, Codex proof, exit, completion, and cleanup verified"
    real_hash_after = hashlib.sha256(real_state_path.read_bytes()).hexdigest() if real_state_path.exists() else None
    after = {"worker_exit_code": worker_exit_code, "codex_started": codex_started, "codex_exited": codex_exited, "classification": classification, "detail": detail, "mechanics_status_before_cleanup": git(MECHANICS_REPO, "status", "--short"), "real_state_hash_after": real_hash_after, "proof": proof_value}
    write_json(run_diag / "after.json", after); write_json(run_diag / "state-samples.json", state_samples); write_json(run_diag / "codex-diagnostics.json", codex_diagnostics); (run_diag / "ledger.jsonl").write_text(ledger_text, encoding="utf-8")
    try: shutil.rmtree(artifact_root)
    except OSError as exc: after["cleanup_error"] = f"{type(exc).__name__}: {exc}"
    mechanics_status = git(MECHANICS_REPO, "status", "--short")
    after["mechanics_status_after_cleanup"] = mechanics_status
    write_json(run_diag / "after.json", after)
    return {"run": run_number, "nonce": nonce, "worker_id": before.get("worker_id"), "worker_pid": worker_pid, "worker_exit_code": worker_exit_code, "codex_pid": codex_started[-1].get("codex_pid") if codex_started else None, "codex_exit_code": codex_exited[-1].get("exit_code") if codex_exited else None, "classification": classification, "diagnostics": str(run_diag), "proof": proof_value, "real_state_unchanged": real_hash_before == real_hash_after, "mechanics_status": mechanics_status}


def main() -> int:
    if not MECHANICS_REPO.is_dir(): raise SystemExit(f"missing Mechanics repo: {MECHANICS_REPO}")
    DIAGNOSTIC_ROOT.mkdir(parents=True, exist_ok=True)
    session = DIAGNOSTIC_ROOT / f"session-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid4()}"; session.mkdir(parents=True)
    results = [run_once(index, session) for index in range(1, 4)]
    for result in results:
        print(json.dumps(result, sort_keys=True))
        if result["classification"] == "CODEX_TASK_EXECUTED": print(f"PRODUCTION_WORKER_WAKE_RUN_{result['run']}_PASS")
    all_pass = all(item["classification"] == "CODEX_TASK_EXECUTED" and item["real_state_unchanged"] and not item["mechanics_status"] for item in results)
    print("PRODUCTION_WORKER_WAKE_3X_PASS" if all_pass else "PRODUCTION_WORKER_WAKE_NOT_PROVEN")
    return 0 if all_pass else 1


if __name__ == "__main__": raise SystemExit(main())
