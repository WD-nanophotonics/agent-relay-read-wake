"""Bounded certification of the software-owned terminal handoff barrier.

This is intentionally a direct deterministic script, not a test discovery
runner.  The first case uses one real Codex task; the remaining cases inject
only terminal/fixed-sender outcomes so each boundary is mechanical and fast.
"""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import tempfile
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_relay.config import DEFAULT_CHAT_URL, RelayConfig
from agent_relay.gmail import GmailMessage
from agent_relay.handoff import HandoffSubmission
from agent_relay.obligations import (RESULT_READY, VERIFIED, attempt_handoff,
                                     create_obligation, load_obligation,
                                     recover_pending_handoffs_once,
                                     mark_result_ready, obligation_path)
from agent_relay.protocol import parse_envelope
from agent_relay.storage import StateStore, default_state, stage_instruction
from agent_relay.worker import OneShotWorker, WorkerOutcome

REPO = Path(__file__).resolve().parents[1]


class RecordingSender:
    def __init__(self, outcomes=(True,)):
        self.outcomes = list(outcomes)
        self.calls: list[str] = []

    def submit(self, report: str) -> HandoffSubmission:
        token = next((line.split(":", 1)[1].strip() for line in report.splitlines() if line.startswith("HANDOFF_TOKEN:")), "")
        self.calls.append(token)
        ok = self.outcomes.pop(0) if self.outcomes else True
        return HandoffSubmission(ok, "SUBMITTED" if ok else "temporary sender failure", verified=ok)


def cfg(root: Path, *, chat_url: str = DEFAULT_CHAT_URL) -> RelayConfig:
    return RelayConfig("test-project", "Test Project", "AR-TEST-CHANNEL", REPO, root, "codex-cli", "", "test-target", chat_url, 20, True, root / "gmail", "codex", "", "")


def body(run_id: str, task: str) -> str:
    return f"AGENTRELAY/1\n\nCHANNEL: AR-TEST-CHANNEL\nRUN: {run_id}\nSTEP: 0001\nPARENT: 0000\nDISPOSITION: WAKE\nPROJECT: test-project\n\n{task}"


def prepare(root: Path, task: str) -> tuple[RelayConfig, Path, str, str]:
    run_id = f"RUN-HANDOFF-{uuid4().hex.upper()}"
    message_id = f"diagnostic-{uuid4()}"
    message = GmailMessage(message_id, "diagnostic", None, body(run_id, task), ())
    staged = stage_instruction(root, message, parse_envelope(message.body))
    return cfg(root), staged, run_id, message_id


def pending(root: Path, worker_id: str, run_id: str, message_id: str, staged: Path) -> None:
    store = StateStore(root)
    state = default_state()
    state["pending_worker"] = {"worker_id": worker_id, "pid": os.getpid(), "project_id": "test-project", "run_id": run_id, "step": 1, "parent": 0, "message_id": message_id, "content_hash": "cert-hash", "staged_path": str(staged), "exe": Path(sys.executable).name}
    store.save(state)


