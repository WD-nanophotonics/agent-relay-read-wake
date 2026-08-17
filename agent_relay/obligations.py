"""Durable, bounded terminal-handoff obligations."""
from __future__ import annotations

from pathlib import Path
from typing import Any
import json

from .handoff import build_actionable_report
from .ownership import exact_owner_live
from .storage import atomic_json, now


OPEN = "OPEN"
RESULT_READY = "RESULT_READY"
SENDING = "SENDING"
VERIFIED = "VERIFIED"


def obligation_path(root: Path, worker_id: str) -> Path:
    return root / "handoff_obligations" / f"{worker_id}.json"


def create_obligation(root: Path, *, worker_id: str, run_id: str, step: int, parent: int,
                     project_id: str, channel_id: str, repository: str,
                     chat_url: str, message_id: str | None = None,
                     content_hash: str | None = None, worker_pid: int | None = None,
                     worker_exe: str | None = None) -> dict[str, Any]:
    token = f"AR-HANDOFF-{worker_id}"
    value: dict[str, Any] = {
        "protocol": "AGENTRELAY_HANDOFF_OBLIGATION/1",
        "worker_id": worker_id,
        "run_id": run_id,
        "step": step,
        "parent": parent,
        "project_id": project_id,
        "channel_id": channel_id,
        "repository": repository,
        "branch": None,
        "baseline_sha": None,
        "remote_head": None,
        "ending_sha": None,
        "changed_files": None,
        "exit_code": None,
        "message_id": message_id,
        "content_hash": content_hash,
        "worker_pid": worker_pid,
        "worker_exe": worker_exe,
        "created_at": now(),
        "handoff_token": token,
        "chat_url": chat_url,
        "state": OPEN,
        "terminal_outcome": None,
        "terminal_error": None,
        "terminal_detail": None,
        "report": None,
        "send_attempts": 0,
        "submission_verified": False,
        "verified_at": None,
        "last_error": None,
    }
    path = obligation_path(root, worker_id)
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing.get("worker_id") != worker_id or existing.get("handoff_token") != token:
            raise ValueError("handoff obligation identity conflict")
        return existing
    atomic_json(path, value)
    return value


def load_obligation(root: Path, worker_id: str) -> dict[str, Any]:
    path = obligation_path(root, worker_id)
    return json.loads(path.read_text(encoding="utf-8"))


def update_obligation(root: Path, worker_id: str, **changes: Any) -> dict[str, Any]:
    path = obligation_path(root, worker_id)
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("worker_id") != worker_id:
        raise ValueError("handoff obligation identity mismatch")
    value.update(changes)
    atomic_json(path, value)
    return value


def mark_result_ready(root: Path, worker_id: str, *, outcome: str, detail: str,
                      error: str | None, report: str, branch: str | None,
                      baseline_sha: str | None, remote_head: str | None,
                      ending_sha: str | None = None, changed_files: str | None = None,
                      exit_code: int | None = None) -> dict[str, Any]:
    return update_obligation(root, worker_id, state=RESULT_READY,
                             terminal_outcome=outcome, terminal_detail=detail,
                             terminal_error=error, report=report, branch=branch,
                             baseline_sha=baseline_sha, remote_head=remote_head,
                             ending_sha=ending_sha, changed_files=changed_files,
                             exit_code=exit_code,
                             last_error=None)


def _verify_existing(sender: Any, token: str) -> bool:
    verifier = getattr(sender, "verify_token", None)
    if not callable(verifier):
        return False
    try:
        return bool(verifier(token))
    except Exception:
        return False


def attempt_handoff(root: Path, worker_id: str, sender: Any) -> dict[str, Any]:
    value = load_obligation(root, worker_id)
    if value.get("state") == VERIFIED and value.get("submission_verified") is True:
        return value
    token = str(value.get("handoff_token", ""))
    if value.get("state") == SENDING and _verify_existing(sender, token):
        return update_obligation(root, worker_id, state=VERIFIED,
                                 submission_verified=True, verified_at=now(),
                                 last_error=None)
    report = value.get("report")
    if not isinstance(report, str) or not report:
        raise ValueError("terminal handoff report is missing")
    attempts = int(value.get("send_attempts", 0)) + 1
    update_obligation(root, worker_id, state=SENDING, send_attempts=attempts)
    try:
        submission = sender.submit(report)
    except Exception as exc:
        submission = type("Submission", (), {"ok": False, "verified": False, "detail": type(exc).__name__})()
    if bool(getattr(submission, "ok", False)) and bool(getattr(submission, "verified", False)):
        return update_obligation(root, worker_id, state=VERIFIED,
                                 submission_verified=True, verified_at=now(),
                                 last_error=None, submission_detail=str(getattr(submission, "detail", "")))
    return update_obligation(root, worker_id, state=RESULT_READY,
                             submission_verified=False,
                             last_error=str(getattr(submission, "detail", "handoff not verified")))


def recover_pending_handoffs_once(config: Any, *, sender: Any | None = None) -> list[dict[str, Any]]:
    """Recover only terminal obligations whose owner is no longer alive."""
    root = Path(config.local_project_storage)
    directory = root / "handoff_obligations"
    if not directory.exists():
        return []
    if sender is None:
        from .handoff import CommandHandoffSender
        sender = CommandHandoffSender(config)
    recovered: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            worker_id = str(value["worker_id"])
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            continue
        if value.get("state") == VERIFIED:
            continue
        owner = {"worker_id": worker_id, "pid": value.get("worker_pid"), "exe": value.get("worker_exe")}
        if owner.get("pid") and exact_owner_live(owner):
            continue
        if not value.get("report"):
            detail = "worker terminated before terminal outcome was observed"
            report = build_actionable_report(
                run_id=str(value["run_id"]), step=int(value["step"]),
                project_id=str(value["project_id"]), channel_id=str(value["channel_id"]),
                lease_id=worker_id, worker_id=worker_id,
                handoff_token=str(value["handoff_token"]), repository=str(value["repository"]),
                branch=str(value.get("branch") or "UNKNOWN"),
                baseline_sha=str(value.get("baseline_sha") or "UNKNOWN"),
                remote_head=str(value.get("remote_head") or "UNKNOWN"),
                tests="worker-terminal-state-recovered", summary=detail,
                blockers=detail, next_boundary="audit recovered terminal result",
                status="WORKER_FAILED", error=detail)
            mark_result_ready(root, worker_id, outcome="WORKER_INTERNAL_EXCEPTION",
                              detail=detail, error=detail, report=report,
                              branch=value.get("branch"), baseline_sha=value.get("baseline_sha"),
                              remote_head=value.get("remote_head"))
        recovered.append(attempt_handoff(root, worker_id, sender))
    return recovered
