from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
import tomllib

from .url import is_chat_url


IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ChatUrlReplacementRequired(ValueError):
    def __init__(self, project_id: str, current_url: str, requested_url: str):
        super().__init__(f"project {project_id} already has a ChatGPT URL; explicit replacement confirmation is required")
        self.project_id = project_id
        self.current_url = current_url
        self.requested_url = requested_url


def registry_path(home: Path) -> Path:
    return home / "chat_registry.json"


def _read(home: Path) -> dict:
    path = registry_path(home)
    if not path.exists():
        return {"version": 1, "projects": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"chat URL registry is not valid UTF-8 JSON: {path}") from exc
    if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("projects"), dict):
        raise ValueError(f"chat URL registry has an invalid schema: {path}")
    return value


def _write(home: Path, value: dict) -> None:
    home.mkdir(parents=True, exist_ok=True)
    target = registry_path(home)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=home) as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, target)


def _validate_project_id(project_id: str) -> str:
    if not isinstance(project_id, str) or not IDENTIFIER_RE.fullmatch(project_id):
        raise ValueError("project_id must be a non-empty ASCII identifier")
    return project_id.upper()


def current_chat_url(home: Path, project_id: str) -> str | None:
    canonical = _validate_project_id(project_id)
    data = _read(home)
    entry = data["projects"].get(canonical)
    if not isinstance(entry, dict):
        return None
    value = entry.get("active_url")
    return value if isinstance(value, str) and is_chat_url(value) else None


def legacy_default_chat_url(project_id: str) -> str | None:
    """Read the prior AgentRelay binding as a local-only compatibility fallback."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    relay_home = Path(os.environ.get("AGENT_RELAY_HOME", Path(local_app_data or Path.home() / ".local") / "AgentRelay"))
    path = relay_home.expanduser().resolve() / "agentrelay.toml"
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    project = raw.get("project")
    if not isinstance(project, dict):
        return None
    if str(project.get("project_id", "")).lower() != str(project_id).lower():
        return None
    value = project.get("chat_url")
    return value if isinstance(value, str) and is_chat_url(value) else None


def register_chat_url(home: Path, project_id: str, url: str, *, confirm_replace: bool = False) -> dict:
    canonical = _validate_project_id(project_id)
    if not isinstance(url, str) or not is_chat_url(url):
        raise ValueError("chat_url must be an HTTPS ChatGPT conversation URL")
    data = _read(home)
    prior = data["projects"].get(canonical)
    current = prior.get("active_url") if isinstance(prior, dict) else None
    if current == url:
        return {"project_id": canonical, "active_url": url, "changed": False, "history_count": len(prior.get("history", [])) if isinstance(prior, dict) else 1}
    if isinstance(current, str) and current and not confirm_replace:
        raise ChatUrlReplacementRequired(canonical, current, url)
    history = list(prior.get("history", [])) if isinstance(prior, dict) and isinstance(prior.get("history", []), list) else []
    if not any(isinstance(item, dict) and item.get("url") == url for item in history):
        history.append({"url": url, "registered_at": datetime.now(timezone.utc).isoformat()})
    data["projects"][canonical] = {"active_url": url, "history": history}
    _write(home, data)
    return {"project_id": canonical, "active_url": url, "changed": True, "history_count": len(history)}


def list_chat_urls(home: Path, project_id: str) -> dict:
    canonical = _validate_project_id(project_id)
    data = _read(home)
    entry = data["projects"].get(canonical)
    if not isinstance(entry, dict):
        return {"project_id": canonical, "active_url": None, "history": []}
    history = entry.get("history", [])
    return {"project_id": canonical, "active_url": entry.get("active_url"), "history": history if isinstance(history, list) else []}
