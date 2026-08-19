from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Callable
from uuid import uuid4

from .handoff import build_actionable_report, CommandHandoffSender, update_watchdog_startup_evidence, write_evidence
from .obligations import attempt_handoff, create_obligation, mark_result_ready, update_obligation
from .ownership import exact_owner_live
from .storage import Ledger, StateStore, now


@dataclass(frozen=True)
class WorkerOutcome:
    ok: bool
    detail: str


class OneShotWorker:
    """Own exactly one staged task and terminate after one bounded execution."""

    def __init__(self, config, *, executor: Callable[[str | Path, Path], WorkerOutcome] | None = None, handoff_sender=None, watchdog_spawn: Callable[[int, str], object] | None = None):
        self.config = config
        self.store = StateStore(config.local_project_storage)
        self.ledger = Ledger(config.local_project_storage)
        self._uses_default_executor = executor is None
        self.executor = executor or self._subprocess_executor
        self.handoff_sender = handoff_sender or CommandHandoffSender(config)
        self.watchdog_spawn = watchdog_spawn
        self._current_owner: dict | None = None

    def claim(self, *, run_id: str, step: int, staged_path: Path, worker_id: str | None = None, message_id: str | None = None, content_hash: str | None = None) -> dict:
        state = self.store.load()
        if state.get("stop_requested"):
            raise RuntimeError("relay is stopped")
        existing = state.get("active_worker")
        if existing and exact_owner_live(existing):
            if not (worker_id and existing.get("worker_id") == worker_id and existing.get("pid") == os.getpid()):
                raise RuntimeError("another worker owns the project")
        pending = state.get("pending_worker") or state.get("dispatch_intent")
        if worker_id:
            for _ in range(20):
                if pending:
                    break
                time.sleep(0.05)
                state = self.store.load()
                pending = state.get("pending_worker") or state.get("dispatch_intent")
            if not pending or pending.get("worker_id") != worker_id or pending.get("run_id") != run_id or int(pending.get("step", -1)) != step:
                raise RuntimeError("worker launch claim was not pending")
            if pending.get("pid") is not None and pending.get("pid") != os.getpid():
                raise RuntimeError("worker PID does not match launch owner")
            message_id = message_id or pending.get("message_id")
            content_hash = content_hash or pending.get("content_hash")
        try:
            manifest = json.loads((Path(staged_path) / "manifest.json").read_text(encoding="utf-8"))
            staged_protocol = manifest.get("protocol") or {}
        except (OSError, json.JSONDecodeError, AttributeError) as exc:
            raise RuntimeError("staged manifest is missing or malformed") from exc
        if (
            staged_protocol.get("project_id") != self.config.project_id
            or staged_protocol.get("run_id") != run_id
            or int(staged_protocol.get("step", -1)) != step
            or int(staged_protocol.get("parent", -1)) != int((pending or {}).get("parent", step - 1))
        ):
            raise RuntimeError("worker claim identity does not match staged protocol")
        if worker_id and (
            staged_protocol.get("decision_id", "") != (pending or {}).get("decision_id", "")
            or staged_protocol.get("work_order_id", "") != (pending or {}).get("work_order_id", "")
        ):
            raise RuntimeError("worker claim decision identity does not match dispatch intent")
        if staged_protocol.get("version") == 2 and content_hash and manifest.get("content_sha256") != content_hash:
            raise RuntimeError("worker claim content hash does not match staged manifest")
        expected_work_order_hash = (pending or {}).get("work_order_hash")
        if staged_protocol.get("version") == 2 and expected_work_order_hash and manifest.get("work_order_sha256") != expected_work_order_hash:
            raise RuntimeError("worker claim work-order hash does not match dispatch intent")
        owner = {"worker_id": worker_id or str(uuid4()), "pid": os.getpid(), "project_id": self.config.project_id, "run_id": run_id, "step": step, "parent": int((pending or {}).get("parent", step - 1)), "decision_id": (pending or {}).get("decision_id", ""), "work_order_id": (pending or {}).get("work_order_id", ""), "work_order_hash": (pending or {}).get("work_order_hash", ""), "post_completion": (pending or {}).get("post_completion", ""), "started_at": now(), "exe": Path(sys.executable).name}
        owner["handoff_token"] = f"AR-HANDOFF-{owner['worker_id']}"
        owner.update({"codex_status": "NOT_STARTED", "codex_pid": None, "codex_exe": None})
        state.update({"mode": "BUSY", "active_worker": owner, "pending_worker": None, "last_error": None})
        # A managed ``run-agent`` has no Gmail message id, but it still owns a
        # real logical cursor.  Advance that cursor only after the child has
        # claimed the pending owner; ordinary Gmail workers retain the
        # consumed-message acknowledgement semantics.
        managed_entry = os.environ.get("AGENT_RELAY_MANAGED_AGENT") == "1"
        if message_id or managed_entry:
            consumed = set(state.get("consumed_message_ids", [])); consumed.add(message_id)
            state["consumed_message_ids"] = sorted(item for item in consumed if item)
            state["current_run"] = run_id
            state["expected_step"] = step + 1
            state["expected_parent"] = owner["step"]
            state["logical_hashes"][f"{run_id}:{step:04d}"] = content_hash or ""
        self.store.save(state)
        self.ledger.append("worker_claimed", worker_id=owner["worker_id"], run_id=run_id, step=step)
        try:
            create_obligation(self.config.local_project_storage, worker_id=owner["worker_id"], run_id=run_id,
                              step=step, parent=owner["parent"], project_id=self.config.project_id,
                              channel_id=self.config.channel_id, repository=str(self.config.repo_path),
                              chat_url=self.config.chat_url, message_id=message_id,
                              content_hash=content_hash, worker_pid=owner["pid"],
                              worker_exe=owner["exe"], decision_id=owner.get("decision_id") or None,
                              work_order_id=owner.get("work_order_id") or None,
                              work_order_hash=owner.get("work_order_hash") or None,
                              post_completion=owner.get("post_completion") or None,
                              further_work_requires_new_decision=bool(owner.get("work_order_id")))
        except Exception:
            rollback = self.store.load()
            if rollback.get("active_worker", {}).get("worker_id") == owner["worker_id"]:
                rollback.update({"active_worker": None, "mode": "IDLE"})
                self.store.save(rollback)
            self.ledger.append("handoff_obligation_create_failed", worker_id=owner["worker_id"], run_id=run_id, step=step)
            raise
        return owner

    def run(self, *, run_id: str, step: int, staged_path: Path, worker_id: str | None = None, message_id: str | None = None, content_hash: str | None = None) -> WorkerOutcome:
        owner = self.claim(run_id=run_id, step=step, staged_path=staged_path, worker_id=worker_id, message_id=message_id, content_hash=content_hash)
        self._current_owner = owner
        outcome = WorkerOutcome(False, "worker did not complete")
        baseline_sha: str | None = None
        branch: str | None = None
        remote_head: str | None = None
        ending_sha: str | None = None
        changed_files: str | None = None
        exit_code: int | None = None
        terminal_kind = "WORKER_INTERNAL_EXCEPTION"
        terminal_error: str | None = None
        lifecycle_ok = False
        try:
            try:
                baseline_sha = self._git_value("rev-parse", "HEAD")
                branch = self._git_value("branch", "--show-current")
                try:
                    remote_head = self._git_value("rev-parse", "origin/main")
                except Exception:
                    remote_head = None
            except Exception as exc:
                terminal_kind = "GIT_PROVENANCE_FAILED"
                terminal_error = type(exc).__name__
                raise
            instruction_path = staged_path / "worker_instruction.txt"
            if not instruction_path.is_file():
                instruction_path = staged_path / "message.txt"
            instruction = instruction_path.read_text(encoding="utf-8")
            # Test/in-process executors retain the historical text contract.  The
            # production Codex executor receives only the staged directory so the
            # authoritative instruction never expands the worker argv.
            executor_input = staged_path if self._uses_default_executor else instruction
            outcome = self.executor(executor_input, self.config.repo_path)
            try:
                ending_sha = self._git_value("rev-parse", "HEAD")
                try:
                    remote_head = self._git_value("rev-parse", "origin/main")
                except Exception:
                    # A non-main guinea-pig branch may not have origin/main;
                    # preserve the explicit UNKNOWN provenance instead of
                    # claiming a remote verification that did not occur.
                    remote_head = None
                status_result = subprocess.run(["git", "-C", str(self.config.repo_path), "status", "--short"], capture_output=True, text=True, timeout=15, check=False)
                changed_files = (status_result.stdout or "").strip() or "clean"
            except Exception:
                ending_sha = baseline_sha
                changed_files = "UNKNOWN"
            active_after = self.store.load().get("active_worker") or {}
            exit_code = active_after.get("codex_exit_code")
            if outcome.ok:
                terminal_kind = "SUCCESS"
            else:
                terminal_kind = self._failure_kind(outcome.detail)
                terminal_error = outcome.detail
        except Exception as exc:
            outcome = WorkerOutcome(False, f"{type(exc).__name__}: {exc}")
            terminal_error = terminal_error or type(exc).__name__
        finally:
            obligation = {"state": "RESULT_READY", "submission_verified": False}
            continuation_ok = True
            try:
                report = self._build_terminal_report(run_id, step, owner, outcome, terminal_kind,
                                                     terminal_error, branch, baseline_sha, remote_head,
                                                     ending_sha, changed_files, exit_code)
                mark_result_ready(self.config.local_project_storage, owner["worker_id"],
                                  outcome=terminal_kind, detail=outcome.detail,
                              error=terminal_error, report=report, branch=branch,
                              baseline_sha=baseline_sha, remote_head=remote_head,
                              ending_sha=ending_sha, changed_files=changed_files,
                              exit_code=exit_code)
                obligation = attempt_handoff(self.config.local_project_storage, owner["worker_id"], self.handoff_sender)
                verified = obligation.get("state") == "VERIFIED" and obligation.get("submission_verified") is True
                if verified:
                    self.ledger.append("terminal_handoff_verified", worker_id=owner["worker_id"], step=step, outcome=terminal_kind)
                    try:
                        write_evidence(self.config.local_project_storage, lease_id=owner["worker_id"], worker_id=owner["worker_id"], handoff_token=owner["handoff_token"], chat_url=self.config.chat_url, send_attempts=1, submission_verified=True, watchdog_startup_verified=None)
                    except ValueError:
                        pass
                    if self.watchdog_spawn:
                        try:
                            launch = self.watchdog_spawn(step, run_id)
                            watchdog_verified = bool(launch.get("started")) if isinstance(launch, dict) else launch is not False
                            watchdog_detail = str(launch.get("detail", "")) if isinstance(launch, dict) else ""
                        except Exception as exc:
                            watchdog_verified = False
                            watchdog_detail = f"{type(exc).__name__}: {exc}"
                        try:
                            update_watchdog_startup_evidence(self.config.local_project_storage, owner["worker_id"], watchdog_verified, watchdog_detail)
                        except ValueError:
                            pass
                        self.ledger.append("watchdog_start_confirmed" if watchdog_verified else "watchdog_start_failed", worker_id=owner["worker_id"], step=step, detail=watchdog_detail)
                        continuation_ok = watchdog_verified
                        try:
                            update_obligation(self.config.local_project_storage, owner["worker_id"],
                                              followup_owner_started=watchdog_verified)
                        except Exception:
                            continuation_ok = False
                else:
                    self.ledger.append("terminal_handoff_pending", worker_id=owner["worker_id"], step=step, outcome=terminal_kind)
                lifecycle_ok = terminal_kind == "SUCCESS" and verified and continuation_ok
                self.ledger.append("worker_completed" if lifecycle_ok else "worker_failed", worker_id=owner["worker_id"], step=step, reason=None if lifecycle_ok else (terminal_kind if not continuation_ok else terminal_kind))
                state = self.store.load()
                state["last_error"] = None if lifecycle_ok else ("FOLLOWUP_OWNER_FAILED" if verified and not continuation_ok else (terminal_error or "terminal handoff pending"))
                self.store.save(state)
            except Exception as exc:
                self.ledger.append("terminal_handoff_failed", worker_id=owner["worker_id"], step=step, reason=type(exc).__name__)
                try:
                    state = self.store.load()
                    state["last_error"] = f"HANDOFF_FAILED: {type(exc).__name__}"
                    self.store.save(state)
                except Exception:
                    pass
            finally:
                state = self.store.load()
                if state.get("active_worker", {}).get("worker_id") == owner["worker_id"]:
                    if lifecycle_ok and owner.get("work_order_id") and owner.get("post_completion") == "RETURN_FOR_AUDIT":
                        state["mode"] = "AWAITING_AUDIT"
                        state.setdefault("work_orders", {}).setdefault(owner["work_order_id"], {}).update({"state": "COMPLETED", "completed_at": now()})
                        if owner.get("decision_id"):
                            state.setdefault("decisions", {}).setdefault(owner["decision_id"], {}).update({"state": "COMPLETED", "completed_at": now()})
                    else:
                        state["mode"] = "IDLE" if not state.get("stop_requested") else "STOPPED"
                    state["active_worker"] = None
                    self.store.save(state)
                    self.ledger.append("worker_exited", worker_id=owner["worker_id"], step=step)
                self._current_owner = None
        if terminal_kind == "SUCCESS" and obligation.get("state") == "VERIFIED" and continuation_ok:
            return outcome
        if not outcome.ok:
            return outcome
        return WorkerOutcome(False, "terminal handoff not verified")

    def _failure_kind(self, detail: str) -> str:
        state = self.store.load()
        active = state.get("active_worker") or {}
        status = str(active.get("codex_status", ""))
        if status == "CODEX_TIMEOUT" or "TimeoutExpired" in detail:
            return "CODEX_TIMEOUT"
        if active.get("codex_pid") and active.get("codex_exit_code") not in (None, 0):
            return "CODEX_EXIT_NONZERO"
        if status == "CODEX_START_FAILED" or not active.get("codex_pid"):
            if status == "NOT_STARTED":
                return "WORKER_INTERNAL_EXCEPTION"
            return "CODEX_PROCESS_CREATE_FAILED"
        return "WORKER_INTERNAL_EXCEPTION"

    def _build_terminal_report(self, run_id: str, step: int, owner: dict, outcome: WorkerOutcome,
                               terminal_kind: str, terminal_error: str | None,
                               branch: str | None, baseline_sha: str | None,
                               remote_head: str | None, ending_sha: str | None = None,
                               changed_files: str | None = None, exit_code: int | None = None) -> str:
        return build_actionable_report(run_id=run_id, step=step, project_id=self.config.project_id,
            channel_id=self.config.channel_id, lease_id=owner["worker_id"], worker_id=owner["worker_id"],
            handoff_token=owner["handoff_token"], repository=str(self.config.repo_path),
            branch=branch or "UNKNOWN", baseline_sha=baseline_sha or "UNKNOWN", remote_head=remote_head or "UNKNOWN",
            tests=f"terminal-outcome={terminal_kind}", summary=outcome.detail or terminal_kind,
            blockers=terminal_error or "none", next_boundary="audit terminal result and send next Gmail",
            status="WORK_COMPLETED" if terminal_kind == "SUCCESS" else "WORKER_FAILED", error=terminal_error,
            starting_sha=baseline_sha, ending_sha=ending_sha, exit_code=exit_code,
            terminal_outcome=terminal_kind, changed_files=changed_files)

    def _record_codex(self, status: str, *, pid: int | None = None, exe: str | None = None, error: str | None = None, exit_code: int | None = None) -> None:
        owner = self._current_owner
        if not owner:
            return
        state = self.store.load()
        active = state.get("active_worker")
        if not isinstance(active, dict) or active.get("worker_id") != owner.get("worker_id"):
            return
        active["codex_status"] = status
        if pid is not None:
            active["codex_pid"] = pid
        if exe is not None:
            active["codex_exe"] = exe
        if error is not None:
            active["codex_error"] = error
        if exit_code is not None:
            active["codex_exit_code"] = exit_code
        state["active_worker"] = active
        self.store.save(state)

    def _subprocess_executor(self, staged_path: str | Path, repo_path: Path) -> WorkerOutcome:
        command = getattr(self.config, "codex_command", "codex")
        staged_path = Path(staged_path).resolve()
        staged_instruction = staged_path / "worker_instruction.txt"
        if not staged_instruction.is_file():
            staged_instruction = staged_path / "message.txt"
        if not staged_instruction.is_file():
            return WorkerOutcome(False, f"staged instruction missing: {staged_path}")
        # Keep argv bounded and deterministic.  Codex supports ``-`` as a
        # stdin prompt, so the staged file is the sole authority and this
        # bootstrap intentionally contains no copy of its body.
        prompt = (
            "Read the authoritative staged instruction at "
            f"{staged_instruction}. Treat that file as the sole task authority. "
            "Perform only its staged local repository task, requested checks, "
            "commit and push, verify HEAD equals origin/main, then return a "
            "concise report and exit normally to AgentRelay. Do not run pytest "
            "or unittest discovery unless that staged task explicitly contains "
            "PYTEST_EXPLICITLY_AUTHORIZED. OneShotWorker owns the normal "
            "post-exit ChatGPT handoff and watchdog startup, so do not wait for "
            "Gmail or ChatGPT. For routine engineering failures, use the "
            "existing automated recovery/handoff path rather than asking a user "
            "to relay messages. Preserve staged-file prompt transport; never "
            "copy staged task bodies into process argv."
        )
        owner = self._current_owner or {}
        self._record_codex("CODEX_STARTING")
        self.ledger.append("codex_starting", worker_id=owner.get("worker_id"), step=owner.get("step"))
        codex_exe = "cmd.exe" if os.name == "nt" and str(command).lower().endswith((".cmd", ".bat")) else Path(str(command)).name
        try:
            # Codex exposes these as mutually-exclusive approval modes.  Use the
            # bounded non-interactive approval flag alone; it grants the worker's
            # workspace scope without producing an invalid invocation.
            process = subprocess.Popen([command, "exec", "--approve-for-me", "-"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=repo_path, text=True, close_fds=True)
            self._record_codex("CODEX_STARTED", pid=process.pid, exe=codex_exe)
            self.ledger.append("codex_started", worker_id=owner.get("worker_id"), step=owner.get("step"), codex_pid=process.pid)
            stdout, stderr = process.communicate(prompt, timeout=3600)
            self._record_codex("CODEX_EXITED", exit_code=process.returncode)
            self.ledger.append("codex_exited", worker_id=owner.get("worker_id"), step=owner.get("step"), codex_pid=process.pid, exit_code=process.returncode)
            if os.environ.get("AGENT_RELAY_DIAGNOSTIC_POST_EXIT_SINK") == "1":
                diagnostic = self.config.local_project_storage / "diagnostics" / f"codex-{process.pid}.json"
                diagnostic.parent.mkdir(parents=True, exist_ok=True)
                diagnostic.write_text(json.dumps({"pid": process.pid, "returncode": process.returncode, "stdout": stdout, "stderr": stderr}, ensure_ascii=False), encoding="utf-8")
            result = type("Completed", (), {"returncode": process.returncode, "stdout": stdout, "stderr": stderr})()
        except subprocess.TimeoutExpired as exc:
            try:
                process.kill()
            except (UnboundLocalError, OSError):
                pass
            self._record_codex("CODEX_TIMEOUT", error=type(exc).__name__)
            self.ledger.append("codex_timeout", worker_id=owner.get("worker_id"), step=owner.get("step"), reason=type(exc).__name__)
            return WorkerOutcome(False, type(exc).__name__)
        except OSError as exc:
            self._record_codex("CODEX_START_FAILED", error=type(exc).__name__)
            self.ledger.append("codex_start_failed", worker_id=owner.get("worker_id"), step=owner.get("step"), reason=type(exc).__name__)
            return WorkerOutcome(False, type(exc).__name__)
        detail = (result.stdout or result.stderr or "").strip()[-4000:]
        return WorkerOutcome(result.returncode == 0, detail)

    def _git_value(self, *args: str) -> str:
        result = subprocess.run(["git", "-C", str(self.config.repo_path), *args], capture_output=True, text=True, timeout=15, check=False)
        value = (result.stdout or "").strip()
        if result.returncode != 0 or not value:
            raise RuntimeError(f"git provenance unavailable: {' '.join(args)}")
        return value

class ProcessWorkerLauncher:
    """Launch one detached worker process; the worker owns its own exact PID."""

    def __init__(self, python: str | None = None):
        self.python = python or sys.executable

    def launch(self, *, staged_path: Path, envelope, content_hash: str, message_id: str, worker_id: str | None = None) -> dict:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        worker_id = worker_id or str(uuid4())
        process = subprocess.Popen([self.python, "-m", "agent_relay.cli", "worker", "--run", envelope.run_id, "--step", str(envelope.step), "--staged", str(staged_path), "--worker-id", worker_id, "--message-id", message_id, "--content-hash", content_hash], cwd=Path.cwd(), creationflags=flags, close_fds=True)
        return {"worker_id": worker_id, "pid": process.pid, "project_id": envelope.project_id, "run_id": envelope.run_id, "step": envelope.step, "parent": envelope.parent, "decision_id": envelope.decision_id, "work_order_id": envelope.work_order_id, "started_at": now(), "exe": Path(self.python).name}
