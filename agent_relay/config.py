from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from pathlib import Path
import tomllib

EXPECTED_CHAT_URL = "https://chatgpt.com/c/6a818a0c-5208-83ee-95cd-fd558d66ecc9"


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
    codex_command: str = "codex.cmd"
    dev_session_id: str = ""


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
    target_type = str(raw["target_type"])
    if target_type not in {"mock", "codex-cli", "codex-app-server"}:
        raise ValueError("target_type must be mock, codex-cli, or codex-app-server")
    if target_type in {"codex-cli", "codex-app-server"} and str(raw["chat_url"]) != EXPECTED_CHAT_URL:
        raise ValueError("chat_url must be the configured fixed ChatGPT conversation")
    return RelayConfig(project_id, str(raw["display_name"]), str(raw["channel_id"]), expand(raw["repo_path"]), expand(raw["local_project_storage"]), target_type, str(raw.get("target_id", "")), str(raw["target_label"]), str(raw["chat_url"]), interval, bool(raw.get("enabled", True)), expand(raw["gmail_auth_home"]), str(raw.get("codex_command", "codex.cmd")), str(raw.get("dev_session_id", "")))


def save_binding(home: Path, *, target_id: str, target_type: str = "codex-cli", chat_url: str = EXPECTED_CHAT_URL, dev_session_id: str = "") -> Path:
    target = config_path(home)
    target.parent.mkdir(parents=True, exist_ok=True)
    repo = Path.cwd().resolve()
    storage = app_home() / "projects" / "gmail-courier"
    from gmail_courier.config import home_dir
    target.write_text("[project]\n" + f'project_id = "gmail-courier"\n' + 'display_name = "Gmail Courier"\n' + 'channel_id = "AR-GMAILCOURIER-A1R7P"\n' + f'repo_path = "{repo.as_posix()}"\n' + f'local_project_storage = "{storage.as_posix()}"\n' + f'target_type = "{target_type}"\n' + f'target_id = "{target_id}"\n' + f'dev_session_id = "{dev_session_id}"\n' + 'target_label = "AgentRelay Dedicated Worker"\n' + f'chat_url = "{chat_url}"\n' + 'poll_interval = 20\nenabled = true\n' + f'gmail_auth_home = "{home_dir().as_posix()}"\n' + 'codex_command = "codex.cmd"\n', encoding="utf-8")
    return target


def write_example(path: Path, repo_path: Path) -> None:
    text = f'''# Copy this file to %LOCALAPPDATA%/AgentRelay/agentrelay.toml and edit the target fields.\n[project]\nproject_id = "gmail-courier"\ndisplay_name = "Gmail Courier"\nchannel_id = "AR-GMAILCOURIER-A1R7P"\nrepo_path = "{repo_path.as_posix()}"\nlocal_project_storage = "%LOCALAPPDATA%/AgentRelay/projects/gmail-courier"\ntarget_type = "mock"\ntarget_id = ""\ntarget_label = "Gmail Courier Phase 1 mock"\nchat_url = ""\npoll_interval = 20\nenabled = true\ngmail_auth_home = "%LOCALAPPDATA%/GmailCourier"\n'''
    path.write_text(text, encoding="utf-8")
