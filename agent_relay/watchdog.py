from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import math
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any, Callable
from uuid import uuid4

from .ownership import exact_owner_live
from .storage import Ledger, StateStore, atomic_json, now

WATCHDOG_WINDOW_SECONDS = 300
POLL_INTERVAL_SECONDS = 10
MAX_POLLS = 10
POLL_TIMEOUT_SECONDS = 30
WORKER_SHUTDOWN_SECONDS = 10
NO_WAKE_SHUTDOWN_SECONDS = 30
WORKER_CLAIM_TIMEOUT_SECONDS = 30
CODEX_START_TIMEOUT_SECONDS = 30


def watchdog_status_path(root: Path, run_id: str, after_step: int) -> Path:
    return root / "watchdogs" / f"{run_id}-after-{after_step:04d}.json"


def _lock_path(root: Path, run_id: str, after_step: int) -> Path:
    return root / "watchdogs" / f"{run_id}-after-{after_step:04d}.lock"


def _status_template(*, watchdog_id: str, pid: int | None, exe: str, run_id: str, after_step: int, status: str) -> dict[str, Any]:
    stamp = now()
    return {
        "watchdog_id": watchdog_id, "pid": pid, "exe": exe, "run_id": run_id,
        "after_step": after_step, "status": status, "started_at": stamp,
        "updated_at": stamp, "poll_number": 0, "polls_completed": 0,
        "max_polls": MAX_POLLS, "poll_interval_seconds": POLL_INTERVAL_SECONDS,
        "poll_timeout_seconds": POLL_TIMEOUT_SECONDS, "next_poll_at": None,
        "service_window_seconds": WATCHDOG_WINDOW_SECONDS, "service_window_remaining_seconds": WATCHDOG_WINDOW_SECONDS,
        "countdown_seconds": None, "poll_owner_pid": None, "poll_owner_terminated": False,
        "poll_owner_termination_verified": False,
        "poll_started_at": None, "poll_elapsed_seconds": 0.0,
        "poll_finished_at": None, "poll_duration_seconds": None,
        "last_poll_at": None, "last_poll_action": None, "last_error": None,
        "finish_reason": None, "finished_at": None, "closing_countdown_seconds": None,
        "worker_pid": None, "startup_ack_at": None,
        "codex_pid": None, "worker_claim_elapsed_seconds": None,
        "codex_start_elapsed_seconds": None,
        "ui_pid": None, "ui_started_at": None, "ui_error": None,
    }


def _save_status(root: Path, run_id: str, after_step: int, status: dict[str, Any]) -> Path:
    status["updated_at"] = now()
    path = watchdog_status_path(root, run_id, after_step)
    atomic_json(path, status)
    return path


def load_watchdog_status(root: Path, run_id: str | None = None, after_step: int | None = None) -> dict[str, Any] | None:
    import json
    if run_id is not None and after_step is not None:
        paths = [watchdog_status_path(root, run_id, after_step)]
    else:
        paths = sorted((root / "watchdogs").glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True) if (root / "watchdogs").exists() else []
    for path in paths:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                return value
        except (OSError, ValueError):
            continue
    return None


@contextmanager
def exact_watchdog_lock(root: Path, run_id: str, after_step: int):
    lock = _lock_path(root, run_id, after_step)
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            text = lock.read_text(encoding="utf-8")
            pid = int(next((line.split("=", 1)[1] for line in text.splitlines() if line.startswith("pid=")), "-1"))
            exe = next((line.split("=", 1)[1] for line in text.splitlines() if line.startswith("exe=")), "")
            if exact_owner_live({"pid": pid, "exe": exe}):
                yield False
                return
        except (OSError, ValueError):
            pass
        try:
            lock.unlink()
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except (OSError, FileExistsError):
            yield False
            return
    try:
        os.write(fd, f"pid={os.getpid()}\nexe={Path(sys.executable).name}\ncreated={now()}\n".encode())
        os.close(fd)
        yield True
    finally:
        pass


