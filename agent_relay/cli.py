from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import app_home, config_path, load_config, save_binding, write_example
from .gmail import GoogleGmailGateway
from .relay import NoopWorkerLauncher, Relay
from .storage import StateStore
from .watchdog import run_watchdog
from .worker import OneShotWorker, ProcessWorkerLauncher


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="agent-relay")
    parser.add_argument("command", choices=("init", "poll-once", "worker", "watchdog", "stop", "status", "test-gmail", "test-wake", "bind"), nargs="?", default="status")
    parser.add_argument("--run")
    parser.add_argument("--step", type=int)
    parser.add_argument("--after-step", type=int)
    parser.add_argument("--staged")
    parser.add_argument("--worker-id")
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
    if args.command == "worker":
        if not args.run or args.step is None or not args.staged:
            parser.error("worker requires --run, --step, and --staged")
        worker = OneShotWorker(config, watchdog_spawn=lambda step, run: _spawn_watchdog(config, run, step))
        outcome = worker.run(run_id=args.run, step=args.step, staged_path=Path(args.staged), worker_id=args.worker_id)
        print(json.dumps({"ok": outcome.ok, "detail": outcome.detail}, ensure_ascii=False))
        return 0 if outcome.ok else 1
    if args.command == "watchdog":
        if not args.run or args.after_step is None:
            parser.error("watchdog requires --run and --after-step")
        result = run_watchdog(config, run_id=args.run, after_step=args.after_step, poll_factory=lambda: Relay(config, GoogleGmailGateway(config.gmail_auth_home), ProcessWorkerLauncher()))
        print(f"WATCHDOG_{result.upper()}")
        return 0
    return 0


def _spawn_watchdog(config, run_id: str, after_step: int) -> None:
    from .watchdog import spawn_watchdog
    spawn_watchdog(config, run_id=run_id, after_step=after_step)


if __name__ == "__main__":
    raise SystemExit(main())
