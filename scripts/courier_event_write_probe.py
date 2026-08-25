"""Isolate Courier's first request event write without queue, browser, or network."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import sys
import time


stage = "startup"


def _write_log(path: Path, name: str, **values: object) -> None:
    record = {
        "event": name,
        "stage": stage,
        "pid": os.getpid(),
        "parent_pid": os.getppid(),
        "time": time.time(),
        **values,
    }
    line = json.dumps(record, ensure_ascii=False, sort_keys=True)
    print(line, flush=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(line + "\n")


def main() -> int:
    global stage
    parser = argparse.ArgumentParser(
        description="Write exactly one normal Courier event; no queue, Chrome, or network."
    )
    parser.add_argument("request_directory")
    parser.add_argument("--log", required=True)
    args = parser.parse_args()
    request_directory = Path(args.request_directory).resolve()
    log = Path(args.log).resolve()
    log.parent.mkdir(parents=True, exist_ok=True)

    def interrupted(signum: int, _frame: object) -> None:
        _write_log(log, "console_signal_received", signal=signal.Signals(signum).name)
        raise SystemExit(130)

    signal.signal(signal.SIGINT, interrupted)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, interrupted)

    stage = "before_import"
    _write_log(log, "probe_checkpoint")
    # Import only the request parser and storage helper used by the first line
    # after queue_turn_acquired.  This deliberately does not import browser.py.
    from chat_courier.model import load_request
    from chat_courier.storage import event

    stage = "before_load_request"
    _write_log(log, "probe_checkpoint")
    request = load_request(request_directory)
    stage = "before_courier_event"
    _write_log(log, "probe_checkpoint")
    event(request, "request_validated", phase="diagnostic")
    stage = "after_courier_event"
    _write_log(log, "probe_completed", events_path=str(request.directory / "events.jsonl"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
