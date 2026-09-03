from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import time
from typing import Any


MAX_INLINE_MESSAGE_BYTES = 32 * 1024
from urllib.parse import urlsplit

from .locking import RuntimeLock

DEFAULT_WINDOW_SECONDS = 600
DEFAULT_QUEUE_WAIT_SECONDS = 3600
ACTIVE_SETUP_BUDGET_SECONDS = 600
CALLER_GRACE_SECONDS = 60
REGISTRATION_CONFIRMATION_SECONDS = 900
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
REGISTRATION_BASES = {"user_direct", "prior_authorization"}

class ValidationError(ValueError):
    pass


def minimum_caller_window_seconds(queue_wait_seconds: int, workflow_window_seconds: int) -> int:
    """Conservative process lifetime for a bounded queued Chat round trip."""
    return queue_wait_seconds + ACTIVE_SETUP_BUDGET_SECONDS + workflow_window_seconds + CALLER_GRACE_SECONDS

def runtime_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "ChatCourier"

def _safe_relative(value: object, name: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{name} must be a non-empty relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValidationError(f"{name} must stay inside the request directory")
    return path

def conversation_id_from_url(value: object) -> str | None:
    if not isinstance(value, str): return None
    try: parsed = urlsplit(value)
    except ValueError: return None
    if parsed.scheme != "https" or parsed.hostname not in {"chatgpt.com", "www.chatgpt.com"} or parsed.username or parsed.password or parsed.port: return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) == 2 and parts[0] == "c": return parts[1]
    if len(parts) >= 3 and parts[-2] == "c": return parts[-1]
    return None

def valid_chat_url(value: object) -> bool:
    return conversation_id_from_url(value) is not None

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
    attachments: tuple[Path, ...]; workflow_window_seconds: int; queue_wait_seconds: int; task_difficulty: str
    instruction_level: str; report_policy: str; chat_url: str; fingerprint: str
    retry_message: str | None = None
    idle_supervision_required: bool = False
    supervisor_task_id: str | None = None

def registry_path() -> Path: return runtime_root() / "chat_urls.json"
def pending_registry_path() -> Path: return runtime_root() / "pending_chat_url_registrations.json"

def _load_registry() -> dict[str, str]:
    path = registry_path()
    if not path.exists(): return {}
    try: raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc: raise ValidationError(f"invalid Chat URL registry: {path}") from exc
    if not isinstance(raw, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in raw.items()): raise ValidationError("invalid Chat URL registry schema")
    return raw

def _load_pending_registrations() -> dict[str, dict[str, Any]]:
    path = pending_registry_path()
    if not path.exists(): return {}
    try: raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc: raise ValidationError(f"invalid pending Chat URL registrations: {path}") from exc
    if not isinstance(raw, dict) or not all(isinstance(key, str) and isinstance(value, dict) for key, value in raw.items()):
        raise ValidationError("invalid pending Chat URL registrations schema")
    return raw

def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)

def propose_url_registration(project_id: str, url: str) -> dict[str, Any]:
    """Create a short-lived registration proposal without changing the active URL."""
    if not IDENTIFIER.fullmatch(project_id): raise ValidationError("project_id is invalid")
    if not valid_chat_url(url): raise ValidationError("url must be an HTTPS ChatGPT conversation URL")
    with RuntimeLock("ChatCourier-RegistryState", runtime_root()):
        registry = _load_registry(); current = registry.get(project_id)
        if current == url:
            return {"state": "already_registered", "project_id": project_id, "url": url, "current_url": current, "changed": False}
        pending = _load_pending_registrations(); existing = pending.get(project_id)
        if existing:
            if existing.get("candidate_url") != url:
                raise ValidationError("a different URL registration is already awaiting confirmation for this project")
            if not isinstance(existing.get("confirmation_id"), str) or not isinstance(existing.get("expires_at"), int):
                raise ValidationError("pending Chat URL registration is invalid")
            if existing["expires_at"] > int(time.time()):
                return {"state": "confirmation_required", "project_id": project_id, "current_url": current, **existing}
        record = {
            "candidate_url": url,
            "confirmation_id": secrets.token_urlsafe(18),
            "created_at": int(time.time()),
            "expires_at": int(time.time()) + REGISTRATION_CONFIRMATION_SECONDS,
        }
        pending[project_id] = record; atomic_json(pending_registry_path(), pending)
        return {"state": "confirmation_required", "project_id": project_id, "current_url": current, **record}

