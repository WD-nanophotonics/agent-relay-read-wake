from __future__ import annotations

import json
from pathlib import Path
import shlex
import subprocess
from typing import Any

from .config import EXPECTED_CHAT_URL
from .storage import atomic_json, now


PROTOCOL = "AGENTRELAY_HANDOFF/1"
RETURN_PROTOCOL = "AGENTRELAY_CHATGPT_RETURN/1"
ACTION_SEND_NEXT = "SEND_NEXT_GMAIL"
ACTION_SEND_RECOVERY = "SEND_RECOVERY_GMAIL"
ACTION_HUMAN = "HUMAN_REQUIRED"


class HandoffSubmission:
    def __init__(self, ok: bool, detail: str, *, attempts: int = 1, verified: bool = False):
        self.ok = ok
        self.detail = detail
        self.attempts = attempts
        self.verified = verified


class CommandHandoffSender:
    """Bounded bridge to the installed fixed-URL ChatGPT sender.

    The command receives ``--url <fixed-url>`` and the exact handoff envelope
    on stdin, and must print ``SUBMITTED`` after the UI/API submission is
    visibly acknowledged. It is one short-lived process, never a supervisor.
    """

    def __init__(self, config):
        self.command = str(getattr(config, "handoff_command", ""))
        self.chat_url = str(config.chat_url)

    def submit(self, report: str) -> HandoffSubmission:
        if not self.command:
            from .chatgpt_sender import BrowserChatGPTSender
            return BrowserChatGPTSender(type("Config", (), {"chat_url": self.chat_url})()).submit(report)
        if self.chat_url != EXPECTED_CHAT_URL:
            return HandoffSubmission(False, "fixed ChatGPT sender is not configured")
        try:
            result = subprocess.run([*shlex.split(self.command), "--url", self.chat_url], input=report, text=True, capture_output=True, timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return HandoffSubmission(False, type(exc).__name__)
        output = (result.stdout or "") + "\n" + (result.stderr or "")
        verified = result.returncode == 0 and "SUBMITTED" in output.upper()
        return HandoffSubmission(verified, output[-1000:].strip() or f"exit={result.returncode}", verified=verified)


def validate_return_envelope(report: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in report.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            if key.strip() in {"PROTOCOL", "CHANNEL", "RUN", "STEP", "PROJECT", "ACTION_REQUIRED", "NEXT_STEP", "NEXT_PARENT", "RESPONSE_CONTRACT", "HANDOFF_TOKEN"}:
                fields[key.strip()] = value.strip()
    required = {"PROTOCOL", "CHANNEL", "RUN", "STEP", "PROJECT", "ACTION_REQUIRED", "NEXT_STEP", "NEXT_PARENT", "RESPONSE_CONTRACT", "HANDOFF_TOKEN"}
    if set(fields) != required or fields["PROTOCOL"] != RETURN_PROTOCOL:
        raise ValueError("malformed actionable return envelope")
    if fields["ACTION_REQUIRED"] not in {ACTION_SEND_NEXT, ACTION_SEND_RECOVERY, ACTION_HUMAN}:
        raise ValueError("invalid return action")
    expected_contract = "HUMAN_REQUIRED" if fields["ACTION_REQUIRED"] == ACTION_HUMAN else "GMAIL_REQUIRED"
    if fields["RESPONSE_CONTRACT"] != expected_contract:
        raise ValueError("return response contract does not match action")
    if fields["ACTION_REQUIRED"] != ACTION_HUMAN:
        if not fields["CHANNEL"].startswith("AR-") or not fields["RUN"].startswith("RUN-") or not fields["PROJECT"] or not fields["NEXT_STEP"].isdigit() or not fields["NEXT_PARENT"].isdigit():
            raise ValueError("invalid actionable return identity")
    return fields


def build_actionable_report(*, run_id: str, step: int, project_id: str, channel_id: str, lease_id: str, worker_id: str, handoff_token: str, repository: str, branch: str, baseline_sha: str, remote_head: str, tests: str, summary: str, blockers: str, next_boundary: str, next_step: int | None = None, next_parent: int | None = None, status: str = "WORK_COMPLETED", error: str | None = None, starting_sha: str | None = None, ending_sha: str | None = None, exit_code: int | None = None, terminal_outcome: str | None = None, changed_files: str | None = None, action_required: str = ACTION_SEND_NEXT, response_contract: str = "GMAIL_REQUIRED") -> str:
    """Build the deterministic Phase 2I return-path message."""
    next_step = step + 1 if next_step is None else next_step
    next_parent = step if next_parent is None else next_parent
    instructions = (
        [
            "CHATGPT MUST:",
            "1. Audit the remote GitHub result before deciding the next task.",
            "2. Decide the next bounded task from the audited repository state.",
            "3. Generate the authoritative next AgentRelay instruction.",
            "4. SEND it immediately through Gmail in this same ChatGPT turn.",
            "5. Use the same CHANNEL / RUN / PROJECT.",
            "6. Use STEP = NEXT_STEP and PARENT = NEXT_PARENT exactly.",
            "7. Use DISPOSITION: WAKE when work should continue.",
            "8. Use DISPOSITION: HUMAN_REQUIRED if audit shows the loop must stop for human intervention.",
            "9. Do not merely draft the email.",
            "10. Do not ask the user to copy or forward anything.",
            "11. Do not wait for another user message before sending Gmail.",
            "12. After sending Gmail, end the ChatGPT turn. Do not wait for Codex.",
        ] if action_required != ACTION_HUMAN else [
            "CHATGPT MUST:",
            "1. Treat this as a genuine human-only boundary.",
            "2. Do not invent or send a follow-up Gmail until the human boundary is resolved.",
        ]
    )
    report = "\n".join([
        "AGENTRELAY_CHATGPT_HANDOFF/1",
        f"PROTOCOL: {RETURN_PROTOCOL}",
        "",
        f"CHANNEL: {channel_id}",
        f"RUN: {run_id}",
        f"STEP: {step:04d}",
        f"PROJECT: {project_id}",
        "",
        f"LEASE: {lease_id}",
        f"WORKER: {worker_id}",
        f"HANDOFF_TOKEN: {handoff_token}",
        "",
        f"REPOSITORY: {repository}",
        f"BRANCH: {branch}",
        f"BASELINE_SHA: {baseline_sha}",
        f"REMOTE_HEAD: {remote_head}",
        f"STARTING_SHA: {starting_sha or baseline_sha}",
        f"ENDING_SHA: {ending_sha or baseline_sha}",
        *( [f"EXIT_CODE: {exit_code}"] if exit_code is not None else [] ),
        *( [f"TERMINAL_OUTCOME: {terminal_outcome}"] if terminal_outcome else [] ),
        *( [f"CHANGED_FILES: {changed_files}"] if changed_files else [] ),
        "",
        f"STATUS: {status}",
        *( [f"ERROR: {error}"] if error else [] ),
        f"TESTS: {tests}",
        f"SUMMARY: {summary}",
        f"BLOCKERS: {blockers}",
        f"SUGGESTED_NEXT_BOUNDARY: {next_boundary}",
        "",
        f"ACTION_REQUIRED: {action_required}",
        f"NEXT_STEP: {next_step:04d}",
        f"NEXT_PARENT: {next_parent:04d}",
        f"RESPONSE_CONTRACT: {response_contract}",
        "",
        *instructions,
    ])
    validate_return_envelope(report)
    return report


def evidence_path(project_storage: Path, lease_id: str) -> Path:
    return project_storage / "handoffs" / f"{lease_id}.json"


def write_evidence(
    project_storage: Path,
    *,
    lease_id: str,
    worker_id: str,
    handoff_token: str,
    chat_url: str,
    send_attempts: int = 1,
    navigation_attempts: int = 1,
    verification_attempts: int = 1,
    submission_verified: bool = True,
    watchdog_startup_verified: bool | None = None,
) -> Path:
    if chat_url != EXPECTED_CHAT_URL:
        raise ValueError("handoff URL does not match the fixed ChatGPT target")
    if not handoff_token or send_attempts != 1 or not 0 <= navigation_attempts <= 2 or not 0 <= verification_attempts <= 1 or submission_verified is not True:
        raise ValueError("handoff evidence exceeds the bounded certification contract")
    target = evidence_path(project_storage, lease_id)
    if target.exists():
        raise ValueError("handoff evidence already exists for this lease")
    atomic_json(target, {
        "protocol": PROTOCOL,
        "lease_id": lease_id,
        "worker_id": worker_id,
        "handoff_token": handoff_token,
        "chat_url": chat_url,
        "send_attempts": send_attempts,
        "navigation_attempts": navigation_attempts,
        "verification_attempts": verification_attempts,
        "submission_verified": True,
        "watchdog_startup_verified": watchdog_startup_verified,
        "recorded_at": now(),
    })
    return target


def update_watchdog_startup_evidence(project_storage: Path, lease_id: str, verified: bool, detail: str = "") -> Path:
    target = evidence_path(project_storage, lease_id)
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("handoff evidence is missing or malformed") from exc
    value["watchdog_startup_verified"] = bool(verified)
    value["watchdog_startup_detail"] = detail
    atomic_json(target, value)
    return target


def validate_evidence(project_storage: Path, active: dict[str, Any], *, handoff_token: str = "") -> dict[str, Any]:
    path = evidence_path(project_storage, str(active.get("lease_id", "")))
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("handoff evidence is missing or malformed") from exc
    expected_token = handoff_token or str(active.get("handoff_token", ""))
    if (
        value.get("protocol") != PROTOCOL
        or value.get("lease_id") != active.get("lease_id")
        or value.get("worker_id") != active.get("worker_id")
        or value.get("handoff_token") != expected_token
        or value.get("chat_url") != EXPECTED_CHAT_URL
        or value.get("send_attempts") != 1
        or not isinstance(value.get("navigation_attempts"), int) or not 0 <= value["navigation_attempts"] <= 2
        or not isinstance(value.get("verification_attempts"), int) or not 0 <= value["verification_attempts"] <= 1
        or value.get("submission_verified") is not True
    ):
        raise ValueError("handoff evidence identity or bounded submission proof is invalid")
    return value
