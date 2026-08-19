"""Bounded local Controller/Worker handoff prototype.

This deliberately does not participate in the Gmail/watchdog path.  A and B
exchange only durable files; command-line arguments identify a runtime root,
role, and nonce and never contain task content.
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


ACK_TIMEOUT = 15.0


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _event(root: Path, event: str, **fields) -> None:
    path = root / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": now(), "event": event, **fields}, sort_keys=True) + "\n")


def _owner(root: Path, role: str, nonce: str, state: str) -> None:
    atomic_json(root / "owners" / f"{role}.json", {"role": role, "nonce": nonce, "pid": os.getpid(), "state": state, "at": now()})


def _ack(root: Path, role: str, nonce: str) -> Path:
    path = root / "acks" / f"{nonce}.{role}.json"
    atomic_json(path, {"role": role, "nonce": nonce, "pid": os.getpid(), "at": now()})
    _event(root, "startup_ack", role=role, nonce=nonce, pid=os.getpid())
    return path


def spawn_peer(root: Path, role: str, nonce: str, *, parent_death: bool) -> subprocess.Popen:
    """Shared, detached Windows peer launcher used in both directions."""
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    command = [sys.executable, "-m", "agent_relay.dual_agent", "--root", str(root), "--role", role, "--nonce", nonce]
    if parent_death:
        command.append("--parent-death")
    process = subprocess.Popen(command, cwd=Path(__file__).resolve().parents[1], stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                               creationflags=flags)
    _event(root, "peer_spawned", role=role, nonce=nonce, pid=process.pid, parent_pid=os.getpid())
    return process


def _wait_ack(root: Path, role: str, nonce: str, pid: int) -> None:
    path = root / "acks" / f"{nonce}.{role}.json"
    deadline = time.monotonic() + ACK_TIMEOUT
    while time.monotonic() < deadline:
        if path.exists():
            value = _json(path)
            if value.get("role") == role and value.get("nonce") == nonce and value.get("pid") == pid:
                if role == "B":
                    claim = root / "active" / f"{nonce}.claim"
                    if not claim.exists() or _json(claim).get("pid") != pid:
                        time.sleep(0.02)
                        continue
                atomic_json(root / "verified" / f"{nonce}.{role}.json", {"role": role, "nonce": nonce, "pid": pid})
                live = root / "live" / f"{nonce}.{role}.json"
                until = time.monotonic() + ACK_TIMEOUT
                while time.monotonic() < until:
                    if live.exists() and _json(live).get("pid") == pid:
                        atomic_json(root / "release" / f"{nonce}.{role}.json", {"pid": pid})
                        _event(root, "successor_verified", role=role, nonce=nonce, pid=pid)
                        return
                    time.sleep(0.02)
        time.sleep(0.03)
    raise RuntimeError(f"successor {role} failed exact ACK/liveness")


def _wait_verified(root: Path, role: str, nonce: str) -> None:
    """Keep a successor alive until its parent durably verifies its ACK."""
    path = root / "verified" / f"{nonce}.{role}.json"
    deadline = time.monotonic() + ACK_TIMEOUT
    while time.monotonic() < deadline:
        if path.exists():
            value = _json(path)
            if value.get("role") == role and value.get("nonce") == nonce and value.get("pid") == os.getpid():
                atomic_json(root / "live" / f"{nonce}.{role}.json", {"role": role, "nonce": nonce, "pid": os.getpid()})
                release = root / "release" / f"{nonce}.{role}.json"
                while time.monotonic() < deadline:
                    if release.exists() and _json(release).get("pid") == os.getpid():
                        return
                    time.sleep(0.02)
        time.sleep(0.02)
    raise RuntimeError("parent did not verify startup ACK")


def _result_text(value: dict) -> str:
    """Return the exact durable terminal result without putting it on argv."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def controller(root: Path, nonce: str, parent_death: bool) -> None:
    _owner(root, "A", nonce, "STARTED"); _ack(root, "A", nonce)
    task = root / "inbox" / nonce
    result = root / "results" / f"{nonce}.result.json"
    # A only waits for B's startup verification when it is the returning A.
    # A raw ChatGPT task also already has an inbox directory, so using that as
    # the discriminator would deadlock the first handoff.
    _wait_verified(root, "A", nonce) if result.exists() else None
    if not task.exists():
        payload = f"harmless nonce task {nonce}\n".encode()
        atomic_json(task / "manifest.json", {"nonce": nonce, "payload_sha256": _sha(payload), "created_by": "A"})
        (task / "payload.md").parent.mkdir(parents=True, exist_ok=True)
        (task / "payload.md").write_bytes(payload)
        _event(root, "task_written", role="A", nonce=nonce)
    if not result.exists():
        # This is either A's locally-created task or a raw ChatGPT turn which
        # was already durably published by the read-only bridge.
        child = spawn_peer(root, "B", nonce, parent_death=parent_death)
        _wait_ack(root, "B", nonce, child.pid)
        _event(root, "parent_exit_after_ack", role="A", nonce=nonce, child_pid=child.pid, parent_death=parent_death)
        return
    # B has produced a result and returned ownership to a fresh Controller.
    deadline = time.monotonic() + ACK_TIMEOUT
    while not result.exists() and time.monotonic() < deadline:
        time.sleep(0.03)
    value = _json(result)
    manifest = _json(task / "manifest.json")
    if value.get("nonce") != nonce or value.get("payload_sha256") != manifest.get("payload_sha256"):
        raise RuntimeError("result hash/nonce mismatch")
    # A configured bridge is deliberately invoked only by the returning A:
    # B can neither access nor semantically transform the ChatGPT payload.
    bridge = root / "chatgpt" / "bridge.json"
    if bridge.exists():
        from .chatgpt_bridge import submit_durable_result
        delivery = submit_durable_result(root, nonce, _result_text(value))
        if not delivery.get("verified"):
            raise RuntimeError("configured ChatGPT result delivery was not verified")
    atomic_json(root / "terminal" / f"{nonce}.json", {"nonce": nonce, "state": "COMPLETE", "at": now()})
    _event(root, "cycle_complete", role="A", nonce=nonce)


