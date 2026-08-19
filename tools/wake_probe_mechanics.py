"""Bounded real-Codex wake certification against the Mechanics guinea-pig repo.

This deliberately bypasses Gmail, ChatGPT handoff, and watchdogs.  It uses the
same staged-file/stdin Codex launch form as the production Worker and proves
success only from the nonce proof file plus the Codex stdout marker.
"""
from __future__ import annotations

from datetime import datetime, UTC
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from uuid import uuid4


MECHANICS_REPO = Path(os.environ.get("RELAY_CERT_REPOSITORY", Path.cwd()))
EXPECTED_BRANCH = "sandbox"
AGENTRELAY_REPO = Path(__file__).resolve().parents[1]
DIAGNOSTIC_ROOT = Path(os.environ.get("LOCALAPPDATA", str(Path.cwd()))) / "AgentRelay" / "diagnostics" / "mechanics-wake-probe"


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


def task_text(*, nonce: str, proof_path: Path) -> str:
    return f"""Read this exact wake-probe task and execute it now.

Do NOT modify Mechanics source code. Do NOT run tests.

Write exactly one JSON proof file at:
{proof_path}

The JSON must contain exactly these fields and values:
{{
  \"probe_id\": \"{nonce}\",
  \"mechanics_repo\": \"{MECHANICS_REPO}\",
  \"cwd\": <the actual cwd observed by you>,
  \"git_branch\": <the branch observed by you>,
  \"git_head\": <the HEAD observed by you>,
  \"agent_message\": \"MECHANICS_AGENT_AWAKE\"
}}

Use the current working directory and git commands to observe cwd, branch, and HEAD.
Then print exactly:
MECHANICS_AGENT_WAKE_PROBE_COMPLETE {nonce}
Then exit.
"""


def run_probe(*, run_number: int, codex_exe: str, diagnostics: Path) -> dict:
    nonce = f"AR-WAKE-{uuid4()}"
    artifact_root = MECHANICS_REPO / ".agentrelay-wake-probe"
    run_root = artifact_root / nonce
    proof_path = run_root / "wake-proof.json"
    staged_path = run_root / "staged-task.txt"
    if artifact_root.exists():
        shutil.rmtree(artifact_root)
    run_root.mkdir(parents=True, exist_ok=False)
    staged_path.write_text(task_text(nonce=nonce, proof_path=proof_path), encoding="utf-8")
    if proof_path.exists():
        raise RuntimeError("probe proof existed before launch")

    before = {
        "timestamp": stamp(),
        "controller_pid": os.getpid(),
        "agentrelay_head": git(AGENTRELAY_REPO, "rev-parse", "HEAD"),
        "mechanics_head": git(MECHANICS_REPO, "rev-parse", "HEAD"),
        "mechanics_branch": git(MECHANICS_REPO, "branch", "--show-current"),
        "mechanics_status": git(MECHANICS_REPO, "status", "--short"),
        "probe_id": nonce,
        "codex_exe": str(Path(codex_exe).resolve()),
        "argv": [str(Path(codex_exe).resolve()), "exec", "--approve-for-me", "-"],
        "cwd": str(MECHANICS_REPO),
        "staged_task": str(staged_path),
        "proof_path": str(proof_path),
        "stdin_mode": "codex exec - with bounded bootstrap on stdin",
    }
    run_diag = diagnostics / f"run-{run_number}-{nonce}"
    run_diag.mkdir(parents=True, exist_ok=True)
    write_json(run_diag / "before.json", before)

    bootstrap = f"Read and execute the authoritative staged task at {staged_path}. Treat that file as the sole task authority. Do not add or infer other work."
    started = stamp()
    samples: list[dict] = []
    proof_seen_at: str | None = None
    process = None
    monitor_stop = threading.Event()

    def monitor() -> None:
        nonlocal proof_seen_at
        while not monitor_stop.is_set():
            alive = process is not None and process.poll() is None
            sample = {"timestamp": stamp(), "alive": alive}
            if proof_path.exists() and proof_seen_at is None:
                proof_seen_at = stamp()
                sample["proof_seen"] = True
            samples.append(sample)
            monitor_stop.wait(0.5)

    try:
        process = subprocess.Popen(
            before["argv"], cwd=MECHANICS_REPO, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            close_fds=True,
        )
        created = stamp()
        before["popen_returned"] = True
        before["codex_pid"] = process.pid
        before["codex_create_time"] = created
        write_json(run_diag / "launch.json", before)
        thread = threading.Thread(target=monitor, name=f"mechanics-wake-probe-{run_number}", daemon=True)
        thread.start()
        try:
            stdout, stderr = process.communicate(bootstrap, timeout=900)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            classification = "CODEX_PROCESS_ALIVE_NO_TASK_EXECUTION"
            detail = "Codex exceeded bounded 900-second probe timeout"
        else:
            classification = None
            detail = ""
        exit_time = stamp()
    except OSError as exc:
        created = None
        exit_time = stamp()
        stdout, stderr = "", f"{type(exc).__name__}: {exc}"
        classification = "CODEX_PROCESS_CREATE_FAILED"
        detail = str(exc)
    finally:
        monitor_stop.set()
        if "thread" in locals():
            thread.join(timeout=2)

    if process is not None and classification is None:
        proof_seen_at = proof_seen_at or (stamp() if proof_path.exists() else None)
        classification = "CODEX_PROCESS_CREATED_BUT_DIED"
        detail = f"exit_code={process.returncode}"
        if proof_path.exists():
            try:
                proof = json.loads(proof_path.read_text(encoding="utf-8"))
                if (
                    proof.get("probe_id") == nonce
                    and proof.get("mechanics_repo") == str(MECHANICS_REPO)
                    and proof.get("git_branch") == EXPECTED_BRANCH
                    and proof.get("git_head") == before["mechanics_head"]
                    and proof.get("agent_message") == "MECHANICS_AGENT_AWAKE"
                    and f"MECHANICS_AGENT_WAKE_PROBE_COMPLETE {nonce}" in stdout
                    and proof_seen_at is not None
                    and process.returncode == 0
                ):
                    classification = "CODEX_TASK_EXECUTED"
                    detail = "nonce proof, context, stdout marker, and normal exit verified"
            except (OSError, json.JSONDecodeError) as exc:
                detail = f"proof parse failed: {type(exc).__name__}: {exc}"
        elif process.returncode is None:
            classification = "CODEX_PROCESS_ALIVE_NO_TASK_EXECUTION"

    (run_diag / "stdout.txt").write_text(stdout or "", encoding="utf-8")
    (run_diag / "stderr.txt").write_text(stderr or "", encoding="utf-8")
    write_json(run_diag / "alive-samples.json", samples)
    if proof_path.exists():
        shutil.copy2(proof_path, run_diag / "wake-proof.json")
    after = {
        "timestamp": stamp(),
        "codex_pid": process.pid if process is not None else None,
        "codex_exit_time": exit_time,
        "codex_exit_code": process.returncode if process is not None else None,
        "proof_seen_at": proof_seen_at,
        "classification": classification,
        "detail": detail,
        "mechanics_head": git(MECHANICS_REPO, "rev-parse", "HEAD"),
        "mechanics_branch": git(MECHANICS_REPO, "branch", "--show-current"),
        "mechanics_status": git(MECHANICS_REPO, "status", "--short"),
    }
    write_json(run_diag / "after.json", after)
    proof_value = json.loads((run_diag / "wake-proof.json").read_text(encoding="utf-8")) if (run_diag / "wake-proof.json").exists() else None
    try:
        if artifact_root.exists():
            shutil.rmtree(artifact_root)
    except OSError as exc:
        after["cleanup_error"] = f"{type(exc).__name__}: {exc}"
    return {"run": run_number, "nonce": nonce, "pid": after["codex_pid"], "exit_code": after["codex_exit_code"], "classification": classification, "proof": proof_value, "diagnostics": str(run_diag), "stdout": stdout or "", "stderr": stderr or "", "before": before, "after": after}