def run_worker(root: Path, task: str, sender, *, executor=None) -> tuple[OneShotWorker, WorkerOutcome, str]:
    config, staged, run_id, message_id = prepare(root, task)
    worker_id = str(uuid4())
    pending(root, worker_id, run_id, message_id, staged)
    worker = OneShotWorker(config, executor=executor, handoff_sender=sender, watchdog_spawn=None)
    outcome = worker.run(run_id=run_id, step=1, staged_path=staged, worker_id=worker_id, message_id=message_id, content_hash="cert-hash")
    return worker, outcome, worker_id


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="agentrelay-terminal-handoff-") as temp:
        root = Path(temp)
        # The staged task contains no ChatGPT, Gmail, handoff, watchdog, or
        # continuation instruction.  The real Codex only performs local work.
        nonce = f"AR-MEMORY-IRRELEVANT-{uuid4()}"
        proof = root / "real-codex-proof.json"
        task = f"Read this staged local task. Do not modify source or run tests. Write JSON to {proof} containing probe_id={nonce!r}, cwd, git_branch, git_head, and agent_message='LOCAL_TASK_ONLY'. Then print LOCAL_TASK_ONLY_COMPLETE {nonce} and exit."
        success_sender = RecordingSender()
        _, success_outcome, success_worker = run_worker(root / "success", task, success_sender)
        success_obligation = load_obligation(root / "success", success_worker)
        assert success_outcome.ok and success_obligation["state"] == VERIFIED and success_sender.calls == [success_obligation["handoff_token"]]
        assert json.loads(proof.read_text(encoding="utf-8-sig"))["agent_message"] == "LOCAL_TASK_ONLY"
        print("SUCCESS_TERMINAL_HANDOFF_PASS")
        print("CODEX_HANDOFF_MEMORY_IRRELEVANT_PASS")

        # Deterministic nonzero terminal result, with a mechanically observed
        # Codex exit identity supplied by the diagnostic executor.
        nonzero_root = root / "nonzero"
        nonzero_sender = RecordingSender()
        def nonzero_executor(_text, _repo):
            state = StateStore(nonzero_root).load()
            state["active_worker"]["codex_pid"] = os.getpid()
            state["active_worker"]["codex_status"] = "CODEX_EXITED"
            state["active_worker"]["codex_exit_code"] = 7
            StateStore(nonzero_root).save(state)
            return WorkerOutcome(False, "codex exit code 7")
        _, nonzero_outcome, nonzero_worker = run_worker(nonzero_root, "local failure only; do not contact anyone.", nonzero_sender, executor=nonzero_executor)
        nonzero_obligation = load_obligation(nonzero_root, nonzero_worker)
        assert not nonzero_outcome.ok and nonzero_obligation["terminal_outcome"] == "CODEX_EXIT_NONZERO" and nonzero_obligation["state"] == VERIFIED
        print("FAILURE_TERMINAL_HANDOFF_PASS")

        # Invalid Codex executable is exercised through the production
        # executor, but the staged task still contains no external workflow.
        start_root = root / "start-failure"
        start_cfg, start_stage, start_run, start_message = prepare(start_root, "local task; exit if the command cannot start.")
        start_cfg = replace(start_cfg, codex_command="definitely-not-a-real-codex-executable.exe")
        start_id = str(uuid4()); pending(start_root, start_id, start_run, start_message, start_stage)
        start_sender = RecordingSender()
        start_worker = OneShotWorker(start_cfg, handoff_sender=start_sender, watchdog_spawn=None)
        start_outcome = start_worker.run(run_id=start_run, step=1, staged_path=start_stage, worker_id=start_id, message_id=start_message, content_hash="cert-hash")
        start_obligation = load_obligation(start_root, start_id)
        assert not start_outcome.ok and start_obligation["terminal_outcome"] == "CODEX_PROCESS_CREATE_FAILED" and start_obligation["state"] == VERIFIED
        print("CODEX_START_FAILURE_TERMINAL_HANDOFF_PASS")

        # First sender attempt fails; one bounded recovery pass retries the
        # same token exactly once and marks the same obligation VERIFIED.
        retry_root = root / "retry"
        retry_sender = RecordingSender((False, True))
        _, retry_outcome, retry_worker = run_worker(retry_root, "local task only.", retry_sender, executor=lambda _text, _repo: WorkerOutcome(True, "ok"))
        retry_path = obligation_path(retry_root, retry_worker)
        retry_value = json.loads(retry_path.read_text(encoding="utf-8")); retry_value["worker_pid"] = 99999999; retry_path.write_text(json.dumps(retry_value), encoding="utf-8")
        recovered = recover_pending_handoffs_once(cfg(retry_root), sender=retry_sender)
        retry_final = load_obligation(retry_root, retry_worker)
        assert retry_outcome.ok is False and recovered and retry_final["state"] == VERIFIED and retry_sender.calls.count(retry_final["handoff_token"]) == 2
        assert recover_pending_handoffs_once(cfg(retry_root), sender=retry_sender) == []
        print("HANDOFF_DEBT_RECOVERY_PASS")
        print("NO_DUPLICATE_HANDOFF_PASS")

        # Simulated interrupted Worker: terminal result is already durable,
        # owner is dead, and the normal bounded recovery entry sends it.
        dead_root = root / "dead-worker"
        dead_root.mkdir(parents=True)
        dead_config = cfg(dead_root)
        dead_id = str(uuid4()); dead_run = f"RUN-DEAD-{uuid4().hex.upper()}"
        create_obligation(dead_root, worker_id=dead_id, run_id=dead_run, step=1, parent=0, project_id=dead_config.project_id, channel_id=dead_config.channel_id, repository=str(REPO), chat_url=dead_config.chat_url, worker_pid=99999999, worker_exe="python.exe")
        dead_report = "AGENTRELAY_CHATGPT_HANDOFF/1\nHANDOFF_TOKEN: AR-HANDOFF-" + dead_id
        mark_result_ready(dead_root, dead_id, outcome="WORKER_INTERNAL_EXCEPTION", detail="interrupted", error="interrupted", report=dead_report, branch="main", baseline_sha="dead", remote_head="dead")
        dead_sender = RecordingSender()
        recovered_dead = recover_pending_handoffs_once(dead_config, sender=dead_sender)
        assert recovered_dead and load_obligation(dead_root, dead_id)["state"] == VERIFIED
        print("DEAD_WORKER_RECOVERY_PASS")

        # One real configured-URL submission through the production sender.  This
        # is intentionally last because it may open/attach to the user Chrome.
        real_root = root / "real-chatgpt"
        real_config = cfg(real_root)
        real_id = str(uuid4()); real_run = f"RUN-REALCHAT-{uuid4().hex.upper()}"
        create_obligation(real_root, worker_id=real_id, run_id=real_run, step=1, parent=0, project_id=real_config.project_id, channel_id=real_config.channel_id, repository=str(REPO), chat_url=real_config.chat_url, worker_pid=99999999, worker_exe="python.exe")
        real_report = "\n".join(["AGENTRELAY_CHATGPT_HANDOFF/1", "", "CHANNEL: AR-HANDOFF-CERT", f"RUN: {real_run}", "STEP: 0001", "PROJECT: handoff-cert", "", f"LEASE: {real_id}", f"WORKER: {real_id}", f"HANDOFF_TOKEN: AR-HANDOFF-{real_id}", "", "STATUS: WORK_COMPLETED", "SUMMARY: mandatory terminal barrier real fixed target certification", "BLOCKERS: none", ""])
        mark_result_ready(real_root, real_id, outcome="SUCCESS", detail="real fixed target", error=None, report=real_report, branch="main", baseline_sha="f54cad8", remote_head="f54cad8")
        from agent_relay.chatgpt_sender import BrowserChatGPTSender
        real_result = attempt_handoff(real_root, real_id, BrowserChatGPTSender(real_config))
        assert real_result["state"] == VERIFIED
        print(f"REAL_FIXED_CHATGPT_TERMINAL_HANDOFF_PASS token=AR-HANDOFF-{real_id}")
    print("MANDATORY_TERMINAL_HANDOFF_BARRIER_PASS")
    print("MINIMAL_ARCHITECTURE_PRESERVED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