def _append(ledger: Ledger, event: str, *, run_id: str, after_step: int, watchdog_id: str, pid: int, poll_number: int | None = None, **values: Any) -> None:
    ledger.append(event, run_id=run_id, after_step=after_step, watchdog_id=watchdog_id, pid=pid, poll_number=poll_number, **values)


def spawn_watchdog_ui(config) -> int:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    relay_root = Path(__file__).resolve().parents[1]
    process = subprocess.Popen([sys.executable, "-m", "agent_relay.cli", "watchdog-ui"], cwd=relay_root, creationflags=flags, close_fds=True)
    return process.pid


def spawn_watchdog(config, *, run_id: str, after_step: int) -> dict[str, Any]:
    root = config.local_project_storage
    ledger = Ledger(root)
    watchdog_id = str(uuid4())
    path = watchdog_status_path(root, run_id, after_step)
    existing = load_watchdog_status(root, run_id, after_step)
    if existing and existing.get("pid") and exact_owner_live(existing):
        return {"started": False, "watchdog_id": existing.get("watchdog_id", ""), "pid": existing.get("pid"), "status_path": str(path), "detail": "watchdog already owned"}
    status = _status_template(watchdog_id=watchdog_id, pid=None, exe=Path(sys.executable).name, run_id=run_id, after_step=after_step, status="STARTING")
    _save_status(root, run_id, after_step, status)
    _append(ledger, "watchdog_spawn_requested", run_id=run_id, after_step=after_step, watchdog_id=watchdog_id, pid=os.getpid())
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    try:
        relay_root = Path(__file__).resolve().parents[1]
        child_env = os.environ.copy()
        # The managed-entry marker belongs only to the Worker that was
        # explicitly launched by ``run-agent``.  It must not leak into the
        # follow-up Gmail Worker and turn an ordinary message into a synthetic
        # cursor transition.
        child_env.pop("AGENT_RELAY_MANAGED_AGENT", None)
        process = subprocess.Popen([sys.executable, "-m", "agent_relay.cli", "watchdog", "--run", run_id, "--after-step", str(after_step), "--watchdog-id", watchdog_id], cwd=relay_root, creationflags=flags, close_fds=True, env=child_env)
    except Exception as exc:
        status.update({"status": "FAILED", "last_error": f"Popen: {type(exc).__name__}: {exc}", "finished_at": now(), "finish_reason": "spawn_failed"})
        _save_status(root, run_id, after_step, status)
        _append(ledger, "watchdog_failed", run_id=run_id, after_step=after_step, watchdog_id=watchdog_id, pid=os.getpid(), error=status["last_error"])
        return {"started": False, "watchdog_id": watchdog_id, "pid": None, "status_path": str(path), "detail": status["last_error"]}
    status = load_watchdog_status(root, run_id, after_step) or status
    status.update({"pid": process.pid, "exe": Path(sys.executable).name})
    _save_status(root, run_id, after_step, status)
    _append(ledger, "watchdog_process_created", run_id=run_id, after_step=after_step, watchdog_id=watchdog_id, pid=process.pid)
    deadline = time.monotonic() + 5
    acknowledged = False
    while time.monotonic() < deadline:
        current = load_watchdog_status(root, run_id, after_step)
        if current and current.get("watchdog_id") == watchdog_id and current.get("startup_ack_at") and current.get("pid") == process.pid:
            acknowledged = True
            break
        if process.poll() is not None:
            break
        time.sleep(0.1)
    if not acknowledged:
        current = load_watchdog_status(root, run_id, after_step) or status
        current.update({"status": "FAILED", "last_error": "startup acknowledgement timeout", "finished_at": now(), "finish_reason": "startup_ack_timeout"})
        _save_status(root, run_id, after_step, current)
        _append(ledger, "watchdog_failed", run_id=run_id, after_step=after_step, watchdog_id=watchdog_id, pid=process.pid, error=current["last_error"])
    return {"started": acknowledged, "watchdog_id": watchdog_id, "pid": process.pid, "status_path": str(path), "detail": "STARTING acknowledged" if acknowledged else "startup acknowledgement timeout"}


