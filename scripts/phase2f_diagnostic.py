"""Run exactly one Phase 2F diagnostic through the Supervisor-owned App Server."""
from __future__ import annotations

import ctypes
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_relay.config import app_home, load_config
from agent_relay.gmail import GoogleGmailGateway
from agent_relay.supervisor import Supervisor
from agent_relay.wake import CodexAppServerWakeAdapter, CodexTarget, LeaseKind


MESSAGE_ID = "1a00b1d38bece679"


def foreground() -> dict[str, int]:
    user32 = ctypes.windll.user32
    hwnd = int(user32.GetForegroundWindow())
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    name = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, name, 256)
    return {"hwnd": hwnd, "pid": int(pid.value), "is_console": int(name.value in {"ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS"})}


def console_count() -> int:
    user32 = ctypes.windll.user32
    count = 0
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def callback(hwnd, _lparam):
        nonlocal count
        if user32.IsWindowVisible(hwnd):
            name = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, name, 256)
            count += int(name.value in {"ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS"})
        return True
    user32.EnumWindows(callback_type(callback), 0)
    return count


def main() -> int:
    config = load_config(app_home())
    target = CodexTarget(config.target_type, config.target_id, config.target_label, config.repo_path)
    adapter = CodexAppServerWakeAdapter(target, config.local_project_storage / "logs", config.codex_command, config.local_project_storage, config.dev_session_id)
    relay = Supervisor(config, GoogleGmailGateway(config.gmail_auth_home), adapter)
    evidence = {"message_id": MESSAGE_ID, "target_type": config.target_type, "dev_session_id": config.dev_session_id, "foreground_before": foreground(), "console_count_before": console_count()}
    try:
        relay.start()
        evidence.update({"backend_pid": adapter.controller.pid if adapter.controller else None, "worker_id": adapter.worker_id, "superseded_worker_id": adapter.superseded_worker_id, "state_after_start": relay.snapshot()["state"]})
        result = relay.process_message_id(MESSAGE_ID, LeaseKind.DIAGNOSTIC)
        snap = relay.snapshot()
        lease = snap.get("active_lease") or {}
        evidence.update({"wake_result": result, "adapter_error": adapter.last_error, "worker_status_after_wake": adapter.worker_status, "state_after_wake": snap["state"], "lease_id": lease.get("lease_id"), "turn_id": lease.get("turn_id"), "staged_instruction_path": lease.get("staged_instruction_path")})
        print(json.dumps(evidence, ensure_ascii=False), flush=True)
        if result != "wake-accepted":
            return 2
        deadline = time.monotonic() + 240
        consumed = False
        while time.monotonic() < deadline:
            time.sleep(1)
            if relay.consume_completion_record():
                consumed = True
                break
        final = relay.snapshot()
        evidence.update({"completion_consumed": consumed, "final_state_before_stop": final["state"], "active_lease_before_stop": final.get("active_lease"), "worker_status": adapter.worker_status, "turn_status": adapter.last_turn_status, "foreground_after": foreground(), "console_count_after": console_count()})
        if not consumed:
            relay.fail_active_lease("Phase 2F diagnostic completion timed out")
            return 3
        return 0
    except Exception as exc:
        evidence.update({"exception": str(exc), "state_on_exception": relay.snapshot().get("state")})
        if relay.snapshot().get("active_lease"):
            relay.fail_active_lease(str(exc))
        return 4
    finally:
        evidence["worker_status_before_stop"] = adapter.worker_status
        evidence_path = config.local_project_storage / "diagnostics" / "phase2f-evidence.json"
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        relay.stop()
        print(json.dumps({"evidence_path": str(evidence_path), "stopped_state": relay.snapshot()["state"]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
