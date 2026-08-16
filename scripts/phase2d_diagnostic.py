"""Run one bounded Phase 2D diagnostic lease through the real Supervisor path."""
from __future__ import annotations

import time

from agent_relay.config import app_home, load_config
from agent_relay.gmail import GoogleGmailGateway
from agent_relay.supervisor import Supervisor
from agent_relay.wake import CodexCliWakeAdapter, CodexTarget, LeaseKind


MESSAGE_ID = "1a00a9fcb9f9f817"


def main() -> int:
    config = load_config(app_home())
    target = CodexTarget(config.target_type, config.target_id, config.target_label, config.repo_path)
    adapter = CodexCliWakeAdapter(target, config.local_project_storage / "logs", config.codex_command)
    relay = Supervisor(config, GoogleGmailGateway(config.gmail_auth_home), adapter)
    relay.start()
    result = relay.process_message_id(MESSAGE_ID, LeaseKind.DIAGNOSTIC)
    print(f"wake_result={result} state={relay.snapshot()['state']}", flush=True)
    lease = relay.snapshot().get("active_lease") or {}
    print(f"lease_id={lease.get('lease_id')} token_present={bool(lease.get('completion_token'))}", flush=True)
    for _ in range(60):
        time.sleep(1)
        if relay.consume_completion_record():
            print(f"completion_consumed state={relay.snapshot()['state']} active={relay.snapshot().get('active_lease')}", flush=True)
            return 0
    print(f"completion_timeout state={relay.snapshot()['state']}", flush=True)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