def _finish(status: dict[str, Any], root: Path, run_id: str, after_step: int, ledger: Ledger, watchdog_id: str, pid: int, terminal: str, reason: str, *, return_value: str | None = None, event: str | None = None) -> str:
    status.update({"status": terminal, "finished_at": now(), "finish_reason": reason, "next_poll_at": None, "closing_countdown_seconds": None})
    _save_status(root, run_id, after_step, status)
    if event:
        _append(ledger, event, run_id=run_id, after_step=after_step, watchdog_id=watchdog_id, pid=pid, poll_number=status.get("poll_number"), reason=reason)
    _append(ledger, "watchdog_finished", run_id=run_id, after_step=after_step, watchdog_id=watchdog_id, pid=pid, poll_number=status.get("poll_number"), reason=reason)
    return return_value or reason


def _countdown(root: Path, run_id: str, after_step: int, status: dict[str, Any], seconds: int, sleep: Callable[[float], None], clock: Callable[[], float], *, state_store: StateStore | None = None) -> bool:
    end = clock() + seconds
    while True:
        remaining = max(0, int(math.ceil(end - clock())))
        status["closing_countdown_seconds"] = remaining
        _save_status(root, run_id, after_step, status)
        if remaining <= 0:
            return True
        if state_store and state_store.load().get("stop_requested"):
            return False
        sleep(1)


def _wait_for_poll(root: Path, run_id: str, after_step: int, status: dict[str, Any], sleep: Callable[[float], None], clock: Callable[[], float], state_store: StateStore, interval_seconds: int) -> bool:
    end = clock() + interval_seconds
    status["status"] = "WAITING_FOR_POLL"
    status["next_poll_at"] = (datetime.now(UTC) + timedelta(seconds=interval_seconds)).isoformat()
    while True:
        remaining = max(0, int(math.ceil(end - clock())))
        status["countdown_seconds"] = remaining
        _save_status(root, run_id, after_step, status)
        if state_store.load().get("stop_requested"):
            return False
        if remaining <= 0:
            return True
        sleep(1)


