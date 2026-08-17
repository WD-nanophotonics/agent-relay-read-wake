from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any
from uuid import uuid4

from .ownership import exact_owner_live
from .storage import Ledger, StateStore, atomic_json, now

WAIT_SECONDS = 120
MAX_ATTEMPTS = 2


def watchdog_status_path(root: Path, run_id: str, after_step: int) -> Path:
    return root / "watchdogs" / f"{run_id}-after-{after_step:04d}.json"


def _lock_path(root: Path, run_id: str, after_step: int) -> Path:
    return root / "watchdogs" / f"{run_id}-after-{after_step:04d}.lock"


def _status_template(*, watchdog_id: str, pid: int | None, exe: str, run_id: str, after_step: int, status: str) -> dict[str, Any]:
    stamp = now()
    return {
        "watchdog_id": watchdog_id, "pid": pid, "exe": exe, "run_id": run_id,
        "after_step": after_step, "status": status, "started_at": stamp,
        "updated_at": stamp, "attempt": 0, "max_attempts": MAX_ATTEMPTS,
        "wait_started_at": None, "next_poll_at": None, "last_poll_at": None,
        "last_poll_action": None, "last_error": None, "finished_at": None,
        "finish_reason": None, "startup_ack_at": None,
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
        # Keep the marker: the exact tuple is one-shot.
        pass


def _append(ledger: Ledger, event: str, *, run_id: str, after_step: int, watchdog_id: str, pid: int, attempt: int | None = None, **values: Any) -> None:
    ledger.append(event, run_id=run_id, after_step=after_step, watchdog_id=watchdog_id, pid=pid, attempt=attempt, **values)


def spawn_watchdog_ui(config) -> int:
    """Launch the read-only Tk monitor without making it the foreground window."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen([sys.executable, "-m", "agent_relay.cli", "watchdog-ui"], cwd=config.repo_path, creationflags=flags, close_fds=True)
    return process.pid


def spawn_watchdog(config, *, run_id: str, after_step: int) -> dict[str, Any]:
    """Spawn one detached watchdog and wait briefly for its startup ACK."""
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
        process = subprocess.Popen([sys.executable, "-m", "agent_relay.cli", "watchdog", "--run", run_id, "--after-step", str(after_step), "--watchdog-id", watchdog_id], cwd=config.repo_path, creationflags=flags, close_fds=True)
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


def _finish(status: dict[str, Any], root: Path, run_id: str, after_step: int, ledger: Ledger, watchdog_id: str, pid: int, terminal: str, reason: str, event: str | None = None, return_value: str | None = None) -> str:
    status.update({"status": terminal, "finished_at": now(), "finish_reason": reason, "next_poll_at": None})
    _save_status(root, run_id, after_step, status)
    if event:
        _append(ledger, event, run_id=run_id, after_step=after_step, watchdog_id=watchdog_id, pid=pid, attempt=status.get("attempt"), reason=reason)
    _append(ledger, "watchdog_finished", run_id=run_id, after_step=after_step, watchdog_id=watchdog_id, pid=pid, attempt=status.get("attempt"), reason=reason)
    return return_value or reason


def run_watchdog(config, *, run_id: str, after_step: int, poll_factory, sleep=time.sleep, watchdog_id: str | None = None, ui_spawn=None) -> str:
    root = config.local_project_storage
    ledger = Ledger(root)
    watchdog_id = watchdog_id or str(uuid4())
    pid = os.getpid()
    with exact_watchdog_lock(root, run_id, after_step) as acquired:
        if not acquired:
            existing = load_watchdog_status(root, run_id, after_step)
            if not existing:
                _save_status(root, run_id, after_step, _status_template(watchdog_id=watchdog_id, pid=pid, exe=Path(sys.executable).name, run_id=run_id, after_step=after_step, status="DEDUPED"))
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
        for attempt in range(1, MAX_ATTEMPTS + 1):
            status.update({"status": "WAITING", "attempt": attempt, "wait_started_at": now(), "next_poll_at": datetime.fromtimestamp(time.time() + WAIT_SECONDS, UTC).isoformat(), "last_error": None})
            _save_status(root, run_id, after_step, status)
            _append(ledger, "watchdog_wait_started", run_id=run_id, after_step=after_step, watchdog_id=watchdog_id, pid=pid, attempt=attempt)
            sleep(WAIT_SECONDS)
            status.update({"status": "POLLING", "next_poll_at": None})
            _save_status(root, run_id, after_step, status)
            _append(ledger, "watchdog_poll_started", run_id=run_id, after_step=after_step, watchdog_id=watchdog_id, pid=pid, attempt=attempt)
            try:
                state = StateStore(root).load()
                if state.get("stop_requested"):
                    _append(ledger, "watchdog_stop_detected", run_id=run_id, after_step=after_step, watchdog_id=watchdog_id, pid=pid, attempt=attempt)
                    return _finish(status, root, run_id, after_step, ledger, watchdog_id, pid, "STOPPED", "stop_requested", return_value="stopped")
                owner = state.get("active_worker")
                if owner and exact_owner_live(owner):
                    return _finish(status, root, run_id, after_step, ledger, watchdog_id, pid, "FINISHED", "active_worker", "watchdog_worker_detected", "active")
                if state.get("current_run") == run_id and int(state.get("expected_step", 0)) != after_step + 1:
                    _append(ledger, "watchdog_protocol_advanced", run_id=run_id, after_step=after_step, watchdog_id=watchdog_id, pid=pid, attempt=attempt)
                    return _finish(status, root, run_id, after_step, ledger, watchdog_id, pid, "ADVANCED", "protocol_advanced", return_value="advanced")
                result = poll_factory().poll_once()
                action = result.action
                status.update({"last_poll_at": now(), "last_poll_action": action})
                _save_status(root, run_id, after_step, status)
                _append(ledger, "watchdog_poll_finished", run_id=run_id, after_step=after_step, watchdog_id=watchdog_id, pid=pid, attempt=attempt, action=action)
                if action == "launched":
                    return _finish(status, root, run_id, after_step, ledger, watchdog_id, pid, "WORKER_STARTED", "worker_launched", return_value="launched")
                if action == "advanced":
                    return _finish(status, root, run_id, after_step, ledger, watchdog_id, pid, "ADVANCED", "poll_advanced", return_value="advanced")
            except Exception as exc:
                status["last_error"] = f"{type(exc).__name__}: {exc}"
                _save_status(root, run_id, after_step, status)
                _append(ledger, "watchdog_failed", run_id=run_id, after_step=after_step, watchdog_id=watchdog_id, pid=pid, attempt=attempt, error=status["last_error"])
                return _finish(status, root, run_id, after_step, ledger, watchdog_id, pid, "FAILED", "exception")
        return _finish(status, root, run_id, after_step, ledger, watchdog_id, pid, "EXHAUSTED", "two_poll_attempts_missed", "watchdog_exhausted", "exhausted")
