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
RUNTIME_PROTOCOL = "AGENTRELAY_LOCAL_RUNTIME/1"
MAX_JOURNAL_LINES = 2000
MAX_TRACE_LINES = 4000
# STEP-0009's amendment requires that both real roles select Luna High
# explicitly.  Keep this immutable in the production launch path: a machine
# default (currently Terra on this workstation) is not acceptable evidence.
LUNA_MODEL = "gpt-5.6-luna"
LUNA_REASONING_EFFORT = "high"


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _append_jsonl(path: Path, value: dict, *, limit: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(value, sort_keys=True) + "\n")
    if limit is not None and (limit <= 100 or path.stat().st_size > limit * 256):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            if len(lines) > limit:
                path.write_text("\n".join(lines[-limit:]) + "\n", encoding="utf-8")
        except OSError:
            pass


def _event(root: Path, event: str, **fields: object) -> None:
    _append_jsonl(root / "events.jsonl", {"at": now(), "event": event, **fields}, limit=MAX_JOURNAL_LINES)


def _trace(root: Path, event: str, **fields: object) -> None:
    _append_jsonl(root / "trace" / "process.jsonl", {"at": now(), "event": event, **fields}, limit=MAX_TRACE_LINES)


def _incident(root: Path, *, severity: str, actor: str, subject: str, reason: str, evidence: dict, next_boundary: str) -> str:
    incident_id = f"INC-{uuid4().hex}"
    value = {
        "incident_id": incident_id,
        "severity": severity,
        "detecting_actor": actor,
        "subject": subject,
        "reason": reason,
        "observed_evidence": evidence,
        "timestamps": {"detected_at": now()},
        "next_diagnostic_boundary": next_boundary,
    }
    atomic_json(root / "incidents" / f"{incident_id}.json", value)
    _event(root, "incident_opened", incident_id=incident_id, severity=severity, actor=actor, subject=subject, reason=reason)
    _trace(root, "incident_opened", incident_id=incident_id, severity=severity, actor=actor, subject=subject, reason=reason)
    return incident_id