def confirm_url_registration(project_id: str, confirmation_id: str, basis: str) -> dict[str, Any]:
    """Commit only a separately confirmed URL proposal and record its basis."""
    if not IDENTIFIER.fullmatch(project_id): raise ValidationError("project_id is invalid")
    if not isinstance(confirmation_id, str) or not confirmation_id: raise ValidationError("confirmation_id is required")
    if basis not in REGISTRATION_BASES: raise ValidationError("basis must be user_direct or prior_authorization")
    with RuntimeLock("ChatCourier-RegistryState", runtime_root()):
        pending = _load_pending_registrations(); record = pending.get(project_id)
        if not record: raise ValidationError("no pending Chat URL registration for this project")
        if record.get("confirmation_id") != confirmation_id: raise ValidationError("confirmation_id does not match the pending registration")
        if not isinstance(record.get("expires_at"), int) or record["expires_at"] <= int(time.time()):
            pending.pop(project_id, None); atomic_json(pending_registry_path(), pending)
            raise ValidationError("pending Chat URL registration has expired; propose it again")
        url = record.get("candidate_url")
        if not valid_chat_url(url): raise ValidationError("pending Chat URL is invalid")
        registry = _load_registry(); previous = registry.get(project_id)
        registry[project_id] = url; atomic_json(registry_path(), registry)
        pending.pop(project_id, None); atomic_json(pending_registry_path(), pending)
        return {"project_id": project_id, "url": url, "previous_url": previous, "changed": previous != url, "basis": basis}

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
    if len(message.encode("utf-8")) > MAX_INLINE_MESSAGE_BYTES:
        raise ValidationError(
            f"message_file exceeds the {MAX_INLINE_MESSAGE_BYTES}-byte inline limit; "
            "publish the evidence and send a compact immutable reference"
        )
    retry_message = None
    retry_name = raw.get("retry_message_file")
    retry_path = None
    if retry_name is not None:
        retry_path = (root / _safe_relative(retry_name, "retry_message_file")).resolve()
        if retry_path.parent != root: raise ValidationError("retry_message_file must be directly inside the request directory")
        retry_message = _read_utf8(retry_path, "retry_message_file")
        if not retry_message.strip(): raise ValidationError("retry_message_file must not be empty")
    values = raw.get("attachments", [])
    if not isinstance(values, list) or not all(isinstance(item, str) for item in values): raise ValidationError("attachments must be a list of relative file paths")
    attachments: list[Path] = []
    for item in values:
        path = (root / _safe_relative(item, "attachment")).resolve()
        if root not in path.parents: raise ValidationError("attachment must stay inside the request directory")
        _regular_file(path, "attachment"); attachments.append(path)
    window = raw.get("workflow_window_seconds", DEFAULT_WINDOW_SECONDS)
    if not isinstance(window, int) or not 1 <= window <= 3600: raise ValidationError("workflow_window_seconds must be an integer from 1 to 3600")
    queue_wait = raw.get("queue_wait_seconds", DEFAULT_QUEUE_WAIT_SECONDS)
    if not isinstance(queue_wait, int) or not 1 <= queue_wait <= 7200: raise ValidationError("queue_wait_seconds must be an integer from 1 to 7200")
    difficulty, detail = raw.get("task_difficulty", "normal"), raw.get("instruction_level", "normal")
    if difficulty not in {"normal", "hard", "challenge"}: raise ValidationError("task_difficulty must be normal, hard, or challenge")
    if detail not in {"normal", "detailed", "manual_book"}: raise ValidationError("instruction_level must be normal, detailed, or manual_book")
    report_policy = raw.get("report_policy", "per-work-order")
    if report_policy not in {"adaptive", "per-work-order", "milestone", "final-only"}: raise ValidationError("report_policy must be adaptive, per-work-order, milestone, or final-only")
    idle_supervision_required = raw.get("idle_supervision_required", False)
    if type(idle_supervision_required) is not bool: raise ValidationError("idle_supervision_required must be boolean")
    supervisor_task_id = raw.get("supervisor_task_id")
    if idle_supervision_required and (not isinstance(supervisor_task_id, str) or not IDENTIFIER.fullmatch(supervisor_task_id)):
        raise ValidationError("supervisor_task_id is required when idle supervision is enabled")
    if not idle_supervision_required and supervisor_task_id is not None:
        raise ValidationError("supervisor_task_id requires idle_supervision_required=true")
    explicit_url = raw.get("chat_url")
    if explicit_url is not None and not valid_chat_url(explicit_url): raise ValidationError("chat_url must be an HTTPS ChatGPT conversation URL")
    registered_url = _load_registry().get(project_id)
    if not registered_url: raise ValidationError(f"no registered chat_url for project {project_id}; use the two-step register and confirm-register flow")
    if explicit_url is not None and explicit_url != registered_url:
        raise ValidationError("request chat_url does not match the registered URL; propose and confirm a registration change instead")
    chat_url = registered_url
    digest = hashlib.sha256(); metadata = {"project_id": project_id, "request_id": request_id, "message_file": message_path.name, "attachments": values, "workflow_window_seconds": window, "task_difficulty": difficulty, "instruction_level": detail, "chat_url": chat_url}
    # Preserve fingerprints of legacy requests that predate this optional preference.
    if "report_policy" in raw: metadata["report_policy"] = report_policy
    if retry_path: metadata["retry_message_file"] = retry_path.name
    if "idle_supervision_required" in raw: metadata["idle_supervision_required"] = idle_supervision_required
    if supervisor_task_id is not None: metadata["supervisor_task_id"] = supervisor_task_id
    metadata["queue_wait_seconds"] = queue_wait
    digest.update(json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8")); digest.update(message_path.read_bytes())
    if retry_path: digest.update(retry_path.read_bytes())
    for path in attachments: digest.update(path.name.encode("utf-8")); digest.update(path.read_bytes())
    return Request(root, project_id, request_id, message_path, message, tuple(attachments), window, queue_wait, difficulty, detail, report_policy, chat_url, digest.hexdigest(), retry_message, idle_supervision_required, supervisor_task_id)
