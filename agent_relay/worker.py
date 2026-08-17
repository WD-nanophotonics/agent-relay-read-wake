from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Callable
from uuid import uuid4

from .handoff import build_actionable_report, CommandHandoffSender, update_watchdog_startup_evidence, write_evidence
from .ownership import exact_owner_live
from .storage import Ledger, StateStore, now


@dataclass(frozen=True)
class WorkerOutcome:
    ok: bool
    detail: str


class OneShotWorker:
    """Own exactly one staged task and terminate after one bounded execution."""

    def __init__(self, config, *, executor: Callable[[str, Path], WorkerOutcome] | None = None, handoff_sender=None, watchdog_spawn: Callable[[int, str], object] | None = None):
        self.config = config
        self.store = StateStore(config.local_project_storage)
        self.ledger = Ledger(config.local_project_storage)
        self.executor = executor or self._subprocess_executor
        self.handoff_sender = handoff_sender or CommandHandoffSender(config)
        self.watchdog_spawn = watchdog_spawn

    def claim(self, *, run_id: str, step: int, staged_path: Path, worker_id: str | None = None, message_id: str | None = None, content_hash: str | None = None) -> dict:
        state = self.store.load()
        if state.get("stop_requested"):
            raise RuntimeError("relay is stopped")
        existing = state.get("active_worker")
        if existing and exact_owner_live(existing):
            if not (worker_id and existing.get("worker_id") == worker_id and existing.get("pid") == os.getpid()):
                raise RuntimeError("another worker owns the project")
        pending = state.get("pending_worker")
        if worker_id:
            for _ in range(20):
                if pending:
                    break
                time.sleep(0.05)
                state = self.store.load()
                pending = state.get("pending_worker")
            if not pending or pending.get("worker_id") != worker_id or pending.get("run_id") != run_id or int(pending.get("step", -1)) != step:
                raise RuntimeError("worker launch claim was not pending")
            if pending.get("pid") != os.getpid():
                raise RuntimeError("worker PID does not match launch owner")
            message_id = message_id or pending.get("message_id")
            content_hash = content_hash or pending.get("content_hash")
        owner = {"worker_id": worker_id or str(uuid4()), "pid": os.getpid(), "project_id": self.config.project_id, "run_id": run_id, "step": step, "parent": int((pending or {}).get("parent", step - 1)), "started_at": now(), "exe": Path(sys.executable).name}
        state.update({"mode": "BUSY", "active_worker": owner, "pending_worker": None, "last_error": None})
        if message_id:
            consumed = set(state.get("consumed_message_ids", [])); consumed.add(message_id)
            state["consumed_message_ids"] = sorted(consumed)
            state["current_run"] = run_id
            state["expected_step"] = step + 1
            state["expected_parent"] = owner["step"]
            state["logical_hashes"][f"{run_id}:{step:04d}"] = content_hash or ""
        self.store.save(state)
        self.ledger.append("worker_claimed", worker_id=owner["worker_id"], run_id=run_id, step=step)
        return owner

    def run(self, *, run_id: str, step: int, staged_path: Path, worker_id: str | None = None, message_id: str | None = None, content_hash: str | None = None) -> WorkerOutcome:
        owner = self.claim(run_id=run_id, step=step, staged_path=staged_path, worker_id=worker_id, message_id=message_id, content_hash=content_hash)
        outcome = WorkerOutcome(False, "worker did not complete")
        baseline_sha = ""
        try:
            baseline_sha = self._git_value("rev-parse", "HEAD")
            instruction = (staged_path / "message.txt").read_text(encoding="utf-8")
            outcome = self.executor(instruction, self.config.repo_path)
            if not outcome.ok:
                raise RuntimeError(outcome.detail)
            self._write_handoff(run_id, step, owner, outcome.detail, baseline_sha=baseline_sha)
            self.ledger.append("worker_completed", worker_id=owner["worker_id"], step=step)
            if self.watchdog_spawn:
                try:
                    launch = self.watchdog_spawn(step, run_id)
                    verified = bool(launch.get("started")) if isinstance(launch, dict) else launch is not False
                    detail = str(launch.get("detail", "")) if isinstance(launch, dict) else ""
                except Exception as exc:
                    verified = False
                    detail = f"{type(exc).__name__}: {exc}"
                update_watchdog_startup_evidence(self.config.local_project_storage, owner["worker_id"], verified, detail)
                self.ledger.append("watchdog_start_confirmed" if verified else "watchdog_start_failed", worker_id=owner["worker_id"], step=step, detail=detail)
            return outcome
        except Exception as exc:
            outcome = WorkerOutcome(False, f"{type(exc).__name__}: {exc}")
            state = self.store.load()
            state["last_error"] = outcome.detail
            self.store.save(state)
            self.ledger.append("worker_failed", worker_id=owner["worker_id"], step=step, reason=type(exc).__name__)
            return outcome
        finally:
            state = self.store.load()
            if state.get("active_worker", {}).get("worker_id") == owner["worker_id"]:
                state.update({"active_worker": None, "mode": "IDLE" if not state.get("stop_requested") else "STOPPED"})
                self.store.save(state)
                self.ledger.append("worker_exited", worker_id=owner["worker_id"], step=step)

    def _subprocess_executor(self, instruction: str, repo_path: Path) -> WorkerOutcome:
        command = getattr(self.config, "codex_command", "codex.cmd")
        prompt = instruction + "\n\nBounded worker contract: complete this task, run the requested checks, commit and push, verify HEAD equals origin/main, then return a concise report. Do not wait for Gmail or ChatGPT."
        try:
            # Codex exposes these as mutually-exclusive approval modes.  Use the
            # bounded non-interactive approval flag alone; it grants the worker's
            # workspace scope without producing an invalid invocation.
            result = subprocess.run([command, "exec", "--approve-for-me", prompt], cwd=repo_path, text=True, capture_output=True, timeout=3600, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return WorkerOutcome(False, type(exc).__name__)
        detail = (result.stdout or result.stderr or "").strip()[-4000:]
        return WorkerOutcome(result.returncode == 0, detail)

    def _git_value(self, *args: str) -> str:
        result = subprocess.run(["git", "-C", str(self.config.repo_path), *args], capture_output=True, text=True, timeout=15, check=False)
        value = (result.stdout or "").strip()
        if result.returncode != 0 or not value:
            raise RuntimeError(f"git provenance unavailable: {' '.join(args)}")
        return value

    def _write_handoff(self, run_id: str, step: int, owner: dict, detail: str, *, baseline_sha: str) -> Path:
        branch = self._git_value("branch", "--show-current")
        remote_head = self._git_value("rev-parse", "origin/main")
        report = build_actionable_report(run_id=run_id, step=step, project_id=self.config.project_id, channel_id=self.config.channel_id, lease_id=owner["worker_id"], worker_id=owner["worker_id"], handoff_token=owner["worker_id"], repository=str(self.config.repo_path), branch=branch, baseline_sha=baseline_sha, remote_head=remote_head, tests="worker-command-exit=0", summary=detail or "bounded worker completed", blockers="none", next_boundary="audit remote and send next Gmail")
        submission = self.handoff_sender.submit(report)
        if not submission.ok or not submission.verified or submission.attempts != 1:
            raise RuntimeError(f"ChatGPT handoff not verified: {submission.detail}")
        target = self.config.local_project_storage / "handoffs" / f"{owner['worker_id']}.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(report, encoding="utf-8")
        write_evidence(self.config.local_project_storage, lease_id=owner["worker_id"], worker_id=owner["worker_id"], handoff_token=owner["worker_id"], chat_url=self.config.chat_url, send_attempts=submission.attempts, submission_verified=True, watchdog_startup_verified=None)
        return target


class ProcessWorkerLauncher:
    """Launch one detached worker process; the worker owns its own exact PID."""

    def __init__(self, python: str | None = None):
        self.python = python or sys.executable

    def launch(self, *, staged_path: Path, envelope, content_hash: str, message_id: str) -> dict:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        worker_id = str(uuid4())
        process = subprocess.Popen([self.python, "-m", "agent_relay.cli", "worker", "--run", envelope.run_id, "--step", str(envelope.step), "--staged", str(staged_path), "--worker-id", worker_id, "--message-id", message_id, "--content-hash", content_hash], cwd=Path.cwd(), creationflags=flags, close_fds=True)
        return {"worker_id": worker_id, "pid": process.pid, "project_id": envelope.project_id, "run_id": envelope.run_id, "step": envelope.step, "parent": envelope.parent, "started_at": now(), "exe": Path(self.python).name}
