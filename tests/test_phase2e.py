from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from agent_relay.wake import LeaseKind, WakeLease, diagnostic_completion_command, wake_instruction


def _write_config(home: Path, repo: Path, storage: Path) -> None:
    home.mkdir(parents=True)
    (home / "agentrelay.toml").write_text(
        "[project]\n"
        'project_id = "gmail-courier"\n'
        'display_name = "Gmail Courier"\n'
        'channel_id = "AR-GMAILCOURIER-A1R7P"\n'
        f'repo_path = "{repo.as_posix()}"\n'
        f'local_project_storage = "{storage.as_posix()}"\n'
        'target_type = "mock"\n'
        'target_id = ""\n'
        'target_label = "test"\n'
        'chat_url = ""\n'
        'poll_interval = 20\n'
        'enabled = true\n'
        f'gmail_auth_home = "{(home / "gmail").as_posix()}"\n',
        encoding="utf-8",
    )


def test_generated_diagnostic_command_executes_real_cli_once(tmp_path: Path):
    home = tmp_path / "agentrelay"
    storage = tmp_path / "configured-project-storage"
    _write_config(home, Path(__file__).parents[1], storage)
    lease = WakeLease.create("gmail-courier", "RUN-TEST", 5, tmp_path / "staged", LeaseKind.DIAGNOSTIC, "worker")
    command = diagnostic_completion_command(lease)
    assert command in wake_instruction(lease)
    parts = command.split()
    assert parts[:4] == ["python", "-m", "agent_relay.cli", "complete-diagnostic"]
    env = dict(os.environ, AGENT_RELAY_HOME=str(home))
    wrong = subprocess.run(
        [sys.executable, *parts[1:4], lease.lease_id, *parts[6:]],
        cwd=Path(__file__).parents[1],
        env=env,
        capture_output=True,
        text=True,
    )
    assert wrong.returncode != 0
    assert not (storage / "completions" / f"{lease.lease_id}.json").exists()
    unsafe = subprocess.run(
        [sys.executable, "-m", "agent_relay.cli", "complete-diagnostic", "--lease-id", "..\\escape", "--completion-token", lease.completion_token],
        cwd=Path(__file__).parents[1],
        env=env,
        capture_output=True,
        text=True,
    )
    assert unsafe.returncode != 0
    assert not (storage.parent / "escape.json").exists()
    completed = subprocess.run(
        [sys.executable, *parts[1:]],
        cwd=Path(__file__).parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    receipt = storage / "completions" / f"{lease.lease_id}.json"
    assert receipt.exists()
    data = json.loads(receipt.read_text(encoding="utf-8"))
    assert data == {
        "completion_token": lease.completion_token,
        "handoff_succeeded": False,
        "lease_id": lease.lease_id,
        "lease_kind": "DIAGNOSTIC",
        "outcome": "completed",
        "protocol": "AGENTRELAY_COMPLETION/1",
        "recorded_at": data["recorded_at"],
    }
    assert [p.name for p in storage.rglob("*") if p.is_file()] == [receipt.name]
    assert not (home / "projects" / "gmail-courier").exists()