def _bounded_poll(root: Path, run_id: str, after_step: int, status: dict[str, Any], ledger: Ledger, watchdog_id: str, pid: int, poll_number: int, poll_factory, sleep: Callable[[float], None], clock: Callable[[], float], poll_timeout_seconds: int, *, poll_command: list[str] | None = None, poll_env: dict[str, str] | None = None, poll_cwd: Path | None = None) -> tuple[Any, bool]:
    status.update({"status": "POLLING", "poll_number": poll_number, "poll_started_at": now(), "poll_elapsed_seconds": 0.0, "poll_finished_at": None, "poll_duration_seconds": None, "countdown_seconds": None, "next_poll_at": None})
    _save_status(root, run_id, after_step, status)
    _append(ledger, "watchdog_poll_started", run_id=run_id, after_step=after_step, watchdog_id=watchdog_id, pid=pid, poll_number=poll_number)
    started = clock()
    if poll_command is not None:
        # Production polls have a killable OS owner.  A Python daemon thread
        # cannot be terminated after timeout and is therefore reserved for
        # deterministic in-process fake-clock certification only.
        process = subprocess.Popen(poll_command, cwd=str(poll_cwd) if poll_cwd else None,
                                   env=poll_env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        status.update({"poll_owner_pid": process.pid, "poll_owner_terminated": False,
                       "poll_owner_termination_verified": False})
        _save_status(root, run_id, after_step, status)
        while process.poll() is None:
            elapsed = max(0.0, clock() - started)
            status["poll_elapsed_seconds"] = round(elapsed, 1)
            _save_status(root, run_id, after_step, status)
            if elapsed >= poll_timeout_seconds:
                try:
                    process.terminate()
                except OSError:
                    pass
                termination_deadline = time.monotonic() + 5
                while process.poll() is None and time.monotonic() < termination_deadline:
                    time.sleep(0.05)
                terminated = process.poll() is not None
                status.update({"poll_owner_terminated": True,
                               "poll_owner_termination_verified": terminated,
                               "status": "POLL_TIMEOUT",
                               "poll_elapsed_seconds": round(max(0.0, clock() - started), 1),
                               "poll_finished_at": now(),
                               "poll_duration_seconds": round(max(0.0, clock() - started), 1),
                               "last_poll_at": now(), "last_poll_action": "POLL_TIMEOUT",
                               "last_error": f"poll exceeded {poll_timeout_seconds}s" if terminated else "poll owner termination unverified"})
                _save_status(root, run_id, after_step, status)
                _append(ledger, "watchdog_poll_timeout", run_id=run_id, after_step=after_step, watchdog_id=watchdog_id, pid=pid, poll_number=poll_number, error=status["last_error"], poll_owner_pid=process.pid, termination_verified=terminated)
                return None, True
            time.sleep(0.05)
        stdout, stderr = process.communicate()
        duration = max(0.0, clock() - started)
        status.update({"poll_elapsed_seconds": round(duration, 1), "poll_finished_at": now(),
                       "poll_duration_seconds": round(duration, 1), "last_poll_at": now()})
        if process.returncode != 0:
            status.update({"last_poll_action": "FAILED", "last_error": (stderr or f"poll exit={process.returncode}").strip()[-1000:]})
            _save_status(root, run_id, after_step, status)
            return None, False
        try:
            from .relay import PollResult
            payload = json.loads(stdout or "{}")
            result = PollResult(payload.get("action", "FAILED"), payload.get("message_id"), Path(payload["staged_path"]) if payload.get("staged_path") else None, payload.get("worker"))
        except Exception as exc:
            status.update({"last_poll_action": "FAILED", "last_error": f"invalid poll result: {type(exc).__name__}"})
            _save_status(root, run_id, after_step, status)
            return None, False
        status.update({"last_poll_action": result.action, "last_error": None, "polls_completed": poll_number})
        _save_status(root, run_id, after_step, status)
        _append(ledger, "watchdog_poll_finished", run_id=run_id, after_step=after_step, watchdog_id=watchdog_id, pid=pid, poll_number=poll_number, action=result.action, duration_seconds=round(duration, 1))
        return result, False
    done = threading.Event()
    box: dict[str, Any] = {}

    def invoke() -> None:
        try:
            box["result"] = poll_factory().poll_once()
        except BaseException as exc:  # transport failure is reported to the watchdog
            box["error"] = exc
        finally:
            done.set()

    thread = threading.Thread(target=invoke, name=f"agentrelay-poll-{poll_number}", daemon=True)
    thread.start()
    timed_out = False
    while not done.is_set():
        done.wait(0.05)
        elapsed = max(0.0, clock() - started)
        status["poll_elapsed_seconds"] = round(elapsed, 1)
        _save_status(root, run_id, after_step, status)
        if elapsed >= poll_timeout_seconds:
            timed_out = True
            break
        sleep(1)
    duration = max(0.0, clock() - started)
    if timed_out:
        status.update({"status": "POLL_TIMEOUT", "poll_elapsed_seconds": round(duration, 1), "poll_finished_at": now(), "poll_duration_seconds": round(duration, 1), "last_poll_at": now(), "last_poll_action": "POLL_TIMEOUT", "last_error": f"poll exceeded {poll_timeout_seconds}s"})
        _save_status(root, run_id, after_step, status)
        _append(ledger, "watchdog_poll_timeout", run_id=run_id, after_step=after_step, watchdog_id=watchdog_id, pid=pid, poll_number=poll_number, error=status["last_error"])
        return None, True
    status.update({"poll_elapsed_seconds": round(duration, 1), "poll_finished_at": now(), "poll_duration_seconds": round(duration, 1), "last_poll_at": now()})
    if "error" in box:
        status.update({"last_poll_action": "FAILED", "last_error": f"{type(box['error']).__name__}: {box['error']}"})
        _save_status(root, run_id, after_step, status)
        _append(ledger, "watchdog_poll_finished", run_id=run_id, after_step=after_step, watchdog_id=watchdog_id, pid=pid, poll_number=poll_number, action="FAILED", error=status["last_error"])
        return None, False
    result = box.get("result")
    action = getattr(result, "action", "FAILED")
    status.update({"last_poll_action": action, "last_error": None, "polls_completed": poll_number})
    _save_status(root, run_id, after_step, status)
    _append(ledger, "watchdog_poll_finished", run_id=run_id, after_step=after_step, watchdog_id=watchdog_id, pid=pid, poll_number=poll_number, action=action, duration_seconds=round(duration, 1))
    return result, False


def _wait_for_worker_chain(root: Path, run_id: str, after_step: int, status: dict[str, Any], ledger: Ledger, watchdog_id: str, pid: int, result: Any, sleep: Callable[[float], None], clock: Callable[[], float], state_store: StateStore, claim_timeout_seconds: int = WORKER_CLAIM_TIMEOUT_SECONDS, codex_timeout_seconds: int = CODEX_START_TIMEOUT_SECONDS) -> tuple[bool, str]:
    worker = result.worker or {}
    worker_id = str(worker.get("worker_id") or "")
    worker_pid = worker.get("pid")
    status.update({"status": "GMAIL_WAKE_FOUND", "worker_pid": worker_pid, "codex_pid": None, "worker_claim_elapsed_seconds": 0.0, "codex_start_elapsed_seconds": None, "last_error": None})
    _save_status(root, run_id, after_step, status)
    _append(ledger, "watchdog_gmail_wake_found", run_id=run_id, after_step=after_step, watchdog_id=watchdog_id, pid=pid, poll_number=status.get("poll_number"), worker_pid=worker_pid)
    status["status"] = "WORKER_PROCESS_CREATED"
    _save_status(root, run_id, after_step, status)
    _append(ledger, "watchdog_worker_process_created", run_id=run_id, after_step=after_step, watchdog_id=watchdog_id, pid=pid, poll_number=status.get("poll_number"), worker_pid=worker_pid)
    started = clock()
    claimed_at: float | None = None
    while True:
        elapsed = max(0.0, clock() - started)
        status["worker_claim_elapsed_seconds"] = round(elapsed, 1)
        try:
            state = state_store.load()
        except Exception as exc:
            status.update({"status": "WORKER_CLAIM_FAILED", "last_error": f"state read failed: {type(exc).__name__}"})
            _save_status(root, run_id, after_step, status)
            return False, "WORKER_CLAIM_FAILED"
        active = state.get("active_worker")
        pending = state.get("pending_worker")
        if isinstance(active, dict) and active.get("worker_id") == worker_id:
            if claimed_at is None:
                claimed_at = clock()
                status["status"] = "WORKER_CLAIMED"
                _save_status(root, run_id, after_step, status)
                _append(ledger, "watchdog_worker_claimed", run_id=run_id, after_step=after_step, watchdog_id=watchdog_id, pid=pid, poll_number=status.get("poll_number"), worker_pid=worker_pid)
            codex_elapsed = max(0.0, clock() - claimed_at)
            status["codex_start_elapsed_seconds"] = round(codex_elapsed, 1)
            codex_status = str(active.get("codex_status") or "NOT_STARTED")
            codex_pid = active.get("codex_pid")
            codex_exe = active.get("codex_exe") or ""
            if codex_status in {"NOT_STARTED", "CODEX_STARTING"}:
                if status.get("status") != "CODEX_STARTING":
                    status["status"] = "CODEX_STARTING"
                    _save_status(root, run_id, after_step, status)
                    _append(ledger, "watchdog_codex_starting", run_id=run_id, after_step=after_step, watchdog_id=watchdog_id, pid=pid, poll_number=status.get("poll_number"), worker_pid=worker_pid)
            if codex_status == "CODEX_STARTED" and isinstance(codex_pid, int) and exact_owner_live({"pid": codex_pid, "exe": codex_exe}):
                status.update({"status": "CODEX_RUNNING", "codex_pid": codex_pid})
                _save_status(root, run_id, after_step, status)
                _append(ledger, "watchdog_codex_running", run_id=run_id, after_step=after_step, watchdog_id=watchdog_id, pid=pid, poll_number=status.get("poll_number"), codex_pid=codex_pid)
                status["status"] = "WAKE_CHAIN_CONFIRMED"
                _save_status(root, run_id, after_step, status)
                _append(ledger, "watchdog_wake_chain_confirmed", run_id=run_id, after_step=after_step, watchdog_id=watchdog_id, pid=pid, poll_number=status.get("poll_number"), worker_pid=worker_pid, codex_pid=codex_pid)
                return True, "WAKE_CHAIN_CONFIRMED"
            if codex_status in {"CODEX_START_FAILED", "CODEX_EXITED"} or codex_elapsed >= codex_timeout_seconds:
                status.update({"status": "CODEX_START_FAILED", "last_error": active.get("codex_error") or f"Codex did not start within {codex_timeout_seconds}s"})
                _save_status(root, run_id, after_step, status)
                _append(ledger, "watchdog_codex_start_failed", run_id=run_id, after_step=after_step, watchdog_id=watchdog_id, pid=pid, poll_number=status.get("poll_number"), error=status["last_error"])
                return False, "CODEX_START_FAILED"
        elif isinstance(pending, dict) and pending.get("worker_id") == worker_id:
            if isinstance(worker_pid, int) and not exact_owner_live({"pid": worker_pid, "exe": worker.get("exe", "")}):
                status.update({"status": "WORKER_CLAIM_FAILED", "last_error": "worker exited before claim"})
                _save_status(root, run_id, after_step, status)
                _append(ledger, "watchdog_worker_claim_failed", run_id=run_id, after_step=after_step, watchdog_id=watchdog_id, pid=pid, poll_number=status.get("poll_number"), worker_pid=worker_pid)
                return False, "WORKER_CLAIM_FAILED"
        elif claimed_at is None and elapsed >= claim_timeout_seconds:
            status.update({"status": "WORKER_CLAIM_FAILED", "last_error": f"worker claim not observed within {claim_timeout_seconds}s"})
            _save_status(root, run_id, after_step, status)
            _append(ledger, "watchdog_worker_claim_failed", run_id=run_id, after_step=after_step, watchdog_id=watchdog_id, pid=pid, poll_number=status.get("poll_number"), worker_pid=worker_pid)
            return False, "WORKER_CLAIM_FAILED"
        _save_status(root, run_id, after_step, status)
        sleep(0.5)


def run_watchdog(config, *, run_id: str, after_step: int, poll_factory=None, sleep=time.sleep, watchdog_id: str | None = None, ui_spawn=None, clock=time.monotonic, poll_interval_seconds: int = POLL_INTERVAL_SECONDS, max_polls: int | None = None, poll_timeout_seconds: int = POLL_TIMEOUT_SECONDS, service_window_seconds: int = WATCHDOG_WINDOW_SECONDS, poll_command: list[str] | None = None, poll_env: dict[str, str] | None = None, poll_cwd: Path | None = None) -> str:
    root = config.local_project_storage
    ledger = Ledger(root)
    watchdog_id = watchdog_id or str(uuid4())
    pid = os.getpid()
    with exact_watchdog_lock(root, run_id, after_step) as acquired:
        if not acquired:
            return "deduped"
        status = load_watchdog_status(root, run_id, after_step) or _status_template(watchdog_id=watchdog_id, pid=pid, exe=Path(sys.executable).name, run_id=run_id, after_step=after_step, status="STARTING")
        status.update({"watchdog_id": watchdog_id, "pid": pid, "exe": Path(sys.executable).name, "status": "STARTING", "startup_ack_at": now()})
        _save_status(root, run_id, after_step, status)
        _append(ledger, "watchdog_started", run_id=run_id, after_step=after_step, watchdog_id=watchdog_id, pid=pid)
        try:
            ui_pid = (ui_spawn or (lambda: spawn_watchdog_ui(config)))()
            status.update({"ui_pid": ui_pid, "ui_started_at": now()})
            _save_status(root, run_id, after_step, status)
            _append(ledger, "watchdog_ui_process_created", run_id=run_id, after_step=after_step, watchdog_id=watchdog_id, pid=pid, ui_pid=ui_pid)
        except Exception as exc:
            status["ui_error"] = f"{type(exc).__name__}: {exc}"
            _save_status(root, run_id, after_step, status)
            _append(ledger, "watchdog_ui_failed", run_id=run_id, after_step=after_step, watchdog_id=watchdog_id, pid=pid, error=status["ui_error"])
        state_store = StateStore(root)
        status.update({"max_polls": max_polls if max_polls is not None else 0, "poll_interval_seconds": poll_interval_seconds, "poll_timeout_seconds": poll_timeout_seconds, "service_window_seconds": service_window_seconds})
        _save_status(root, run_id, after_step, status)
        started_clock = clock()
        deadline = started_clock + service_window_seconds
        poll_number = 0
        while True:
            if max_polls is not None:
                if poll_number >= max_polls:
                    break
            elif clock() >= deadline:
                break
            status["service_window_remaining_seconds"] = max(0, int(math.ceil(deadline - clock())))
            _save_status(root, run_id, after_step, status)
            if not _wait_for_poll(root, run_id, after_step, status, sleep, clock, state_store, poll_interval_seconds):
                return _finish(status, root, run_id, after_step, ledger, watchdog_id, pid, "STOPPED", "stop_requested", return_value="stopped")
            poll_number += 1
            result, timed_out = _bounded_poll(root, run_id, after_step, status, ledger, watchdog_id, pid, poll_number, poll_factory, sleep, clock, poll_timeout_seconds, poll_command=poll_command, poll_env=poll_env, poll_cwd=poll_cwd)
            if timed_out:
                # A production subprocess is killable and verified above.  An
                # in-process test double has no supported thread-termination
                # primitive; fail closed instead of beginning another poll
                # while the old operation might still be alive.
                if status.get("poll_owner_termination_verified") is not True:
                    return _finish(status, root, run_id, after_step, ledger, watchdog_id, pid, "FAILED", "poll_owner_termination_unverified", return_value="failed")
                if max_polls is not None and poll_number >= max_polls:
                    break
                continue
            if result is None:
                status["status"] = "FAILED"
                _save_status(root, run_id, after_step, status)
                return _finish(status, root, run_id, after_step, ledger, watchdog_id, pid, "FAILED", status.get("last_error") or "poll_failed", return_value="failed")
            action = result.action
            if action in {"worker_process_created", "launched"}:
                confirmed, chain_status = _wait_for_worker_chain(root, run_id, after_step, status, ledger, watchdog_id, pid, result, sleep, clock, state_store)
                if not confirmed:
                    return _finish(status, root, run_id, after_step, ledger, watchdog_id, pid, chain_status, chain_status, return_value="failed")
                _countdown(root, run_id, after_step, status, WORKER_SHUTDOWN_SECONDS, sleep, clock)
                return _finish(status, root, run_id, after_step, ledger, watchdog_id, pid, "FINISHED", "wake_chain_confirmed", return_value="launched")
            if action in {"human_required", "conflict", "launch_failed"}:
                status["status"] = "FAILED"; status["last_error"] = action; _save_status(root, run_id, after_step, status)
                return _finish(status, root, run_id, after_step, ledger, watchdog_id, pid, "FAILED", action, return_value=action)
            if action == "advanced":
                return _finish(status, root, run_id, after_step, ledger, watchdog_id, pid, "ADVANCED", "protocol_advanced", return_value="advanced")
            status["status"] = "IDLE"
            _save_status(root, run_id, after_step, status)
        completed = poll_number
        reason = f"no_matching_wake_after_{completed}_polls" if max_polls is not None else "no_matching_wake_after_service_window"
        status.update({"status": "NO_WAKE_FOUND", "polls_completed": completed, "last_poll_action": status.get("last_poll_action") or "IDLE", "service_window_remaining_seconds": 0, "closing_countdown_seconds": NO_WAKE_SHUTDOWN_SECONDS})
        _save_status(root, run_id, after_step, status)
        _append(ledger, "watchdog_exhausted", run_id=run_id, after_step=after_step, watchdog_id=watchdog_id, pid=pid, poll_number=completed, reason=reason)
        _countdown(root, run_id, after_step, status, NO_WAKE_SHUTDOWN_SECONDS, sleep, clock)
        return _finish(status, root, run_id, after_step, ledger, watchdog_id, pid, "FINISHED", reason, return_value="exhausted")
