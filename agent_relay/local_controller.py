"""Bounded, file-only local Controller (A) / Worker (B) certification loop.

This is intentionally separate from the Gmail, watchdog, and ChatGPT paths.
The controller has a durable objective with a finite checklist and can decide
only CONTINUE, COMPLETE, or HUMAN_REQUIRED.  Process arguments carry only
identity and location data; task and result bodies always live in files.
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


ACK_TIMEOUT = 20.0
FACTS = ("cwd", "branch", "head", "clean", "pyproject", "top_level", "git_dir", "readme", "package", "python_files")


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _event(root: Path, event: str, **fields: object) -> None:
    path = root / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps({"at": now(), "event": event, **fields}, sort_keys=True) + "\n")


def _paths(root: Path) -> None:
    for name in ("acks", "verified", "live", "release", "owners", "tasks", "results", "claims", "terminal"):
        (root / name).mkdir(parents=True, exist_ok=True)


def _handoff_id(role: str, turn: int) -> str:
    return f"{turn:04d}-{role}-{uuid4().hex}"


def _ack(root: Path, role: str, handoff: str, turn: int) -> None:
    atomic_json(root / "acks" / f"{handoff}.{role}.json", {"role": role, "handoff": handoff, "turn": turn, "pid": os.getpid(), "at": now()})
    atomic_json(root / "owners" / f"{handoff}.{role}.json", {"role": role, "handoff": handoff, "turn": turn, "pid": os.getpid(), "state": "STARTED", "at": now()})
    _event(root, "startup_ack", role=role, handoff=handoff, turn=turn, pid=os.getpid())


def _spawn(root: Path, role: str, turn: int, handoff: str) -> subprocess.Popen:
    command = [sys.executable, "-m", "agent_relay.local_controller", "--root", str(root), "--role", role, "--turn", str(turn), "--handoff", handoff]
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    child = subprocess.Popen(command, cwd=Path(__file__).resolve().parents[1], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags)
    _event(root, "peer_spawned", role=role, handoff=handoff, turn=turn, pid=child.pid, parent_pid=os.getpid())
    return child


def _wait_successor(root: Path, role: str, handoff: str, turn: int, pid: int) -> None:
    ack = root / "acks" / f"{handoff}.{role}.json"
    deadline = time.monotonic() + ACK_TIMEOUT
    while time.monotonic() < deadline:
        if ack.exists():
            value = _read(ack)
            if value.get("role") == role and value.get("handoff") == handoff and value.get("turn") == turn and value.get("pid") == pid:
                if role == "B":
                    claim = root / "claims" / f"{turn:04d}.json"
                    if not claim.exists() or _read(claim).get("pid") != pid:
                        time.sleep(.02); continue
                atomic_json(root / "verified" / f"{handoff}.{role}.json", value)
                live = root / "live" / f"{handoff}.{role}.json"
                until = time.monotonic() + ACK_TIMEOUT
                while time.monotonic() < until:
                    if live.exists() and _read(live).get("pid") == pid:
                        atomic_json(root / "release" / f"{handoff}.{role}.json", {"pid": pid, "at": now()})
                        _event(root, "successor_verified", role=role, handoff=handoff, turn=turn, pid=pid)
                        return
                    time.sleep(.02)
        time.sleep(.02)
    raise RuntimeError(f"successor {role} failed exact ACK/liveness")


def _await_release(root: Path, role: str, handoff: str, turn: int) -> None:
    verified = root / "verified" / f"{handoff}.{role}.json"
    deadline = time.monotonic() + ACK_TIMEOUT
    while time.monotonic() < deadline:
        if verified.exists() and _read(verified).get("pid") == os.getpid():
            atomic_json(root / "live" / f"{handoff}.{role}.json", {"pid": os.getpid(), "role": role, "handoff": handoff, "turn": turn, "at": now()})
            release = root / "release" / f"{handoff}.{role}.json"
            while time.monotonic() < deadline:
                if release.exists() and _read(release).get("pid") == os.getpid():
                    return
                time.sleep(.02)
        time.sleep(.02)
    raise RuntimeError("parent did not verify startup ACK")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def _inspect(repo: Path, fact: str) -> str:
    if fact == "cwd": return str(repo.resolve())
    if fact == "branch": return _git(repo, "branch", "--show-current")
    if fact == "head": return _git(repo, "rev-parse", "HEAD")
    if fact == "clean": return "clean" if not _git(repo, "status", "--short") else "dirty"
    if fact == "pyproject": return str((repo / "pyproject.toml").is_file())
    if fact == "top_level": return ",".join(sorted(item.name for item in repo.iterdir())[:8])
    if fact == "git_dir": return str((repo / ".git").exists())
    if fact == "readme": return str((repo / "README.md").is_file())
    if fact == "package": return str((repo / "agent_relay" / "__init__.py").is_file())
    if fact == "python_files": return str(len(list((repo / "agent_relay").glob("*.py"))) > 0)
    raise ValueError("unknown bounded fact")


def _write_task(root: Path, run: dict, turn: int, fact: str, inject_failure: bool = False) -> None:
    directory = root / "tasks" / f"{turn:04d}"
    payload = json.dumps({"run_id": run["run_id"], "turn": turn, "fact": fact, "repository": run["repository"], "inject_failure": inject_failure}, sort_keys=True).encode("utf-8")
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "task.json").write_bytes(payload)
    atomic_json(directory / "manifest.json", {"run_id": run["run_id"], "turn": turn, "payload_sha256": _hash(payload), "fact": fact, "created_by": "A", "at": now()})
    _event(root, "task_written", role="A", turn=turn, fact=fact, injected=inject_failure)


def _decision(root: Path, run: dict, turn: int) -> tuple[str, int | None, str | None, bool]:
    result = _read(root / "results" / f"{turn:04d}.json")
    task = root / "tasks" / f"{turn:04d}"
    manifest = _read(task / "manifest.json")
    payload = (task / "task.json").read_bytes()
    if result.get("run_id") != run["run_id"] or result.get("turn") != turn or result.get("payload_sha256") != manifest.get("payload_sha256") or _hash(payload) != manifest.get("payload_sha256"):
        return "HUMAN_REQUIRED", None, "integrity mismatch", False
    evidence = run.setdefault("evidence", {})
    if result.get("status") == "FAILED":
        if run.get("failure_recovery_used"):
            return "COMPLETE", None, "failure reported deterministically", False
        run["failure_recovery_used"] = True
        return "CONTINUE", turn + 1, "recovery after durable worker failure", False
    evidence[result["fact"]] = result["value"]
    missing = [fact for fact in FACTS if fact not in evidence]
    if missing:
        return "CONTINUE", turn + 1, "bounded checklist remains", False
    if not run.get("failure_injected"):
        run["failure_injected"] = True
        return "CONTINUE", turn + 1, "run one bounded failure injection", True
    return "COMPLETE", None, "all bounded facts and failure recovery recorded", False


def controller(root: Path, turn: int, handoff: str) -> None:
    _ack(root, "A", handoff, turn)
    # The first controller is launched directly by the bounded certification
    # command. Every subsequent A has a B predecessor and must await release.
    if handoff != "initial-A":
        _await_release(root, "A", handoff, turn)
    run_path = root / "run.json"; run = _read(run_path)
    result_path = root / "results" / f"{turn:04d}.json"
    if result_path.exists():
        decision, next_turn, reason, inject = _decision(root, run, turn)
        run["decisions"].append({"turn": turn, "decision": decision, "reason": reason, "at": now()})
        run["status"] = decision
        atomic_json(run_path, run)
        _event(root, "controller_decision", role="A", turn=turn, decision=decision, reason=reason)
        if decision != "CONTINUE":
            atomic_json(root / "terminal" / "result.json", {"run_id": run["run_id"], "decision": decision, "reason": reason, "at": now()})
            return
        if inject or (run.get("failure_recovery_used") and turn > len(FACTS)):
            fact = "clean"
        else:
            fact = FACTS[next_turn - 1]
        _write_task(root, run, next_turn, fact, inject_failure=inject)
        turn = next_turn
    elif turn == 1:
        _write_task(root, run, turn, FACTS[0])
    else:
        raise RuntimeError("controller resumed without durable result")
    next_handoff = _handoff_id("B", turn)
    child = _spawn(root, "B", turn, next_handoff)
    _wait_successor(root, "B", next_handoff, turn, child.pid)
    _event(root, "parent_exit_after_ack", role="A", turn=turn, child_pid=child.pid)


def worker(root: Path, turn: int, handoff: str) -> None:
    task = root / "tasks" / f"{turn:04d}"
    manifest = _read(task / "manifest.json"); payload = (task / "task.json").read_bytes(); request = json.loads(payload)
    if _hash(payload) != manifest.get("payload_sha256") or request.get("turn") != turn:
        raise RuntimeError("task hash/turn mismatch")
    claim = root / "claims" / f"{turn:04d}.json"
    try:
        fd = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise RuntimeError("duplicate task claim") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as output: json.dump({"turn": turn, "pid": os.getpid(), "role": "B", "handoff": handoff}, output)
    _event(root, "task_claimed", role="B", turn=turn, pid=os.getpid())
    _ack(root, "B", handoff, turn); _await_release(root, "B", handoff, turn)
    try:
        if request.get("inject_failure"):
            raise RuntimeError("injected bounded worker failure")
        value = _inspect(Path(request["repository"]), request["fact"])
        outcome = {"run_id": request["run_id"], "turn": turn, "fact": request["fact"], "value": value, "status": "OK", "payload_sha256": _hash(payload), "worker_pid": os.getpid()}
        exit_code = 0
    except Exception as exc:
        outcome = {"run_id": request["run_id"], "turn": turn, "fact": request["fact"], "status": "FAILED", "failure": type(exc).__name__, "payload_sha256": _hash(payload), "worker_pid": os.getpid()}
        exit_code = 1
    atomic_json(root / "results" / f"{turn:04d}.json", outcome)
    _event(root, "result_written", role="B", turn=turn, status=outcome["status"])
    next_handoff = _handoff_id("A", turn)
    child = _spawn(root, "A", turn, next_handoff)
    _wait_successor(root, "A", next_handoff, turn, child.pid)
    _event(root, "parent_exit_after_ack", role="B", turn=turn, child_pid=child.pid)
    if exit_code:
        raise RuntimeError("worker exits nonzero after durable failure result")


def initialize(root: Path, repository: Path, run_id: str) -> None:
    _paths(root)
    atomic_json(root / "run.json", {"run_id": run_id, "repository": str(repository.resolve()), "objective": "Collect ten harmless, mechanically verifiable repository facts, then prove bounded worker-failure recovery.", "completion_condition": "All ten facts are durable and a nonzero worker failure has returned to A and been recovered.", "required_facts": list(FACTS), "evidence": {}, "decisions": [], "status": "CONTINUE", "failure_injected": False, "failure_recovery_used": False})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--root", required=True); parser.add_argument("--role", choices=("A", "B"), required=True); parser.add_argument("--turn", type=int, required=True); parser.add_argument("--handoff", required=True)
    args = parser.parse_args(argv); root = Path(args.root).resolve(); _paths(root)
    try:
        (controller if args.role == "A" else worker)(root, args.turn, args.handoff)
    except Exception as exc:
        _event(root, "failed", role=args.role, turn=args.turn, error=type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
