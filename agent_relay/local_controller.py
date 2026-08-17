"""Durable transport for a real local Codex Controller (A) and Worker (B).

Python owns only the handoff protocol: claims, hashes, exact ACK/liveness and
the three allowed controller enums.  Codex owns both the next-task decision and
the bounded repository work; neither task nor result bodies are command-line
arguments.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

from .storage import atomic_json, now

ACK_TIMEOUT = 30.0
CODEX_TIMEOUT = 600.0
DECISIONS = {"CONTINUE", "COMPLETE", "HUMAN_REQUIRED"}
# STEP-0009's amendment requires that both real roles select Luna High
# explicitly.  Keep this immutable in the production launch path: a machine
# default (currently Terra on this workstation) is not acceptable evidence.
LUNA_MODEL = "gpt-5.6-luna"
LUNA_REASONING_EFFORT = "high"


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _event(root: Path, event: str, **fields: object) -> None:
    with (root / "events.jsonl").open("a", encoding="utf-8") as output:
        output.write(json.dumps({"at": now(), "event": event, **fields}, sort_keys=True) + "\n")


def _paths(root: Path) -> None:
    for name in ("acks", "verified", "live", "release", "owners", "tasks", "results", "claims", "terminal", "agent_instructions", "agent_logs"):
        (root / name).mkdir(parents=True, exist_ok=True)


def _handoff_id(role: str, turn: int) -> str:
    return f"{turn:04d}-{role}-{uuid4().hex}"


def _ack(root: Path, role: str, handoff: str, turn: int) -> None:
    value = {"role": role, "handoff": handoff, "turn": turn, "pid": os.getpid(), "at": now()}
    atomic_json(root / "acks" / f"{handoff}.{role}.json", value)
    atomic_json(root / "owners" / f"{handoff}.{role}.json", {**value, "state": "STARTED"})
    _event(root, "startup_ack", role=role, handoff=handoff, turn=turn, pid=os.getpid())


def _spawn(root: Path, role: str, turn: int, handoff: str) -> subprocess.Popen:
    command = [sys.executable, "-m", "agent_relay.local_controller", "--root", str(root), "--role", role, "--turn", str(turn), "--handoff", handoff]
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    child = subprocess.Popen(command, cwd=Path(__file__).resolve().parents[1], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags)
    _event(root, "peer_spawned", role=role, handoff=handoff, turn=turn, pid=child.pid, parent_pid=os.getpid())
    return child


def _wait_successor(root: Path, role: str, handoff: str, turn: int, pid: int) -> None:
    ack = root / "acks" / f"{handoff}.{role}.json"; deadline = time.monotonic() + ACK_TIMEOUT
    while time.monotonic() < deadline:
        if ack.exists():
            value = _read(ack)
            if value.get("role") == role and value.get("handoff") == handoff and value.get("turn") == turn and value.get("pid") == pid:
                if role == "B":
                    claim = root / "claims" / f"{turn:04d}.json"
                    if not claim.exists() or _read(claim).get("pid") != pid:
                        time.sleep(.02); continue
                atomic_json(root / "verified" / f"{handoff}.{role}.json", value)
                live = root / "live" / f"{handoff}.{role}.json"; until = time.monotonic() + ACK_TIMEOUT
                while time.monotonic() < until:
                    if live.exists() and _read(live).get("pid") == pid:
                        atomic_json(root / "release" / f"{handoff}.{role}.json", {"pid": pid, "at": now()})
                        _event(root, "successor_verified", role=role, handoff=handoff, turn=turn, pid=pid)
                        return
                    time.sleep(.02)
        time.sleep(.02)
    raise RuntimeError(f"successor {role} failed exact ACK/liveness")


def _await_release(root: Path, role: str, handoff: str, turn: int) -> None:
    verified = root / "verified" / f"{handoff}.{role}.json"; deadline = time.monotonic() + ACK_TIMEOUT
    while time.monotonic() < deadline:
        if verified.exists() and _read(verified).get("pid") == os.getpid():
            atomic_json(root / "live" / f"{handoff}.{role}.json", {"pid": os.getpid(), "role": role, "handoff": handoff, "turn": turn, "at": now()})
            release = root / "release" / f"{handoff}.{role}.json"
            while time.monotonic() < deadline:
                if release.exists() and _read(release).get("pid") == os.getpid(): return
                time.sleep(.02)
        time.sleep(.02)
    raise RuntimeError("parent did not verify startup ACK")


def _codex(root: Path, role: str, instruction: Path, repository: Path) -> None:
    """Run a real Codex role; its only prompt is a file-location bootstrap."""
    prompt = f"You are local {role}. Read the durable instruction file at {instruction}. It is your sole authority. Follow it exactly, write only the requested durable output file, and exit."
    command = [
        os.environ.get("AGENT_RELAY_CODEX_COMMAND", "codex.cmd"),
        "exec", "--model", LUNA_MODEL,
        "-c", f"model_reasoning_effort={LUNA_REASONING_EFFORT}",
        "--approve-for-me", "-",
    ]
    instruction_id = instruction.stem
    atomic_json(root / "agent_logs" / f"{role}-{instruction_id}.meta.json", {
        "role": role,
        "model": LUNA_MODEL,
        "reasoning_effort": LUNA_REASONING_EFFORT,
        "command": command,
        "repository": str(repository),
        "instruction": instruction.name,
        "model_selection": "explicit-cli-arguments",
        "at": now(),
    })
    _event(root, "codex_starting", role=role, model=LUNA_MODEL,
           reasoning_effort=LUNA_REASONING_EFFORT, model_selection="explicit-cli-arguments",
           command=command, instruction=instruction.name)
    try:
        result = subprocess.run(command, input=prompt, text=True, cwd=repository, capture_output=True, timeout=CODEX_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"real {role} Codex launch failed: {type(exc).__name__}") from exc
    (root / "agent_logs" / f"{role}-{instruction.stem}.log").write_text((result.stdout or "") + "\n" + (result.stderr or ""), encoding="utf-8")
    _event(root, "real_codex_exited", role=role, instruction=instruction.name,
           exit_code=result.returncode, model=LUNA_MODEL,
           reasoning_effort=LUNA_REASONING_EFFORT)
    if result.returncode:
        raise RuntimeError(f"real {role} Codex exited {result.returncode}")


def _controller_instruction(root: Path, run: dict, turn: int, output: Path) -> Path:
    previous = root / "results" / f"{turn:04d}.json"
    text = {
        "role": "Controller Agent A", "objective_file": str(root / "run.json"),
        "previous_worker_result_file": str(previous) if previous.exists() else None,
        "output_file": str(output), "allowed_decisions": sorted(DECISIONS),
        "requirements": ["Read the durable objective and previous result.", "Independently decide one allowed decision.", "For CONTINUE, author one useful, bounded, read-only repository task for the Worker, including a concise task_body and optional failure_injection boolean.", "For COMPLETE or HUMAN_REQUIRED, provide a concise reason.", "Do not modify the repository."],
        "output_schema": {"decision": "CONTINUE|COMPLETE|HUMAN_REQUIRED", "reason": "string", "task_body": "string required for CONTINUE", "failure_injection": "boolean optional"},
    }
    path = root / "agent_instructions" / f"controller-{turn:04d}.json"; atomic_json(path, text); return path


def _worker_instruction(root: Path, turn: int, task: Path, output: Path) -> Path:
    text = {"role": "Worker Agent B", "task_file": str(task / "task.json"), "output_file": str(output),
            "requirements": ["Read only the durable task file for task authority.", "Perform its bounded read-only repository inspection.", "Do not modify, commit, push, or run tests.", "If failure_injection is true, return status FAILED with a concise failure field instead of doing work."],
            "output_schema": {"status": "OK|FAILED", "summary": "string", "evidence": "object optional", "failure": "string required when FAILED"}}
    path = root / "agent_instructions" / f"worker-{turn:04d}.json"; atomic_json(path, text); return path


def _write_task(root: Path, run: dict, turn: int, decision: dict) -> None:
    directory = root / "tasks" / f"{turn:04d}"; directory.mkdir(parents=True, exist_ok=False)
    body = str(decision["task_body"]).encode("utf-8")
    payload = json.dumps({"run_id": run["run_id"], "turn": turn, "repository": run["repository"], "task_body": body.decode("utf-8"), "failure_injection": bool(decision.get("failure_injection", False))}, sort_keys=True).encode("utf-8")
    (directory / "task.json").write_bytes(payload)
    atomic_json(directory / "manifest.json", {"run_id": run["run_id"], "turn": turn, "payload_sha256": _hash(payload), "body_sha256": _hash(body), "created_by": "real-Codex-A", "at": now()})
    _event(root, "task_written", role="A", turn=turn, task_sha256=_hash(payload), injected=bool(decision.get("failure_injection", False)))


def _decide(root: Path, run: dict, turn: int) -> tuple[str, dict]:
    output = root / "agent_instructions" / f"controller-output-{turn:04d}.json"
    _codex(root, "Controller Agent A", _controller_instruction(root, run, turn, output), Path(run["repository"]))
    value = _read(output); decision = value.get("decision")
    if decision not in DECISIONS: raise RuntimeError("Controller Codex emitted invalid decision")
    if decision == "CONTINUE" and (not isinstance(value.get("task_body"), str) or not value["task_body"].strip()):
        raise RuntimeError("Controller CONTINUE omitted task body")
    return decision, value


def controller(root: Path, turn: int, handoff: str) -> None:
    _ack(root, "A", handoff, turn)
    if handoff != "initial-A": _await_release(root, "A", handoff, turn)
    run_path = root / "run.json"; run = _read(run_path)
    decision, value = _decide(root, run, turn)
    run["decisions"].append({"turn": turn, "decision": decision, "reason": value.get("reason", ""), "at": now()}); run["status"] = decision; atomic_json(run_path, run)
    _event(root, "controller_decision", role="A", turn=turn, decision=decision, reason=value.get("reason", ""))
    if decision != "CONTINUE":
        atomic_json(root / "terminal" / "result.json", {"run_id": run["run_id"], "decision": decision, "reason": value.get("reason", ""), "at": now()}); return
    next_turn = turn + 1 if (root / "results" / f"{turn:04d}.json").exists() else turn
    _write_task(root, run, next_turn, value)
    handoff_id = _handoff_id("B", next_turn); child = _spawn(root, "B", next_turn, handoff_id)
    _wait_successor(root, "B", handoff_id, next_turn, child.pid); _event(root, "parent_exit_after_ack", role="A", turn=next_turn, child_pid=child.pid)


def worker(root: Path, turn: int, handoff: str) -> None:
    task = root / "tasks" / f"{turn:04d}"; manifest = _read(task / "manifest.json"); payload = (task / "task.json").read_bytes(); request = json.loads(payload)
    if _hash(payload) != manifest.get("payload_sha256") or request.get("turn") != turn: raise RuntimeError("task hash/turn mismatch")
    claim = root / "claims" / f"{turn:04d}.json"
    try: fd = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc: raise RuntimeError("duplicate task claim") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as output: json.dump({"turn": turn, "pid": os.getpid(), "role": "B", "handoff": handoff}, output)
    _event(root, "task_claimed", role="B", turn=turn, pid=os.getpid()); _ack(root, "B", handoff, turn); _await_release(root, "B", handoff, turn)
    output = root / "agent_instructions" / f"worker-output-{turn:04d}.json"
    try:
        _codex(root, "Worker Agent B", _worker_instruction(root, turn, task, output), Path(request["repository"]))
        result = _read(output)
        if result.get("status") not in {"OK", "FAILED"}: raise RuntimeError("Worker Codex emitted invalid status")
    except Exception as exc:
        result = {"status": "FAILED", "failure": f"{type(exc).__name__}: {exc}", "summary": "Worker bootstrap failure"}
    outcome = {"run_id": request["run_id"], "turn": turn, "status": result["status"], "summary": str(result.get("summary", "")), "evidence": result.get("evidence", {}), "failure": result.get("failure"), "payload_sha256": _hash(payload), "worker_pid": os.getpid()}
    atomic_json(root / "results" / f"{turn:04d}.json", outcome); _event(root, "result_written", role="B", turn=turn, status=outcome["status"])
    handoff_id = _handoff_id("A", turn); child = _spawn(root, "A", turn, handoff_id)
    _wait_successor(root, "A", handoff_id, turn, child.pid); _event(root, "parent_exit_after_ack", role="B", turn=turn, child_pid=child.pid)


def initialize(root: Path, repository: Path, run_id: str, *, objective: str) -> None:
    _paths(root)
    atomic_json(root / "run.json", {"run_id": run_id, "repository": str(repository.resolve()), "objective": objective, "decisions": [], "status": "CONTINUE"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--root", required=True); parser.add_argument("--role", choices=("A", "B"), required=True); parser.add_argument("--turn", type=int, required=True); parser.add_argument("--handoff", required=True)
    args = parser.parse_args(argv); root = Path(args.root).resolve(); _paths(root)
    try: (controller if args.role == "A" else worker)(root, args.turn, args.handoff)
    except Exception as exc: _event(root, "failed", role=args.role, turn=args.turn, error=type(exc).__name__, detail=str(exc)[:200]); return 1
    return 0


if __name__ == "__main__": raise SystemExit(main())
