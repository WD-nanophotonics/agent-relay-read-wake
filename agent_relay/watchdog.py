from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time
from contextlib import contextmanager

from .relay import Relay, _owner_live
from .storage import Ledger, StateStore, atomic_json, now


@contextmanager
def exact_watchdog_lock(root: Path, run_id: str, after_step: int):
    lock = root / "watchdogs" / f"{run_id}-after-{after_step:04d}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        # The marker is persistent for this exact tuple. A dead owner can be
        # recovered once; no age-only deletion is allowed.
        try:
            text = lock.read_text(encoding="utf-8")
            pid = int(next((line.split("=", 1)[1] for line in text.splitlines() if line.startswith("pid=")), "-1"))
            if pid > 0:
                os.kill(pid, 0)
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
        os.write(fd, f"pid={os.getpid()}\ncreated={now()}\n".encode())
        os.close(fd)
        yield True
    finally:
        # Keep the exact marker: a second watchdog for the same completed
        # (project, RUN, AFTER_STEP) is a duplicate, not a retry.
        pass


def spawn_watchdog(config, *, run_id: str, after_step: int) -> None:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen([sys.executable, "-m", "agent_relay.cli", "watchdog", "--run", run_id, "--after-step", str(after_step)], cwd=config.repo_path, creationflags=flags, close_fds=True)


def run_watchdog(config, *, run_id: str, after_step: int, poll_factory, sleep=time.sleep) -> str:
    store = StateStore(config.local_project_storage)
    ledger = Ledger(config.local_project_storage)
    with exact_watchdog_lock(config.local_project_storage, run_id, after_step) as acquired:
        if not acquired:
            return "deduped"
        for attempt in range(2):
            sleep(120)
            state = store.load()
            if state.get("stop_requested"):
                ledger.append("watchdog_stopped", run_id=run_id, after_step=after_step)
                return "stopped"
            owner = state.get("active_worker")
            if owner and _owner_live(owner):
                ledger.append("watchdog_active_worker", worker_id=owner.get("worker_id"), attempt=attempt + 1)
                return "active"
            if state.get("current_run") == run_id and int(state.get("expected_step", 0)) != after_step + 1:
                ledger.append("watchdog_protocol_advanced", run_id=run_id, after_step=after_step)
                return "advanced"
            result = poll_factory().poll_once()
            ledger.append("watchdog_poll_once", run_id=run_id, after_step=after_step, attempt=attempt + 1, action=result.action)
            if result.action in {"launched", "advanced", "busy"}:
                return result.action
        return "exhausted"