def main() -> int:
    if not MECHANICS_REPO.is_dir():
        raise SystemExit(f"missing mechanics repo: {MECHANICS_REPO}")
    codex_exe = shutil.which("codex") or shutil.which("codex.cmd")
    if not codex_exe:
        raise SystemExit("Codex executable not found")
    DIAGNOSTIC_ROOT.mkdir(parents=True, exist_ok=True)
    version = subprocess.run([codex_exe, "--version"], capture_output=True, text=True, timeout=30, check=False)
    help_result = subprocess.run([codex_exe, "exec", "--help"], capture_output=True, text=True, timeout=30, check=False)
    session = DIAGNOSTIC_ROOT / f"session-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid4()}"
    session.mkdir(parents=True, exist_ok=True)
    (session / "codex-version.txt").write_text((version.stdout or "") + (version.stderr or ""), encoding="utf-8")
    (session / "codex-exec-help.txt").write_text((help_result.stdout or "") + (help_result.stderr or ""), encoding="utf-8")
    results = [run_probe(run_number=index, codex_exe=codex_exe, diagnostics=session) for index in range(1, 4)]
    for result in results:
        print(f"RUN_{result['run']}: nonce={result['nonce']} pid={result['pid']} exit={result['exit_code']} classification={result['classification']} diagnostics={result['diagnostics']}")
    mechanics_status = git(MECHANICS_REPO, "status", "--short")
    all_pass = all(result["classification"] == "CODEX_TASK_EXECUTED" for result in results) and not mechanics_status
    for result in results:
        if result["classification"] == "CODEX_TASK_EXECUTED":
            print(f"REAL_MECHANICS_AGENT_WAKE_RUN_{result['run']}_PASS")
    print("REAL_MECHANICS_AGENT_WAKE_3X_PASS" if all_pass else "REAL_MECHANICS_AGENT_WAKE_NOT_PROVEN")
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
