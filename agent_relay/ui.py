from __future__ import annotations

import os
from pathlib import Path
import subprocess
from threading import Event, Thread
import tkinter as tk
from tkinter import messagebox

from .supervisor import Supervisor, SupervisorState
from .config import config_path


class RelayApp(tk.Tk):
    def __init__(self, supervisor: Supervisor):
        super().__init__()
        self.supervisor = supervisor
        self.title("AgentRelay Read & Wake Supervisor")
        self.geometry("760x520")
        self.worker_stop = Event()
        self.vars = {key: tk.StringVar(value="—") for key in ("state", "project", "project_id", "channel", "target", "target_id", "repo", "chat", "run", "step", "lease", "poll", "matched", "staged", "wake", "error")}
        controls = tk.Frame(self); controls.pack(fill="x", padx=12, pady=12)
        for text, command in (("Start", self.start), ("Stop", self.stop), ("Bind/Edit Project", self.edit_project), ("Test Gmail", self.test_gmail), ("Test Wake", self.test_wake), ("Open Inbox", lambda: self.open_path(self.supervisor.config.local_project_storage / "inbox")), ("Open Logs", lambda: self.open_path(self.supervisor.config.local_project_storage / "ledger"))):
            tk.Button(controls, text=text, command=command).pack(side="left", padx=3)
        panel = tk.Frame(self); panel.pack(fill="both", expand=True, padx=12)
        labels = (("Supervisor state", "state"), ("Project name", "project"), ("Project ID", "project_id"), ("Channel ID", "channel"), ("Codex target", "target"), ("Target ID/session", "target_id"), ("Repository path", "repo"), ("ChatGPT target", "chat"), ("Current RUN", "run"), ("Expected STEP", "step"), ("Agent/lease state", "lease"), ("Last Gmail poll", "poll"), ("Last matched Gmail", "matched"), ("Last staged instruction", "staged"), ("Last wake", "wake"), ("Last error/fault", "error"))
        for row, (label, key) in enumerate(labels):
            tk.Label(panel, text=label + ":", anchor="w", width=22).grid(row=row, column=0, sticky="w", pady=2)
            tk.Label(panel, textvariable=self.vars[key], anchor="w", justify="left", wraplength=540).grid(row=row, column=1, sticky="w", pady=2)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh()

    def refresh(self):
        s, c = self.supervisor.snapshot(), self.supervisor.config
        last = s.get("last", {})
        mapping = {"state": s["state"], "project": c.display_name, "project_id": c.project_id, "channel": c.channel_id, "target": c.target_label, "target_id": c.target_id or "not bound", "repo": str(c.repo_path), "chat": c.chat_url or "not configured", "run": s.get("current_run") or "—", "step": str(s.get("expected_step", "—")), "lease": (s.get("active_lease") or last.get("last_lease") or {}).get("status", "none") if isinstance(s.get("active_lease") or last.get("last_lease") or {}, dict) else "none", "poll": last.get("last_gmail_poll", "—"), "matched": last.get("gmail_message_id", "—"), "staged": last.get("last_staged_instruction", "—"), "wake": last.get("lease_id", "—"), "error": s.get("last_error") or "—"}
        for key, value in mapping.items(): self.vars[key].set(str(value))
        self.after(500, self.refresh)

    def start(self):
        try:
            self.supervisor.start(); self.worker_stop.clear()
            Thread(target=self.poll_loop, daemon=True).start()
        except Exception as exc: messagebox.showerror("AgentRelay", str(exc))

    def poll_loop(self):
        while not self.worker_stop.is_set():
            self.supervisor.poll_once()
            self.worker_stop.wait(self.supervisor.config.poll_interval)

    def stop(self):
        self.worker_stop.set(); self.supervisor.stop()

    def test_gmail(self):
        try: self.supervisor.test_gmail(); messagebox.showinfo("AgentRelay", "Gmail authentication and connection are ready.")
        except Exception as exc: messagebox.showerror("AgentRelay", str(exc))

    def test_wake(self):
        try:
            if self.supervisor.test_wake(): messagebox.showinfo("AgentRelay", "Mock wake accepted exactly one diagnostic lease.")
            else: messagebox.showerror("AgentRelay", "Mock wake was configured to fail.")
        except Exception as exc: messagebox.showwarning("AgentRelay", str(exc))

    def edit_project(self):
        messagebox.showinfo("AgentRelay", f"Edit the local configuration then restart:\n{config_path()}")

    def open_path(self, path: Path):
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path) if hasattr(os, "startfile") else subprocess.Popen(["xdg-open", str(path)])

    def close(self):
        self.stop(); self.destroy()
