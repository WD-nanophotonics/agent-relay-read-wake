"""Exercise Courier's queue-to-receipt path without starting a browser."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time


stage = "startup"


def main() -> int:
    global stage
    parser = argparse.ArgumentParser(
        description="Probe queue/receipt lifecycle without Chrome, Chat, or network."
    )
    parser.add_argument("base_request_directory")
    parser.add_argument("--log", required=True)
    args = parser.parse_args()
    log = Path(args.log).resolve()
    log.parent.mkdir(parents=True, exist_ok=True)

    def emit_local(name: str, **values: object) -> None:
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
        with log.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(line + "\n")

    def interrupted(signum: int, _frame: object) -> None:
        emit_local("console_signal_received", signal=signal.Signals(signum).name)
        raise SystemExit(130)

    signal.signal(signal.SIGINT, interrupted)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, interrupted)

    stage = "before_import"
    emit_local("probe_checkpoint")
    from chat_courier.model import load_request, minimum_caller_window_seconds
    from chat_courier.queue import CourierQueue
    from chat_courier.storage import event, receipt

    stage = "before_base_request_load"
    emit_local("probe_checkpoint")
    base = load_request(args.base_request_directory)
    with tempfile.TemporaryDirectory(prefix="chat-courier-pre-browser-") as temporary:
        directory = Path(temporary)
        request_id = f"COURIER-PREBROWSER-{int(time.time())}-{os.getpid()}"
        manifest = {
            "version": 1,
            "project_id": base.project_id,
            "request_id": request_id,
            "message_file": "message.txt",
            "attachments": [],
            "workflow_window_seconds": 30,
            "queue_wait_seconds": 30,
        }
        (directory / "request.json").write_text(json.dumps(manifest), encoding="utf-8")
        (directory / "message.txt").write_text("Local pre-browser diagnostic only. No Chat message is sent.\n", encoding="utf-8")
        request = load_request(directory)
        queue = CourierQueue(request)
        try:
            stage = "before_queue_join"
            emit_local("probe_checkpoint", request_id=request.request_id)
            queue.join()
            while True:
                status = queue.poll()
                if status.state == "turn_acquired":
                    break
                if status.state != "waiting":
                    emit_local("probe_failed", queue_state=status.state)
                    return 1
                time.sleep(1)
            stage = "after_queue_turn"
            emit_local("probe_checkpoint", queue_ticket=status.ticket)
            event(request, "request_validated", phase="diagnostic")
            stage = "after_request_validated_event"
            emit_local(
                "probe_checkpoint",
                minimum_caller_window_seconds=minimum_caller_window_seconds(
                    request.queue_wait_seconds, request.workflow_window_seconds
                ),
            )
            event(request, "submission_intent_writing", phase="diagnostic")
            stage = "before_receipt_atomic_write"
            emit_local("probe_checkpoint")
            receipt(request, "diagnostic_pre_browser_complete", "No browser or Chat submission was started")
            stage = "after_receipt_atomic_write"
            emit_local("probe_completed", temporary_request_directory=str(directory))
            return 0
        finally:
            queue.complete()


if __name__ == "__main__":
    raise SystemExit(main())
