from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit

DEFAULT_WINDOW_SECONDS = 360
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

class ValidationError(ValueError):
    pass

def runtime_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "ChatCourier"

def _safe_relative(value: object, name: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{name} must be a non-empty relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValidationError(f"{name} must stay inside the request directory")
    return path

def valid_chat_url(value: object) -> bool:
    if not isinstance(value, str): return False
    try: parsed = urlsplit(value)
    except ValueError: return False
    if parsed.scheme != "https" or parsed.hostname not in {"chatgpt.com", "www.chatgpt.com"} or parsed.username or parsed.password or parsed.port: return False
    parts = [part for part in parsed.path.split("/") if part]
    return (len(parts) == 2 and parts[0] == "c") or (len(parts) >= 3 and parts[-2] == "c")

def _regular_file(path: Path, description: str) -> None:
    if not path.exists() or not path.is_file() or path.is_symlink():
        raise ValidationError(f"{description} must be an existing non-symlink regular file: {path}")

def _read_utf8(path: Path, description: str) -> str:
    _regular_file(path, description)
    # Windows PowerShell commonly emits a UTF-8 BOM. It is still UTF-8 input,
    # and must not make an otherwise local request unusable.
    try: return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc: raise ValidationError(f"{description} must be UTF-8: {path}") from exc

@dataclass(frozen=True)
class Request:
    directory: Path; project_id: str; request_id: str; message_path: Path; message: str
    attachments: tuple[Path, ...]; workflow_window_seconds: int; task_difficulty: str
    instruction_level: str; chat_url: str; fingerprint: str

def registry_path() -> Path: return runtime_root() / "chat_urls.json"

def _load_registry() -> dict[str, str]:
    path = registry_path()
    if not path.exists(): return {}
    try: raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc: raise ValidationError(f"invalid Chat URL registry: {path}") from exc
    if not isinstance(raw, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in raw.items()): raise ValidationError("invalid Chat URL registry schema")
    return raw

def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)

def register_url(project_id: str, url: str, *, replace: bool = False) -> dict[str, Any]:
    if not IDENTIFIER.fullmatch(project_id): raise ValidationError("project_id is invalid")
    if not valid_chat_url(url): raise ValidationError("url must be an HTTPS ChatGPT conversation URL")
    registry = _load_registry(); current = registry.get(project_id)
    if current and current != url and not replace: raise ValidationError(f"project {project_id} already has a registered URL; rerun with --replace")
    registry[project_id] = url; atomic_json(registry_path(), registry)
    return {"project_id": project_id, "url": url, "changed": current != url}

def load_request(directory: str | Path) -> Request:
    root = Path(directory).resolve(); manifest = root / "request.json"; _regular_file(manifest, "request.json")
    try: raw = json.loads(manifest.read_text(encoding="utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc: raise ValidationError("request.json must contain UTF-8 JSON") from exc
    if not isinstance(raw, dict) or raw.get("version") != 1: raise ValidationError("request.json requires version: 1")
    project_id, request_id = raw.get("project_id"), raw.get("request_id")
    if not isinstance(project_id, str) or not IDENTIFIER.fullmatch(project_id): raise ValidationError("project_id is invalid")
    if not isinstance(request_id, str) or not IDENTIFIER.fullmatch(request_id): raise ValidationError("request_id is invalid")
    message_path = (root / _safe_relative(raw.get("message_file", "message.txt"), "message_file")).resolve()
    if message_path.parent != root: raise ValidationError("message_file must be directly inside the request directory")
    message = _read_utf8(message_path, "message_file")
    if not message.strip(): raise ValidationError("message_file must not be empty")
    values = raw.get("attachments", [])
    if not isinstance(values, list) or not all(isinstance(item, str) for item in values): raise ValidationError("attachments must be a list of relative file paths")
    attachments: list[Path] = []
    for item in values:
        path = (root / _safe_relative(item, "attachment")).resolve()
        if root not in path.parents: raise ValidationError("attachment must stay inside the request directory")
        _regular_file(path, "attachment"); attachments.append(path)
    window = raw.get("workflow_window_seconds", DEFAULT_WINDOW_SECONDS)
    if not isinstance(window, int) or not 1 <= window <= 3600: raise ValidationError("workflow_window_seconds must be an integer from 1 to 3600")
    difficulty, detail = raw.get("task_difficulty", "normal"), raw.get("instruction_level", "normal")
    if difficulty not in {"normal", "hard", "challenge"}: raise ValidationError("task_difficulty must be normal, hard, or challenge")
    if detail not in {"normal", "detailed", "manual_book"}: raise ValidationError("instruction_level must be normal, detailed, or manual_book")
    explicit_url = raw.get("chat_url")
    if explicit_url is not None and not valid_chat_url(explicit_url): raise ValidationError("chat_url must be an HTTPS ChatGPT conversation URL")
    chat_url = explicit_url or _load_registry().get(project_id)
    if not chat_url or not valid_chat_url(chat_url): raise ValidationError(f"no valid chat_url in request or registry for project {project_id}")
    digest = hashlib.sha256(); metadata = {"project_id": project_id, "request_id": request_id, "message_file": message_path.name, "attachments": values, "workflow_window_seconds": window, "task_difficulty": difficulty, "instruction_level": detail, "chat_url": chat_url}
    digest.update(json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8")); digest.update(message_path.read_bytes())
    for path in attachments: digest.update(path.name.encode("utf-8")); digest.update(path.read_bytes())
    return Request(root, project_id, request_id, message_path, message, tuple(attachments), window, difficulty, detail, chat_url, digest.hexdigest())
