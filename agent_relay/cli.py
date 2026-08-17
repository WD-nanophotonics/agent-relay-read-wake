from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from uuid import uuid4

from .config import app_home, config_path, load_config, save_binding, write_example
from .gmail import GoogleGmailGateway
from .relay import NoopWorkerLauncher, Relay
from .storage import StateStore, read_content_hash
from .protocol import Disposition, ProtocolEnvelope
from .ownership import exact_owner_live
from .watchdog import run_watchdog
from .worker import OneShotWorker, ProcessWorkerLauncher
from .handoff import HandoffSubmission


class _DiagnosticHandoffSink:
    """Explicitly opt-in local sink for bounded production-worker probes."""

    def submit(self, report: str) -> HandoffSubmission:
        return HandoffSubmission(True, "diagnostic local handoff sink", verified=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="agent-relay")
    parser.add_argument("command", choices=("init", "poll-once", "run-agent", "worker", "watchdog", "watchdog-ui", "monitor", "stop", "status", "test-gmail", "test-wake", "bind"), nargs="?", default="status")
    parser.add_argument("--run")
    parser.add_argument("--step", type=int)
    parser.add_argument("--parent", type=int)
    parser.add_argument("--after-step", type=int)
    parser.add_argument("--staged")
    parser.add_argument("--worker-id")
    parser.add_argument("--message-id")
    parser.add_argument("--content-hash")
    parser.add_argument("--watchdog-id")
    parser.add_argument("--target-id")
    parser.add_argument("--target-type", default="codex-cli")
    args = parser.parse_args(argv)
    home = app_home()
    if args.command == "init":
        path = config_path(home)
        if not path.exists():
            home.mkdir(parents=True, exist_ok=True)
            write_example(path, Path.cwd())
            print(f"CREATED {path}")
        else:
            print(f"EXISTS {path}")
        return 0
    if args.command == "bind":
        if not args.target_id:
            parser.error("bind requires --target-id")
        print(f"BOUND {save_binding(home, target_id=args.target_id, target_type=args.target_type)}")
        return 0
    config = load_config(home)
    store = StateStore(config.local_project_storage)
    if args.command == "status":
        print(json.dumps(store.load(), ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "stop":
        state = store.load()
        state["stop_requested"] = True
        state["mode"] = "STOPPED"
        store.save(state)
        print("STOP_REQUESTED")
        return 0
    if args.command == "test-gmail":
        GoogleGmailGateway(config.gmail_auth_home).test_connection()
        print("GMAIL_OK")
        return 0
    if args.command == "test-wake":
        NoopWorkerLauncher()
        print("WAKE_TEST_READY")
        return 0
    if args.command == "poll-once":
        result = Relay(config, GoogleGmailGateway(config.gmail_auth_home), ProcessWorkerLauncher()).poll_once()
        print(json.dumps({"action": result.action, "message_id": result.message_id}, sort_keys=True))
        return 0
    if args.command == "run-agent":
        if not args.staged:
            parser.error("run-agent requires --staged")
        staged = Path(args.staged).resolve()
        if not (staged / "message.txt").is_file() or not (staged / "manifest.json").is_file():
            parser.error("run-agent staged path must contain message.txt and manifest.json")
        run_id = args.run or f"RUN-MANAGED-{uuid4().hex.upper()}"
        step = args.step or 1
        parent = args.parent if args.parent is not None else step - 1
        state = store.load()
        owner = state.get("active_worker") or state.get("pending_worker")
        if owner and exact_owner_live(owner):
            raise RuntimeError("managed AgentRelay project already has a live owner")
        if owner:
            state.update({"active_worker": None, "pending_worker": None, "mode": "IDLE"})
            store.save(state)
        envelope = ProtocolEnvelope(config.channel_id, run_id, step, parent, Disposition.WAKE, config.project_id)
        previous = os.environ.get("AGENT_RELAY_MANAGED_AGENT")
        os.environ["AGENT_RELAY_MANAGED_AGENT"] = "1"
        try:
            worker = ProcessWorkerLauncher().launch(staged_path=staged, envelope=envelope, content_hash=read_content_hash(staged), message_id="")
        finally:
            if previous is None:
                os.environ.pop("AGENT_RELAY_MANAGED_AGENT", None)
            else:
                os.environ["AGENT_RELAY_MANAGED_AGENT"] = previous
        worker.update({"parent": parent, "message_id": None, "content_hash": read_content_hash(staged), "staged_path": str(staged), "managed_entry": True})
        state = store.load()
        state.update({"mode": "BUSY", "pending_worker": worker, "last_error": None})
        store.save(state)
        from .storage import Ledger
        Ledger(config.local_project_storage).append("managed_agent_process_created", worker_id=worker.get("worker_id"), pid=worker.get("pid"), run_id=run_id, step=step)
        print(json.dumps({"action": "managed_agent_started", "run": run_id, "step": step, "worker": worker}, sort_keys=True))
        return 0
    if args.command == "worker":
        if not args.run or args.step is None or not args.staged:
            parser.error("worker requires --run, --step, and --staged")
        if os.environ.get("AGENT_RELAY_DIAGNOSTIC_POST_EXIT_SINK") == "1":
            worker = OneShotWorker(config, handoff_sender=_DiagnosticHandoffSink(), watchdog_spawn=None)
        elif os.environ.get("AGENT_RELAY_MANAGED_AGENT") == "1":
            worker = OneShotWorker(config, watchdog_spawn=None)
        else:
            worker = OneShotWorker(config, watchdog_spawn=lambda step, run: _spawn_watchdog(config, run, step))
        outcome = worker.run(run_id=args.run, step=args.step, staged_path=Path(args.staged), worker_id=args.worker_id, message_id=args.message_id, content_hash=args.content_hash)
        print(json.dumps({"ok": outcome.ok, "detail": outcome.detail}, ensure_ascii=False))
        return 0 if outcome.ok else 1
    if args.command == "watchdog":
        if not args.run or args.after_step is None:
            parser.error("watchdog requires --run and --after-step")
        result = run_watchdog(config, run_id=args.run, after_step=args.after_step, watchdog_id=args.watchdog_id, poll_factory=lambda: Relay(config, GoogleGmailGateway(config.gmail_auth_home), ProcessWorkerLauncher()))
        print(f"WATCHDOG_{result.upper()}")
        return 0
    if args.command in {"watchdog-ui", "monitor"}:
        from .watchdog_ui import run_watchdog_ui
        return run_watchdog_ui(config)
    return 0


def _spawn_watchdog(config, run_id: str, after_step: int) -> dict:
    from .watchdog import spawn_watchdog
    return spawn_watchdog(config, run_id=run_id, after_step=after_step)


if __name__ == "__main__":
    raise SystemExit(main())
