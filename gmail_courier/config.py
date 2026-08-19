from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import tomllib
from typing import Any

from .url import is_chat_url


DEFAULT_HOME_NAME = "GmailCourier"
CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]*$")


def home_dir() -> Path:
    configured = os.environ.get("GMAIL_COURIER_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / DEFAULT_HOME_NAME
    return Path.home() / ".local" / DEFAULT_HOME_NAME


@dataclass(frozen=True)
class ProjectConfig:
    code: str
    aliases: tuple[str, ...]
    root: Path
    inbox: Path
    branch: str
    remote: str
    remote_url: str | None
    push: bool
    legacy_prefixes: tuple[str, ...]
    chat_url: str = ""

    @property
    def codes(self) -> tuple[str, ...]:
        return (self.code, *self.aliases)


@dataclass(frozen=True)
class CourierConfig:
    address: str
    prefix: str
    legacy_prefix: str
    projects: tuple[ProjectConfig, ...]

    def project_by_code(self, code: str) -> ProjectConfig | None:
        normalized = code.upper()
        for project in self.projects:
            if normalized in project.codes:
                return project
        return None

    def legacy_projects(self, prefix: str) -> tuple[ProjectConfig, ...]:
        return tuple(project for project in self.projects if prefix.upper() in {item.upper() for item in project.legacy_prefixes})


def config_path(home: Path | None = None) -> Path:
    return (home or home_dir()) / "projects.toml"


def _path(value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return Path(value).expanduser().resolve()


def _inbox_path(value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty path")
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    if ".." in path.parts:
        raise ValueError(f"{field} relative paths must stay inside the project root")
    return path


def _codes(value: Any, field: str, required: bool = True) -> tuple[str, ...]:
    if value is None and not required:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    result = []
    for item in value:
        if not isinstance(item, str) or not CODE_RE.fullmatch(item.upper()):
            raise ValueError(f"{field} contains invalid project code: {item!r}")
        result.append(item.upper())
    return tuple(dict.fromkeys(result))


def load_config(home: Path | None = None) -> CourierConfig:
    path = config_path(home)
    if not path.exists():
        raise FileNotFoundError(f"Courier registry not found: {path}; create it from the generic projects.example.toml template")
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    protocol = raw.get("protocol", {})
    account = raw.get("account", {})
    address = account.get("address") if isinstance(account, dict) else None
    prefix = protocol.get("prefix")
    legacy_prefix = protocol.get("legacy_prefix", "")
    if not isinstance(address, str) or "@" not in address:
        raise ValueError("account.address must be configured as a valid email address")
    if not isinstance(prefix, str) or not prefix.startswith("[") or not prefix.endswith("]"):
        raise ValueError("protocol.prefix must be configured as a bracketed marker")
    if not isinstance(legacy_prefix, str) or (legacy_prefix and not legacy_prefix.startswith("[")):
        raise ValueError("protocol.legacy_prefix must be empty or a bracketed marker")
    entries = raw.get("projects", [])
    if not isinstance(entries, list) or not entries:
        raise ValueError("at least one [[projects]] entry is required")
    projects: list[ProjectConfig] = []
    seen: dict[str, str] = {}
    for index, item in enumerate(entries, 1):
        if not isinstance(item, dict):
            raise ValueError(f"projects entry {index} must be a table")
        code = str(item.get("code", "")).upper()
        if not CODE_RE.fullmatch(code):
            raise ValueError(f"projects entry {index} has invalid code")
        aliases = _codes(item.get("aliases", []), f"projects[{index}].aliases", required=False)
        all_codes = (code, *aliases)
        for candidate in all_codes:
            if candidate in seen:
                raise ValueError(f"project code {candidate} is ambiguous between {seen[candidate]} and {code}")
            seen[candidate] = code
        root = _path(item.get("root"), f"projects[{index}].root")
        if not root.is_dir():
            raise ValueError(f"projects[{index}].root must exist and be a directory: {root}")
        inbox_value = item.get("inbox", f"inbox/{code.lower()}")
        inbox = _inbox_path(inbox_value, f"projects[{index}].inbox")
        branch = item.get("branch", "")
        remote = item.get("remote", "origin")
        if not isinstance(branch, str) or not branch.strip():
            raise ValueError(f"projects[{index}].branch must be non-empty")
        if not isinstance(remote, str) or not remote.strip():
            raise ValueError(f"projects[{index}].remote must be non-empty")
        remote_url = item.get("remote_url")
        if remote_url is not None and not isinstance(remote_url, str):
            raise ValueError(f"projects[{index}].remote_url must be a string")
        push = bool(item.get("push", True))
        if push and inbox.is_absolute():
            try:
                inbox.relative_to(root)
            except ValueError as exc:
                raise ValueError(f"projects[{index}].push must be false when inbox is outside root") from exc
        legacy = item.get("legacy_prefixes", [])
        if not isinstance(legacy, list) or any(not isinstance(value, str) for value in legacy):
            raise ValueError(f"projects[{index}].legacy_prefixes must be an array of strings")
        chat_url = item.get("chat_url", "")
        if not isinstance(chat_url, str) or (chat_url and not is_chat_url(chat_url)):
            raise ValueError(f"projects[{index}].chat_url must be empty or a valid HTTPS ChatGPT conversation URL")
        projects.append(ProjectConfig(code, aliases, root, inbox, branch, remote, remote_url, push, tuple(legacy), chat_url))
    return CourierConfig(address, prefix, legacy_prefix, tuple(projects))


def write_default_config(home: Path | None = None) -> Path:
    target_home = home or home_dir()
    target_home.mkdir(parents=True, exist_ok=True)
    target = config_path(target_home)
    target.write_text(
        "# Generic template. Fill every required value before starting the courier.\n"
        "[account]\n"
        'address = ""\n\n'
        "[protocol]\n"
        'prefix = "[AUTOMATION]"\n'
        'legacy_prefix = ""\n\n'
        "[[projects]]\n"
        'code = ""\n'
        'aliases = []\n'
        'root = ""\n'
        '# Omit inbox for the safe default: <root>/inbox/<project-id>.\n'
        '# Set inbox to a relative path under root or an explicit absolute path.\n'
        'branch = ""\n'
        'remote = "origin"\n'
        'push = false\n'
        'legacy_prefixes = []\n'
        'chat_url = ""\n',
        encoding="utf-8",
    )
    return target
