from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import tomllib
from urllib.parse import urlsplit

DEFAULT_CHAT_URL = ""
DEFAULT_WORKFLOW_WINDOW_SECONDS = 300
CHAT_HOSTS = {"chatgpt.com", "www.chatgpt.com"}
CONVERSATION_PATH_RE = re.compile(r"(?:^|/)c/([A-Za-z0-9_-]+)(?:/|$)")
PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


def is_chat_url(value: str) -> bool:
    try:
        parsed = urlsplit(str(value))
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme.lower() == "https"
        and parsed.hostname is not None
        and parsed.hostname.lower() in CHAT_HOSTS
        and port is None
        and parsed.username is None
        and parsed.password is None
        and CONVERSATION_PATH_RE.search(parsed.path) is not None
    )


def normalize_chat_url(value: str) -> str:
    """Return a conversation identity while ignoring UI/project URL wrappers."""
    if not is_chat_url(value):
        raise ValueError("invalid ChatGPT conversation URL")
    parsed = urlsplit(str(value))
    conversation = CONVERSATION_PATH_RE.search(parsed.path)
    assert conversation is not None
    return f"https://{parsed.hostname.lower()}/c/{conversation.group(1)}"


def chat_urls_match(left: str, right: str) -> bool:
    try:
        return normalize_chat_url(left) == normalize_chat_url(right)
    except ValueError:
        return False


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
    codex_command: str = "codex"
    dev_session_id: str = ""
    handoff_command: str = ""


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
    if not isinstance(project_id, str) or not PROJECT_ID_RE.fullmatch(project_id):
        raise ValueError("project_id must be a non-empty lowercase identifier")
    def expand(value: object, field: str) -> Path:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must be configured as a non-empty path")
        return Path(os.path.expandvars(value)).expanduser().resolve()
    target_type = str(raw["target_type"])
    if target_type not in {"mock", "codex-cli", "codex-app-server"}:
        raise ValueError("target_type must be mock, codex-cli, or codex-app-server")
    if target_type in {"codex-cli", "codex-app-server"} and not is_chat_url(str(raw["chat_url"])):
        raise ValueError("chat_url must be an HTTPS ChatGPT URL containing /c/<conversation-id>")
    if target_type in {"codex-cli", "codex-app-server"} and not str(raw.get("target_id", "")).strip():
        raise ValueError("target_id must be configured for a non-mock target")
    display_name = raw["display_name"]
    channel_id = raw["channel_id"]
    target_label = raw["target_label"]
    if not all(isinstance(value, str) and value.strip() for value in (display_name, channel_id, target_label)):
        raise ValueError("display_name, channel_id, and target_label must be configured")
    if not channel_id.startswith("AR-"):
        raise ValueError("channel_id must start with AR-")
    repo_path = expand(raw["repo_path"], "repo_path")
    if not repo_path.is_dir():
        raise ValueError(f"repo_path must exist and be a directory: {repo_path}")
    local_storage = expand(raw["local_project_storage"], "local_project_storage")
    gmail_home = expand(raw["gmail_auth_home"], "gmail_auth_home")
    if not gmail_home.is_dir():
        raise ValueError(f"gmail_auth_home must exist and be a directory: {gmail_home}")
    return RelayConfig(project_id, display_name, channel_id, repo_path, local_storage, target_type, str(raw.get("target_id", "")), target_label, str(raw["chat_url"]), interval, bool(raw.get("enabled", True)), gmail_home, str(raw.get("codex_command", "codex")), str(raw.get("dev_session_id", "")), str(raw.get("handoff_command", "")))


def save_binding(
    home: Path,
    *,
    project_id: str,
    display_name: str,
    channel_id: str,
    repo_path: Path,
    local_project_storage: Path,
    gmail_auth_home: Path,
    target_id: str,
    target_type: str = "codex-cli",
    chat_url: str = DEFAULT_CHAT_URL,
    dev_session_id: str = "",
) -> Path:
    if not isinstance(project_id, str) or not PROJECT_ID_RE.fullmatch(project_id):
        raise ValueError("project_id must be a non-empty lowercase identifier")
    if not all(isinstance(value, str) and value.strip() for value in (display_name, channel_id)):
        raise ValueError("display_name and channel_id must be configured")
    if not channel_id.startswith("AR-"):
        raise ValueError("channel_id must start with AR-")
    if target_type not in {"mock", "codex-cli", "codex-app-server"}:
        raise ValueError("target_type must be mock, codex-cli, or codex-app-server")
    if not repo_path.expanduser().resolve().is_dir():
        raise ValueError("repo_path must exist and be a directory")
    if not gmail_auth_home.expanduser().resolve().is_dir():
        raise ValueError("gmail_auth_home must exist and be a directory")
    if target_type != "mock" and (not isinstance(target_id, str) or not target_id.strip() or not is_chat_url(chat_url)):
        raise ValueError("non-mock binding requires target_id and a valid ChatGPT URL")
    target = config_path(home)
    target.parent.mkdir(parents=True, exist_ok=True)
    values = {
        "project_id": project_id,
        "display_name": display_name,
        "channel_id": channel_id,
        "repo_path": str(repo_path.expanduser().resolve()),
        "local_project_storage": str(local_project_storage.expanduser().resolve()),
        "target_type": target_type,
        "target_id": target_id,
        "dev_session_id": dev_session_id,
        "target_label": "configured worker",
        "chat_url": chat_url,
        "gmail_auth_home": str(gmail_auth_home.expanduser().resolve()),
    }
    text = "[project]\n" + "\n".join(f'{key} = {json.dumps(value)}' for key, value in values.items()) + "\npoll_interval = 20\nenabled = true\ncodex_command = \"codex\"\nhandoff_command = \"\"\n"
    target.write_text(text, encoding="utf-8")
    return target


def write_example(path: Path) -> None:
    text = '''# Generic template. Replace every empty value before starting the relay.\n[project]\nproject_id = ""\ndisplay_name = ""\nchannel_id = ""\nrepo_path = ""\nlocal_project_storage = ""\ntarget_type = "mock"\ntarget_id = ""\ntarget_label = ""\nchat_url = ""\npoll_interval = 20\nenabled = true\ngmail_auth_home = ""\ncodex_command = "codex"\nhandoff_command = ""\n'''
    path.write_text(text, encoding="utf-8")
