"""Minimal Windows console-signal probe; it does not import Courier or open a browser."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import sys
import time


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", required=True)
    parser.add_argument("--seconds", type=int, default=30)
    args = parser.parse_args()
    log = Path(args.log)
    log.parent.mkdir(parents=True, exist_ok=True)

    def emit(event: str, **values: object) -> None:
        record = {"event": event, "pid": os.getpid(), "parent_pid": os.getppid(), "time": time.time(), **values}
        line = json.dumps(record, sort_keys=True)
        print(line, flush=True)
        with log.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(line + "\n")

    def interrupted(signum: int, _frame: object) -> None:
        emit("console_signal_received", signal=signal.Signals(signum).name)
        raise SystemExit(130)

    signal.signal(signal.SIGINT, interrupted)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, interrupted)

    emit("probe_started", seconds=args.seconds)
    for elapsed in range(args.seconds):
        time.sleep(1)
        if (elapsed + 1) % 5 == 0:
            emit("probe_heartbeat", elapsed_seconds=elapsed + 1)
    emit("probe_completed", elapsed_seconds=args.seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