def worker(root: Path, nonce: str, parent_death: bool) -> None:
    task = root / "inbox" / nonce
    manifest = _json(task / "manifest.json")
    payload = (task / "payload.md").read_bytes()
    if manifest.get("nonce") != nonce or manifest.get("payload_sha256") != _sha(payload):
        raise RuntimeError("task hash/nonce mismatch")
    claim = root / "active" / f"{nonce}.claim"
    claim.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise RuntimeError("duplicate task claim") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"nonce": nonce, "pid": os.getpid(), "role": "B"}, handle)
    _event(root, "task_claimed", role="B", nonce=nonce)
    _owner(root, "B", nonce, "STARTED"); _ack(root, "B", nonce)
    _wait_verified(root, "B", nonce)
    atomic_json(root / "results" / f"{nonce}.result.json", {"nonce": nonce, "payload_sha256": _sha(payload), "result_sha256": _sha(b"completed:" + payload), "worker_pid": os.getpid()})
    _event(root, "result_written", role="B", nonce=nonce)
    child = spawn_peer(root, "A", nonce, parent_death=parent_death)
    _wait_ack(root, "A", nonce, child.pid)
    _event(root, "parent_exit_after_ack", role="B", nonce=nonce, child_pid=child.pid, parent_death=parent_death)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--role", choices=("A", "B"), required=True)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--parent-death", action="store_true")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    # Create the fixed durable layout before any peer is launched.  This avoids
    # a competing parent-directory creation during the ACK critical section.
    for name in ("acks", "verified", "live", "release", "owners", "inbox", "active", "results", "terminal"):
        (root / name).mkdir(parents=True, exist_ok=True)
    try:
        (controller if args.role == "A" else worker)(root, args.nonce, args.parent_death)
    except Exception as exc:
        _event(root, "failed", role=args.role, nonce=args.nonce, error=type(exc).__name__, detail=str(exc)[:200])
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
