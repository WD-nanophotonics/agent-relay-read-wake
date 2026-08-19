"""Durable, bounded terminal-handoff obligations."""
from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import os

from .handoff import build_actionable_report, ACTION_SEND_RECOVERY
from .ownership import exact_owner_live
from .storage import atomic_json, now, StateStore, Ledger


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
                     worker_exe: str | None = None, decision_id: str | None = None,
                     work_order_id: str | None = None, work_order_hash: str | None = None,
                     post_completion: str | None = None,
                     further_work_requires_new_decision: bool | None = None) -> dict[str, Any]:
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
        "decision_id": decision_id,
        "work_order_id": work_order_id,
        "work_order_hash": work_order_hash,
        "post_completion": post_completion,
        "further_work_requires_new_decision": further_work_requires_new_decision,
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
        "managed_entry": os.environ.get("AGENT_RELAY_MANAGED_AGENT") == "1",
        "followup_owner_started": False,
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
        owner = {"worker_id": worker_id, "pid": value.get("worker_pid"), "exe": value.get("worker_exe")}
        current_state = StateStore(root).load()
        stale_active = (isinstance(current_state.get("active_worker"), dict)
                        and current_state["active_worker"].get("worker_id") == worker_id
                        and not exact_owner_live(owner))
        if value.get("state") == VERIFIED and not stale_active and (value.get("followup_owner_started") or (value.get("managed_entry") is False and value.get("message_id") is not None)):
            continue
        if owner.get("pid") and exact_owner_live(owner):
            continue
        # A claimed Worker can die after the cursor has advanced but before
        # its finally block clears active_worker.  Once exact ownership is
        # proven dead, release only that exact owner so the next bounded poll
        # is not permanently stuck in BUSY.
        state_store = StateStore(root)
        state = state_store.load()
        if isinstance(state.get("active_worker"), dict) and state["active_worker"].get("worker_id") == worker_id:
            next_mode = "AWAITING_AUDIT" if value.get("work_order_id") else "IDLE"
            state.update({"active_worker": None, "mode": next_mode})
            state_store.save(state)
            Ledger(root).append("stale_active_owner_cleared", worker_id=worker_id, pid=owner.get("pid"))
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
                status="WORKER_FAILED", error=detail,
                action_required=ACTION_SEND_RECOVERY)
            mark_result_ready(root, worker_id, outcome="WORKER_INTERNAL_EXCEPTION",
                              detail=detail, error=detail, report=report,
                              branch=value.get("branch"), baseline_sha=value.get("baseline_sha"),
                              remote_head=value.get("remote_head"))
        recovered_value = attempt_handoff(root, worker_id, sender)
        if recovered_value.get("state") == VERIFIED and (recovered_value.get("managed_entry") or recovered_value.get("message_id") is None) and not recovered_value.get("followup_owner_started"):
            try:
                from .watchdog import spawn_watchdog
                launch = spawn_watchdog(config, run_id=str(recovered_value["run_id"]), after_step=int(recovered_value["step"]))
                recovered_value = update_obligation(root, worker_id,
                    followup_owner_started=bool(launch.get("started") or launch.get("detail") == "watchdog already owned"))
            except Exception as exc:
                recovered_value = update_obligation(root, worker_id, last_error=f"FOLLOWUP_OWNER_FAILED: {type(exc).__name__}")
        recovered.append(recovered_value)
    return recovered
