from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from .config import config_path, home_dir, load_config, write_default_config
from .core import SCOPES, configure_logging, now, sync


TASK_NAME = "GmailCourier"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def status_path(home: Path) -> Path:
    return home / "status.json"


def write_status(home: Path, **values) -> None:
    path = status_path(home)
    prior = {}
    try:
        prior = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    prior.update(values)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(prior, indent=2, sort_keys=True), encoding="utf-8")


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def read_status(home: Path, stale_seconds: int = 90) -> dict:
    try:
        data = json.loads(status_path(home).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"state": "STOPPED"}
    pid = data.get("pid")
    try:
        age = time.time() - __import__("datetime").datetime.fromisoformat(data["last_poll_at"]).timestamp()
    except (KeyError, ValueError):
        age = float("inf")
    if data.get("state") == "running" and isinstance(pid, int) and pid_alive(pid) and age <= stale_seconds:
        data["state"] = "HEALTHY"
    elif data.get("last_error"):
        data["state"] = "ERROR"
    else:
        data["state"] = "STALE"
    data["heartbeat_age_seconds"] = None if age == float("inf") else round(age, 1)
    return data


def auth(args) -> int:
    from google_auth_oauthlib.flow import InstalledAppFlow

    home = home_dir()
    home.mkdir(parents=True, exist_ok=True)
    client = Path(args.client).expanduser().resolve() if args.client else home / "oauth-client.json"
    if not client.exists():
        raise RuntimeError(f"OAuth client JSON not found: {client}")
    flow = InstalledAppFlow.from_client_secrets_file(str(client), SCOPES)
    credentials = flow.run_local_server(port=0)
    (home / "token.json").write_text(credentials.to_json(), encoding="utf-8")
    print(f"OAuth token saved in {home / 'token.json'}")
    return 0


def once(_args) -> int:
    print(f"received={sync(home_dir())}")
    return 0


def daemon(args) -> int:
    home = home_dir()
    configure_logging(home)
    stopping = False

    def stop_handler(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    write_status(home, state="running", pid=os.getpid(), started_at=now(), last_poll_at=now(), last_success_at=None, last_error=None)
    while not stopping:
        write_status(home, state="running", pid=os.getpid(), last_poll_at=now())
        try:
            sync(home)
            write_status(home, state="running", pid=os.getpid(), last_success_at=now(), last_error=None)
        except Exception as exc:
            logging.getLogger("gmail_courier").exception("courier poll failed")
            write_status(home, state="running", pid=os.getpid(), last_error=str(exc))
        for _ in range(args.interval):
            if stopping:
                break
            time.sleep(1)
    write_status(home, state="stopped", pid=os.getpid(), last_poll_at=now())
    return 0


def ensure(args) -> int:
    home = home_dir()
    current = read_status(home, args.stale_seconds)
    if current["state"] == "HEALTHY":
        print("HEALTHY")
        return 0
    pid = current.get("pid")
    if isinstance(pid, int):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and pid_alive(pid):
            time.sleep(0.1)
        if pid_alive(pid):
            raise RuntimeError("stale daemon did not stop safely")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen(
        [sys.executable, "-m", "gmail_courier.cli", "run", "--interval", str(args.interval)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=flags,
    )
    print("STARTED")
    return 0


def stop(args) -> int:
    current = read_status(home_dir(), args.stale_seconds)
    pid = current.get("pid")
    if not isinstance(pid, int):
        print("STOPPED")
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
        print("STOPPING")
    except OSError:
        print("STOPPED")
    return 0


def registry_autostart(command: str, uninstall: bool = False) -> None:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if uninstall:
            try:
                winreg.DeleteValue(key, TASK_NAME)
            except FileNotFoundError:
                pass
        else:
            winreg.SetValueEx(key, TASK_NAME, 0, winreg.REG_SZ, command)


def scheduler(args, uninstall: bool = False) -> int:
    command = f'"{sys.executable}" -m gmail_courier.cli run --interval {args.interval}'
    if uninstall:
        subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], capture_output=True, text=True)
        registry_autostart(command, uninstall=True)
        print("UNINSTALLED")
        return 0
    result = subprocess.run(["schtasks", "/Create", "/TN", TASK_NAME, "/SC", "ONLOGON", "/TR", command, "/RL", "LIMITED", "/F"], capture_output=True, text=True)
    if result.returncode:
        registry_autostart(command)
        print("INSTALLED registry-run-key")
        return 0
    print("INSTALLED task-scheduler")
    return 0


def init_config(_args) -> int:
    target = config_path(home_dir())
    if target.exists():
        print(f"EXISTS {target}")
    else:
        print(f"CREATED {write_default_config()}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="gmail-courier")
    parser.add_argument("--interval", type=int, default=30)
    parser.add_argument("--stale-seconds", type=int, default=90)
    sub = parser.add_subparsers(dest="command", required=True)
    auth_parser = sub.add_parser("auth")
    auth_parser.add_argument("--client")
    sub.add_parser("init")
    sub.add_parser("once")
    for name in ("run", "ensure", "install-autostart", "uninstall-autostart"):
        command_parser = sub.add_parser(name)
        command_parser.add_argument("--interval", type=int, default=argparse.SUPPRESS)
    for name in ("status", "stop"):
        command_parser = sub.add_parser(name)
        command_parser.add_argument("--stale-seconds", type=int, default=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.command == "auth":
            return auth(args)
        if args.command == "init":
            return init_config(args)
        if args.command == "once":
            return once(args)
        if args.command == "run":
            return daemon(args)
        if args.command == "status":
            print(json.dumps(read_status(home_dir(), args.stale_seconds), indent=2))
            return 0
        if args.command == "ensure":
            return ensure(args)
        if args.command == "stop":
            return stop(args)
        return scheduler(args, args.command == "uninstall-autostart")
    except Exception as exc:
        logging.basicConfig(level=logging.ERROR)
        logging.exception("gmail-courier failed")
        try:
            write_status(home_dir(), state="error", last_error=str(exc), last_poll_at=now())
        except OSError:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