def _runtime_state(root: Path) -> dict:
    path = root / "runtime-state.json"
    if not path.exists():
        return {}
    try:
        return _read(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _save_runtime_state(root: Path, **updates: object) -> dict:
    value = _runtime_state(root)
    value.update(updates)
    value["updated_at"] = now()
    atomic_json(root / "runtime-state.json", value)
    return value


def _paths(root: Path) -> None:
    for name in ("acks", "verified", "live", "release", "owners", "tasks", "results", "claims", "terminal", "agent_instructions", "agent_logs"):
        (root / name).mkdir(parents=True, exist_ok=True)
    for name in ("trace", "incidents"):
        (root / name).mkdir(parents=True, exist_ok=True)


def _handoff_id(role: str, turn: int) -> str:
    return f"{turn:04d}-{role}-{uuid4().hex}"


def _ack(root: Path, role: str, handoff: str, turn: int) -> None:
    value = {"role": role, "handoff": handoff, "turn": turn, "pid": os.getpid(), "at": now()}
    atomic_json(root / "acks" / f"{handoff}.{role}.json", value)
    atomic_json(root / "owners" / f"{handoff}.{role}.json", {**value, "state": "STARTED"})
    _event(root, "startup_ack", role=role, handoff=handoff, turn=turn, pid=os.getpid())
    _trace(root, "startup_ack", role=role, handoff=handoff, turn=turn, pid=os.getpid(), parent_pid=os.getppid())


def _spawn(root: Path, role: str, turn: int, handoff: str) -> subprocess.Popen:
    command = [sys.executable, "-m", "agent_relay.local_controller", "--root", str(root), "--role", role, "--turn", str(turn), "--handoff", handoff]
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    child = subprocess.Popen(command, cwd=Path(__file__).resolve().parents[1], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags)
    _event(root, "peer_spawned", role=role, handoff=handoff, turn=turn, pid=child.pid, parent_pid=os.getpid())
    _trace(root, "peer_spawned", role=role, handoff=handoff, turn=turn, pid=child.pid, parent_pid=os.getpid(), command=command)
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
                        _trace(root, "successor_verified", role=role, handoff=handoff, turn=turn, pid=pid)
                        return
                    time.sleep(.02)
        time.sleep(.02)
    _incident(root, severity="HIGH", actor="control-plane", subject=role, reason="successor_ack_or_liveness_timeout",
              evidence={"role": role, "handoff": handoff, "turn": turn, "expected_pid": pid, "ack_ref": str(ack)},
              next_boundary="invoke --role RECOVER after proving exact owner state")
    raise RuntimeError(f"successor {role} failed exact ACK/liveness")


def _await_release(root: Path, role: str, handoff: str, turn: int) -> None:
    if handoff.startswith("recovery-"):
        _trace(root, "recovery_owner_admitted", role=role, handoff=handoff, turn=turn, pid=os.getpid())
        return
    verified = root / "verified" / f"{handoff}.{role}.json"; deadline = time.monotonic() + ACK_TIMEOUT
    while time.monotonic() < deadline:
        if verified.exists() and _read(verified).get("pid") == os.getpid():
            atomic_json(root / "live" / f"{handoff}.{role}.json", {"pid": os.getpid(), "role": role, "handoff": handoff, "turn": turn, "at": now()})
            release = root / "release" / f"{handoff}.{role}.json"
            while time.monotonic() < deadline:
                if release.exists() and _read(release).get("pid") == os.getpid(): return
                time.sleep(.02)
        time.sleep(.02)
    _incident(root, severity="HIGH", actor=role, subject="parent", reason="parent_release_timeout",
              evidence={"role": role, "handoff": handoff, "turn": turn, "verified_ref": str(verified)},
              next_boundary="invoke --role RECOVER")
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
    instruction_hash = _hash(instruction.read_bytes()) if instruction.exists() else None
    started_at = now()
    started_monotonic = time.monotonic()
    _event(root, "codex_starting", role=role, model=LUNA_MODEL,
           reasoning_effort=LUNA_REASONING_EFFORT, model_selection="explicit-cli-arguments",
           command=command, instruction=instruction.name, instruction_sha256=instruction_hash,
           pid=None, parent_pid=os.getpid())
    _trace(root, "codex_starting", role=role, model=LUNA_MODEL,
           reasoning_effort=LUNA_REASONING_EFFORT, model_selection="explicit-cli-arguments",
           command=command, instruction=instruction.name, instruction_sha256=instruction_hash,
           started_at=started_at, parent_pid=os.getpid())
    try:
        result = subprocess.run(command, input=prompt, text=True, cwd=repository, capture_output=True, timeout=CODEX_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        _incident(root, severity="HIGH", actor=role, subject="codex", reason=type(exc).__name__,
                  evidence={"instruction": instruction.name, "instruction_sha256": instruction_hash,
                            "model": LUNA_MODEL, "command": command, "elapsed_seconds": time.monotonic() - started_monotonic},
                  next_boundary="bounded recovery entrypoint")
        raise RuntimeError(f"real {role} Codex launch failed: {type(exc).__name__}") from exc
    log_text = (result.stdout or "") + "\n" + (result.stderr or "")
    log_path = root / "agent_logs" / f"{role}-{instruction.stem}.log"
    log_path.write_text(log_text, encoding="utf-8")
    finished_at = now()
    _event(root, "real_codex_exited", role=role, instruction=instruction.name,
           exit_code=result.returncode, model=LUNA_MODEL,
           reasoning_effort=LUNA_REASONING_EFFORT, stdout_sha256=_hash((result.stdout or "").encode()),
           stderr_sha256=_hash((result.stderr or "").encode()), duration_seconds=round(time.monotonic() - started_monotonic, 3))
    _trace(root, "real_codex_exited", role=role, instruction=instruction.name,
           exit_code=result.returncode, model=LUNA_MODEL,
           reasoning_effort=LUNA_REASONING_EFFORT, stdout_sha256=_hash((result.stdout or "").encode()),
           stderr_sha256=_hash((result.stderr or "").encode()), log_path=str(log_path),
           started_at=started_at, finished_at=finished_at, duration_seconds=round(time.monotonic() - started_monotonic, 3))
    if result.returncode:
        _incident(root, severity="HIGH", actor=role, subject="codex", reason="nonzero_exit",
                  evidence={"exit_code": result.returncode, "instruction": instruction.name,
                            "stdout_sha256": _hash((result.stdout or "").encode()),
                            "stderr_sha256": _hash((result.stderr or "").encode()),
                            "duration_seconds": round(time.monotonic() - started_monotonic, 3)},
                  next_boundary="inspect incident bundle and reconstruct runtime")
        raise RuntimeError(f"real {role} Codex exited {result.returncode}")


def _controller_instruction(root: Path, run: dict, turn: int, output: Path) -> Path:
    previous = root / "results" / f"{turn:04d}.json"
    text = {
        "role": "Controller Agent A", "objective_file": str(root / "run.json"),
        "previous_worker_result_file": str(previous) if previous.exists() else None,
        "output_file": str(output), "allowed_decisions": sorted(DECISIONS),
        "requirements": ["Read the durable objective and previous result.", "Independently decide one allowed decision.", "For CONTINUE, author one useful, bounded, read-only repository task for the Worker, including a concise task_body and optional failure_injection boolean.", "For COMPLETE or HUMAN_REQUIRED, provide a concise reason.", "Do not modify the repository."],
        "output_schema": {"decision": "CONTINUE|COMPLETE|HUMAN_REQUIRED", "reason": "string", "task_body": "string required for CONTINUE", "failure_injection": "boolean optional", "concerns": "array of structured concerns optional", "unresolved_questions": "array optional"},
    }
    path = root / "agent_instructions" / f"controller-{turn:04d}.json"; atomic_json(path, text); return path


def _worker_instruction(root: Path, turn: int, task: Path, output: Path) -> Path:
    text = {"role": "Worker Agent B", "task_file": str(task / "task.json"), "output_file": str(output),
            "requirements": ["Read only the durable task file for task authority.", "Perform its bounded read-only repository inspection.", "Do not modify, commit, push, or run tests.", "If failure_injection is true, return status FAILED with a concise failure field instead of doing work."],
            "output_schema": {"status": "OK|FAILED", "summary": "string", "evidence": "object optional", "failure": "string required when FAILED", "concerns": "array of structured concerns optional"}}
    path = root / "agent_instructions" / f"worker-{turn:04d}.json"; atomic_json(path, text); return path


def _write_task(root: Path, run: dict, turn: int, decision: dict) -> None:
    directory = root / "tasks" / f"{turn:04d}"; directory.mkdir(parents=True, exist_ok=False)
    body = str(decision["task_body"]).encode("utf-8")
    payload = json.dumps({"run_id": run["run_id"], "turn": turn, "repository": run["repository"], "task_body": body.decode("utf-8"), "failure_injection": bool(decision.get("failure_injection", False))}, sort_keys=True).encode("utf-8")
    (directory / "task.json").write_bytes(payload)
    atomic_json(directory / "manifest.json", {"run_id": run["run_id"], "turn": turn, "payload_sha256": _hash(payload), "body_sha256": _hash(body), "created_by": "real-Codex-A", "at": now()})
    _event(root, "task_written", role="A", turn=turn, task_sha256=_hash(payload), injected=bool(decision.get("failure_injection", False)))
    _trace(root, "task_written", role="A", run_id=run["run_id"], turn=turn,
           task_path=str(directory / "task.json"), manifest_path=str(directory / "manifest.json"),
           task_sha256=_hash(payload), body_sha256=_hash(body), injected=bool(decision.get("failure_injection", False)))


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
    _save_runtime_state(root, turn_id=turn, current_role="A", next_role="A", continuation_state="CONTROLLER_RUNNING", owner={"role": "A", "pid": os.getpid(), "turn": turn, "handoff": handoff})
    if handoff != "initial-A": _await_release(root, "A", handoff, turn)
    run_path = root / "run.json"; run = _read(run_path)
    decision, value = _decide(root, run, turn)
    concerns = value.get("concerns", [])
    if concerns:
        _incident(root, severity="MEDIUM", actor="Controller Agent A", subject="peer/runtime", reason="agent_concern",
                  evidence={"turn": turn, "concerns": concerns, "output_ref": str(root / "agent_instructions" / f"controller-output-{turn:04d}.json")},
                  next_boundary="include focused incident evidence in terminal handoff")
    if isinstance(value.get("unresolved_questions"), list):
        _save_runtime_state(root, unresolved_questions=value["unresolved_questions"])
    run["decisions"].append({"turn": turn, "decision": decision, "reason": value.get("reason", ""), "at": now()}); run["status"] = decision; atomic_json(run_path, run)
    _event(root, "controller_decision", role="A", turn=turn, decision=decision, reason=value.get("reason", ""))
    _trace(root, "controller_decision", role="A", turn=turn, decision=decision, reason=value.get("reason", ""), output_ref=str(root / "agent_instructions" / f"controller-output-{turn:04d}.json"))
    if decision != "CONTINUE":
        atomic_json(root / "terminal" / "result.json", {"run_id": run["run_id"], "decision": decision, "reason": value.get("reason", ""), "at": now()})
        _save_runtime_state(root, turn_id=turn, current_role="A", next_role=None, continuation_state="TERMINAL", owner=None)
        _trace(root, "runtime_terminal", role="A", turn=turn, decision=decision)
        return
    next_turn = turn + 1 if (root / "results" / f"{turn:04d}.json").exists() else turn
    _write_task(root, run, next_turn, value)
    _save_runtime_state(root, turn_id=next_turn, current_role="A", next_role="B", current_task_ref=str(root / "tasks" / f"{next_turn:04d}" / "task.json"), prior_result_ref=str(root / "results" / f"{turn:04d}.json") if (root / "results" / f"{turn:04d}.json").exists() else None, continuation_state="TASK_READY", owner=None)
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
    _save_runtime_state(root, turn_id=turn, current_role="B", next_role="B", current_task_ref=str(task / "task.json"), continuation_state="WORKER_RUNNING", owner={"role": "B", "pid": os.getpid(), "turn": turn, "handoff": handoff})
    _trace(root, "task_claimed", role="B", turn=turn, pid=os.getpid(), parent_pid=os.getppid(),
           task_path=str(task / "task.json"), payload_sha256=_hash(payload), claim_path=str(claim))
    output = root / "agent_instructions" / f"worker-output-{turn:04d}.json"
    try:
        _codex(root, "Worker Agent B", _worker_instruction(root, turn, task, output), Path(request["repository"]))
        result = _read(output)
        if result.get("status") not in {"OK", "FAILED"}: raise RuntimeError("Worker Codex emitted invalid status")
    except Exception as exc:
        result = {"status": "FAILED", "failure": f"{type(exc).__name__}: {exc}", "summary": "Worker bootstrap failure"}
    outcome = {"run_id": request["run_id"], "turn": turn, "status": result["status"], "summary": str(result.get("summary", "")), "evidence": result.get("evidence", {}), "failure": result.get("failure"), "concerns": result.get("concerns", []), "payload_sha256": _hash(payload), "worker_pid": os.getpid()}
    concerns = result.get("concerns", [])
    if concerns:
        _incident(root, severity="MEDIUM", actor="Worker Agent B", subject="repository/runtime", reason="agent_concern",
                  evidence={"turn": turn, "concerns": concerns, "task_ref": str(task / "task.json"), "payload_sha256": _hash(payload)},
                  next_boundary="Controller A must decide from durable result")
    atomic_json(root / "results" / f"{turn:04d}.json", outcome); _event(root, "result_written", role="B", turn=turn, status=outcome["status"])
    _trace(root, "result_written", role="B", turn=turn, status=outcome["status"],
           result_path=str(root / "results" / f"{turn:04d}.json"), result_sha256=_hash((root / "results" / f"{turn:04d}.json").read_bytes()),
           payload_sha256=outcome["payload_sha256"], failure=outcome.get("failure"), concerns=outcome.get("concerns", []))
    _save_runtime_state(root, turn_id=turn, current_role="B", next_role="A", current_task_ref=str(task / "task.json"), prior_result_ref=str(root / "results" / f"{turn:04d}.json"), continuation_state="RESULT_READY", owner=None)
    handoff_id = _handoff_id("A", turn); child = _spawn(root, "A", turn, handoff_id)
    _wait_successor(root, "A", handoff_id, turn, child.pid); _event(root, "parent_exit_after_ack", role="B", turn=turn, child_pid=child.pid)


def initialize(root: Path, repository: Path, run_id: str, *, objective: str) -> None:
    _paths(root)
    repository = repository.resolve()
    manifest = {
        "protocol": RUNTIME_PROTOCOL,
        "protocol_version": 1,
        "run_id": run_id,
        "turn_id": 0,
        "current_role": "A",
        "next_role": "A",
        "required_model": LUNA_MODEL,
        "required_reasoning_effort": LUNA_REASONING_EFFORT,
        "repository": str(repository),
        "branch": None,
        "expected_provenance": {"branch": None, "head": None, "remote_head": None},
        "objective": objective,
        "durable_constraints": ["file-backed task/result only", "one owner per turn", "no prompt history concatenation"],
        "decisions_ref": "run.json",
        "unresolved_questions": [],
        "current_task_ref": None,
        "prior_result_ref": None,
        "output_contract": {"controller": "controller-output-TURN.json", "worker": "worker-output-TURN.json"},
        "logging": {"normal_journal": "events.jsonl", "process_trace": "trace/process.jsonl", "incident_dir": "incidents"},
        "continuation": {"state": "READY", "owner": None, "claim_ref": None},
        "terminal_contract": {"decisions": sorted(DECISIONS), "terminal_file": "terminal/result.json"},
        "created_at": now(),
    }
    atomic_json(root / "manifest.json", manifest)
    atomic_json(root / "run.json", {"run_id": run_id, "repository": str(repository), "objective": objective, "decisions": [], "status": "CONTINUE"})
    _save_runtime_state(root, protocol=RUNTIME_PROTOCOL, run_id=run_id, turn_id=0,
                        current_role="A", next_role="A", required_model=LUNA_MODEL,
                        required_reasoning_effort=LUNA_REASONING_EFFORT, current_task_ref=None,
                        prior_result_ref=None, continuation_state="READY", owner=None)
    _event(root, "runtime_initialized", run_id=run_id, repository=str(repository), model=LUNA_MODEL, reasoning_effort=LUNA_REASONING_EFFORT)
    _trace(root, "runtime_initialized", run_id=run_id, repository=str(repository), model=LUNA_MODEL,
           reasoning_effort=LUNA_REASONING_EFFORT, manifest_path=str(root / "manifest.json"), parent_pid=os.getpid())


def _pid_live(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def reconstruct_runtime(root: Path) -> dict:
    """Reconstruct the next safe boundary using only durable files.

    This is deliberately side-effect-light: it never invents a result and it
    launches at most one replacement owner after proving the recorded owner is
    dead. Stale claims are retained as immutable evidence before being moved
    aside for the exact same turn to retry.
    """
    root = root.resolve(); _paths(root)
    manifest = _read(root / "manifest.json")
    run = _read(root / "run.json")
    if manifest.get("protocol") != RUNTIME_PROTOCOL or manifest.get("required_model") != LUNA_MODEL or manifest.get("required_reasoning_effort") != LUNA_REASONING_EFFORT:
        incident = _incident(root, severity="CRITICAL", actor="recovery", subject="manifest", reason="runtime_contract_mismatch",
                             evidence={"manifest": manifest}, next_boundary="repair manifest before relaunch")
        raise RuntimeError(f"runtime contract mismatch ({incident})")
    tasks = sorted((root / "tasks").glob("*/task.json"), key=lambda p: int(p.parent.name))
    results = sorted((root / "results").glob("*.json"), key=lambda p: int(p.stem))
    claims = sorted((root / "claims").glob("[0-9][0-9][0-9][0-9].json"), key=lambda p: int(p.stem))
    latest_turn = max([int(p.parent.name) for p in tasks] + [int(p.stem) for p in results] + [int(p.stem) for p in claims] + [0])
    task = root / "tasks" / f"{latest_turn:04d}" / "task.json"
    result = root / "results" / f"{latest_turn:04d}.json"
    claim = root / "claims" / f"{latest_turn:04d}.json"
    owner = _read(claim) if claim.exists() else None
    if (root / "terminal" / "result.json").exists() or run.get("status") in {"COMPLETE", "HUMAN_REQUIRED"}:
        plan = {"action": "TERMINAL", "run_id": run.get("run_id"), "turn": latest_turn, "role": None}
    elif result.exists():
        plan = {"action": "RESUME_CONTROLLER", "run_id": run.get("run_id"), "turn": latest_turn, "role": "A", "result_ref": str(result)}
    elif task.exists() and owner:
        pid = owner.get("pid")
        if _pid_live(pid):
            plan = {"action": "WAIT_OWNER", "run_id": run.get("run_id"), "turn": latest_turn, "role": "B", "pid": pid, "task_ref": str(task)}
        else:
            stale = claim.with_name(f"{claim.stem}.stale-{pid or 'unknown'}.json")
            claim.replace(stale)
            incident = _incident(root, severity="HIGH", actor="recovery", subject="Worker B", reason="owner_dead_before_result",
                                 evidence={"turn": latest_turn, "pid": pid, "task_ref": str(task), "claim_ref": str(stale)},
                                 next_boundary="retry the same durable task exactly once")
            plan = {"action": "RETRY_WORKER", "run_id": run.get("run_id"), "turn": latest_turn, "role": "B", "task_ref": str(task), "incident_id": incident}
    elif task.exists():
        plan = {"action": "RESUME_WORKER", "run_id": run.get("run_id"), "turn": latest_turn, "role": "B", "task_ref": str(task)}
    else:
        plan = {"action": "RESUME_CONTROLLER", "run_id": run.get("run_id"), "turn": latest_turn, "role": "A"}
    state = _save_runtime_state(root, turn_id=latest_turn, current_role=plan.get("role"), next_role=plan.get("role"),
                                current_task_ref=plan.get("task_ref"), prior_result_ref=plan.get("result_ref"),
                                continuation_state=plan["action"])
    _event(root, "runtime_reconstructed", **{k: v for k, v in plan.items() if k != "run_id"})
    _trace(root, "runtime_reconstructed", plan=plan, runtime_state=state)
    return plan


def recover(root: Path) -> dict:
    """Bounded generic recovery entrypoint for a previously durable run."""
    plan = reconstruct_runtime(root)
    if plan["action"] in {"TERMINAL", "WAIT_OWNER"}:
        return plan
    role = str(plan.get("role") or "")
    turn = int(plan.get("turn", 0))
    handoff = f"recovery-{role}-{turn:04d}-{uuid4().hex}"
    child = _spawn(root, role, turn, handoff)
    _save_runtime_state(root, owner={"role": role, "turn": turn, "pid": child.pid, "handoff": handoff}, continuation_state="OWNER_STARTED")
    _event(root, "recovery_owner_started", role=role, turn=turn, pid=child.pid, handoff=handoff)
    _trace(root, "recovery_owner_started", role=role, turn=turn, pid=child.pid, handoff=handoff)
    return {**plan, "action": "OWNER_STARTED", "pid": child.pid, "handoff": handoff}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--root", required=True); parser.add_argument("--role", choices=("A", "B", "RECOVER"), required=True); parser.add_argument("--turn", type=int, default=0); parser.add_argument("--handoff", default="")
    args = parser.parse_args(argv); root = Path(args.root).resolve(); _paths(root)
    try:
        if args.role == "RECOVER":
            recover(root)
        else:
            (controller if args.role == "A" else worker)(root, args.turn, args.handoff)
    except Exception as exc:
        detail = str(exc)[:500]
        _event(root, "failed", role=args.role, turn=args.turn, error=type(exc).__name__, detail=detail)
        _incident(root, severity="HIGH", actor=args.role, subject="runtime", reason=type(exc).__name__,
                  evidence={"turn": args.turn, "detail": detail}, next_boundary="invoke --role RECOVER")
        return 1
    return 0


if __name__ == "__main__": raise SystemExit(main())
