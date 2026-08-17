from __future__ import annotations

from datetime import datetime
import math
import os
from pathlib import Path
import subprocess

from .ownership import exact_owner_live
from .storage import StateStore
from .watchdog import load_watchdog_status


class WatchdogMonitorModel:
    """Read-only view model; it never polls Gmail or changes relay state."""

    def __init__(self, config):
        self.config = config

    def snapshot(self) -> dict:
        watchdog = load_watchdog_status(self.config.local_project_storage)
        state = StateStore(self.config.local_project_storage).load()
        if watchdog:
            watchdog = dict(watchdog)
            watchdog["alive"] = bool(watchdog.get("pid") and exact_owner_live(watchdog))
            try:
                started = datetime.fromisoformat(watchdog["started_at"])
                watchdog["total_elapsed_seconds"] = max(0, int((datetime.now(started.tzinfo) - started).total_seconds()))
            except (KeyError, TypeError, ValueError):
                watchdog["total_elapsed_seconds"] = None
            next_poll = watchdog.get("next_poll_at")
            if next_poll:
                try:
                    target = datetime.fromisoformat(next_poll)
                    watchdog["countdown_seconds"] = max(0, int((target - datetime.now(target.tzinfo)).total_seconds()))
                except (TypeError, ValueError):
                    watchdog["countdown_seconds"] = None
            if watchdog.get("status") == "POLLING" and watchdog.get("poll_started_at"):
                try:
                    poll_started = datetime.fromisoformat(watchdog["poll_started_at"])
                    watchdog["polling_for_seconds"] = max(0, round((datetime.now(poll_started.tzinfo) - poll_started).total_seconds(), 1))
                except (TypeError, ValueError):
                    watchdog["polling_for_seconds"] = None
            else:
                watchdog["polling_for_seconds"] = None
        return {"watchdog": watchdog, "relay": state}


def _open_path(path: Path) -> None:
    try:
        if hasattr(os, "startfile"):
            os.startfile(str(path))
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError:
        pass


def run_watchdog_ui(config) -> int:
    import tkinter as tk
    from tkinter import ttk

    model = WatchdogMonitorModel(config)
    root = tk.Tk()
    # Build hidden, then show without activation so the monitor has a taskbar
    # window but never jumps in front of the user's current application.
    root.withdraw()
    root.title("AgentRelay Watchdog")
    root.geometry("620x430")
    fields = [
        ("Status", "status"), ("RUN", "run_id"), ("After STEP", "after_step"),
        ("PID / alive-dead", "pid_alive"), ("Started at", "started_at"),
        ("UI PID", "ui_pid"),
        ("Poll", "poll"), ("Next Gmail check in", "countdown_seconds"),
        ("Total elapsed", "total_elapsed_seconds"), ("Polling for", "polling_for_seconds"),
        ("Relay mode", "relay_mode"),
        ("Expected STEP", "expected_step"), ("Active/pending Worker", "worker"),
        ("Last check", "last_poll_at"), ("Last poll result", "last_poll_action"),
        ("Last duration", "poll_duration_seconds"), ("Last error", "last_error"),
        ("Closing monitor in", "closing_countdown_seconds"), ("Worker PID", "worker_pid"),
        ("Worker claim wait", "worker_claim_elapsed_seconds"), ("Codex PID", "codex_pid"),
        ("Codex start wait", "codex_start_elapsed_seconds"),
        ("Finished reason", "finish_reason"),
    ]
    values: dict[str, ttk.Label] = {}
    frame = ttk.Frame(root, padding=12)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="AgentRelay Watchdog", font=("Segoe UI", 16, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
    for row, (label, key) in enumerate(fields, 1):
        ttk.Label(frame, text=label + ":").grid(row=row, column=0, sticky="nw", padx=(0, 12), pady=2)
        value = ttk.Label(frame, text="-")
        value.grid(row=row, column=1, sticky="nw", pady=2)
        values[key] = value
    buttons = ttk.Frame(frame)
    buttons.grid(row=len(fields) + 1, column=0, columnspan=2, sticky="w", pady=(12, 0))
    ttk.Button(buttons, text="Refresh", command=lambda: refresh()).pack(side="left", padx=(0, 6))
    ttk.Button(buttons, text="Open log", command=lambda: _open_path(config.local_project_storage / "ledger" / "events.jsonl")).pack(side="left", padx=(0, 6))
    ttk.Button(buttons, text="Open runtime folder", command=lambda: _open_path(config.local_project_storage)).pack(side="left")

    def refresh() -> None:
        snap = model.snapshot()
        watchdog = snap["watchdog"]
        relay = snap["relay"]
        if not watchdog:
            values["status"].configure(text="NO ACTIVE WATCHDOG")
            for key in values:
                if key != "status":
                    values[key].configure(text="-")
        else:
            values["status"].configure(text=watchdog.get("status", "-"))
            values["run_id"].configure(text=watchdog.get("run_id", "-"))
            values["after_step"].configure(text=f"{int(watchdog.get('after_step', 0)):04d}")
            values["pid_alive"].configure(text=f"{watchdog.get('pid', '-')} / {'alive' if watchdog.get('alive') else 'dead'}")
            values["poll"].configure(text=f"{watchdog.get('poll_number', 0)} / {watchdog.get('max_polls', 10)}")
            for key in ("started_at", "ui_pid", "countdown_seconds", "total_elapsed_seconds", "polling_for_seconds", "last_poll_at", "last_poll_action", "poll_duration_seconds", "last_error", "closing_countdown_seconds", "worker_pid", "worker_claim_elapsed_seconds", "codex_pid", "codex_start_elapsed_seconds", "finish_reason"):
                values[key].configure(text=str(watchdog.get(key) if watchdog.get(key) is not None else "-"))
        values["relay_mode"].configure(text=relay.get("mode", "-"))
        values["expected_step"].configure(text=str(relay.get("expected_step", "-")))
        values["worker"].configure(text=str(relay.get("active_worker") or relay.get("pending_worker") or "-"))
        root.after(1000, refresh)

    refresh()
    def show_without_activation() -> None:
        root.deiconify()
        root.attributes("-topmost", False)
        if os.name == "nt":
            try:
                import ctypes
                ctypes.windll.user32.ShowWindow(root.winfo_id(), 4)  # SW_SHOWNOACTIVATE
            except (AttributeError, OSError):
                pass
    root.after_idle(show_without_activation)
    root.mainloop()
    return 0
