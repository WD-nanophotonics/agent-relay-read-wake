from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from pathlib import Path
import tomllib


def app_home() -> Path:
    configured = os.environ.get("AGENT_RELAY_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local")) / "AgentRelay"


@dataclass(frozen=True)
class RelayConfig:
    project_id: str
    display_name: str
    channel_id: str
    repo_path: Path
    local_project_storage: Path
    target_type: str
    target_id: str
    target_label: str
    chat_url: str
    poll_interval: int
    enabled: bool
    gmail_auth_home: Path


def config_path(home: Path | None = None) -> Path:
    return (home or app_home()) / "agentrelay.toml"


def load_config(home: Path | None = None) -> RelayConfig:
    path = config_path(home)
    if not path.exists():
        raise FileNotFoundError(f"AgentRelay configuration not found: {path}")
    raw = tomllib.loads(path.read_text(encoding="utf-8")).get("project")
    if not isinstance(raw, dict):
        raise ValueError("[project] configuration is required")
    required = ("project_id", "display_name", "channel_id", "repo_path", "local_project_storage", "target_type", "target_label", "chat_url", "poll_interval", "gmail_auth_home")
    missing = [key for key in required if key not in raw]
    if missing:
        raise ValueError(f"missing project fields: {', '.join(missing)}")
    interval = raw["poll_interval"]
    if not isinstance(interval, int) or not 5 <= interval <= 3600:
        raise ValueError("poll_interval must be an integer from 5 to 3600")
    project_id = raw["project_id"]
    if not isinstance(project_id, str) or not project_id or project_id != project_id.lower():
        raise ValueError("project_id must be a non-empty lowercase identifier")
    expand = lambda value: Path(os.path.expandvars(str(value))).expanduser().resolve()
    return RelayConfig(project_id, str(raw["display_name"]), str(raw["channel_id"]), expand(raw["repo_path"]), expand(raw["local_project_storage"]), str(raw["target_type"]), str(raw.get("target_id", "")), str(raw["target_label"]), str(raw["chat_url"]), interval, bool(raw.get("enabled", True)), expand(raw["gmail_auth_home"]))


def write_example(path: Path, repo_path: Path) -> None:
    text = f'''# Copy this file to %LOCALAPPDATA%/AgentRelay/agentrelay.toml and edit the target fields.\n[project]\nproject_id = "gmail-courier"\ndisplay_name = "Gmail Courier"\nchannel_id = "AR-GMAILCOURIER-A1R7P"\nrepo_path = "{repo_path.as_posix()}"\nlocal_project_storage = "%LOCALAPPDATA%/AgentRelay/projects/gmail-courier"\ntarget_type = "mock"\ntarget_id = ""\ntarget_label = "Gmail Courier Phase 1 mock"\nchat_url = ""\npoll_interval = 20\nenabled = true\ngmail_auth_home = "%LOCALAPPDATA%/GmailCourier"\n'''
    path.write_text(text, encoding="utf-8")
