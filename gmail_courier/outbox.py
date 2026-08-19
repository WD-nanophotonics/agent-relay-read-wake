from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from datetime import datetime, timezone
from contextlib import contextmanager

from .chat_registry import current_chat_url, legacy_default_chat_url
from .config import load_config
from .protocol import validate_chat_payload, valid_correlation_id
from .url import is_chat_url


REQUEST_VERSION = 1
DEFAULT_WORKFLOW_WINDOW_SECONDS = 360
DEFAULT_POLL_MAX_SECONDS = 360
DEFAULT_POLL_INTERVAL_SECONDS = 10
DEFAULT_LOOKBACK_SECONDS = 1200
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
RESERVED_NAMES = {"request.json", "receipt.json", "READY"}


class RequestValidationError(ValueError):
    def __init__(self, message: str, code: str = "invalid_request"):
        super().__init__(message)
        self.code = code


class RequestReuseError(RequestValidationError):
    def __init__(self, message: str = "request directory has already been used"):
        super().__init__(message, "request_reuse")


def ascii_text(value: str) -> bool:
    return value.isascii() and all(char in "\r\n\t" or 32 <= ord(char) <= 126 for char in value)


def valid_identifier(value: object) -> bool:
    return isinstance(value, str) and bool(IDENTIFIER_RE.fullmatch(value))


def _relative_message_path(value: object) -> Path:
    if value is None:
        return Path("message.txt")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("message_file must be a non-empty relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or path.name in RESERVED_NAMES or path.name != value:
        raise ValueError("message_file must be a simple relative filename")
    return path


@dataclass(frozen=True)
class OutboxRequest:
    directory: Path
    request_id: str
    project_id: str
    correlation_id: str
    task_id: str
    keyword: str
    chat_url: str
    message_path: Path
    message: str
    workflow_window_seconds: int


def request_directory(path: str | Path) -> Path:
    candidate = Path(path).expanduser().resolve()
    directory = candidate if candidate.is_dir() else candidate.parent
    if not directory.is_dir():
        raise ValueError(f"outbox request directory does not exist: {directory}")
    return directory


def _safe_file(path: Path, description: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise RequestValidationError(f"{description} must be a regular file: {path}", "path_integrity")


def _resolved_chat_url(home: Path | None, project_id: str, value: object) -> str:
    if value is not None and value != "":
        if not isinstance(value, str) or not is_chat_url(value):
            raise RequestValidationError("chat_url must be an HTTPS ChatGPT conversation URL", "invalid_chat_url")
        return value
    if home is not None:
        registered = current_chat_url(home, project_id)
        if registered:
            return registered
        try:
            config = load_config(home)
        except (FileNotFoundError, ValueError):
            config = None
        if config is not None:
            project = config.project_by_code(project_id)
            if project and project.chat_url:
                return project.chat_url
        legacy = legacy_default_chat_url(project_id)
        if legacy:
            return legacy
    raise RequestValidationError("chat_url is absent and no registered/default project URL exists", "missing_chat_url")


def _read_manifest(directory: Path) -> dict:
    manifest_path = directory / "request.json"
    _safe_file(manifest_path, "request.json")
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RequestValidationError(f"request.json is not valid UTF-8 JSON: {manifest_path}", "invalid_manifest") from exc
    if not isinstance(raw, dict):
        raise RequestValidationError("request.json must contain a JSON object", "invalid_manifest")
    return raw


def validate_request(path: str | Path, *, home: Path | None = None, require_ready: bool = False, reject_reuse: bool = False) -> OutboxRequest:
    """Validate local request files without network, Chrome, Gmail, or READY creation."""
    directory = request_directory(path)
    raw = _read_manifest(directory)
    ready = directory / "READY"
    if require_ready:
        if not ready.exists():
            raise RequestValidationError(f"outbox request is not ready; missing {ready}", "missing_ready")
        _safe_file(ready, "READY")
    if raw.get("version") != REQUEST_VERSION:
        raise RequestValidationError(f"outbox request version must be {REQUEST_VERSION}", "invalid_version")
    if raw.get("operation", "chat-send") != "chat-send":
        raise RequestValidationError("outbox request operation must be chat-send", "invalid_operation")
    project_id = raw.get("project_id")
    correlation_id = raw.get("correlation_id")
    task_id = raw.get("task_id")
    keyword = raw.get("keyword")
    request_id = raw.get("request_id")
    if not valid_identifier(project_id):
        raise RequestValidationError("outbox project_id must be a non-empty ASCII identifier", "invalid_project_id")
    if not valid_correlation_id(project_id, correlation_id):
        raise RequestValidationError("outbox correlation_id must start with project_id and contain a digit", "invalid_correlation_id")
    if not valid_identifier(task_id):
        raise RequestValidationError("outbox task_id must be a non-empty ASCII identifier", "invalid_task_id")
    if not valid_identifier(keyword):
        raise RequestValidationError("outbox keyword must be a non-empty ASCII identifier", "invalid_keyword")
    if not valid_identifier(request_id):
        raise RequestValidationError("outbox request_id must be a non-empty ASCII identifier", "invalid_request_id")
    chat_url = _resolved_chat_url(home, project_id, raw.get("chat_url"))
    message_name = _relative_message_path(raw.get("message_file"))
    message_path = (directory / message_name).resolve()
    if message_path.parent != directory:
        raise RequestValidationError("outbox message_file must stay inside the request directory", "path_integrity")
    _safe_file(message_path, "message_file")
    try:
        message = message_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise RequestValidationError(f"outbox message is not valid UTF-8: {message_path}", "invalid_message_encoding") from exc
    if message.startswith("\ufeff") or not message.strip():
        raise RequestValidationError("outbox message must be non-empty UTF-8 text without a BOM", "invalid_message")
    try:
        validate_chat_payload(message)
    except (TypeError, ValueError) as exc:
        raise RequestValidationError(f"outbox message is not valid Chat payload: {exc}", "non_ascii_message") from exc
    window = raw.get("workflow_window_seconds", raw.get("close_delay_seconds", DEFAULT_WORKFLOW_WINDOW_SECONDS))
    if not isinstance(window, int) or window <= 0 or window > 3600:
        raise RequestValidationError("workflow_window_seconds must be an integer from 1 to 3600", "invalid_workflow_window")
    if reject_reuse and (directory / "receipt.json").exists():
        raise RequestReuseError(f"request already has receipt.json: {directory / 'receipt.json'}")
    return OutboxRequest(directory, request_id, project_id, correlation_id, task_id, keyword, chat_url, message_path, message, window)


def load_request(path: str | Path, *, home: Path | None = None, require_ready: bool = True, reject_reuse: bool = False) -> OutboxRequest:
    return validate_request(path, home=home, require_ready=require_ready, reject_reuse=reject_reuse)


def create_ready(path: str | Path, *, home: Path | None = None) -> OutboxRequest:
    request = validate_request(path, home=home, require_ready=False, reject_reuse=True)
    ready = request.directory / "READY"
    if ready.exists():
        raise RequestReuseError(f"READY already exists: {ready}")
    try:
        with ready.open("x", encoding="ascii") as handle:
            handle.write("ready\n")
    except FileExistsError as exc:
        raise RequestReuseError(f"READY was created concurrently: {ready}") from exc
    return request


@contextmanager
def submit_lock(request: OutboxRequest):
    """Prevent two submit processes from using one request directory."""
    lock = request.directory / ".submit.lock"
    try:
        with lock.open("x", encoding="ascii") as handle:
            handle.write(str(os.getpid()))
    except FileExistsError as exc:
        raise RequestReuseError(f"a submit process is already running or left a lock: {lock}") from exc
    try:
        yield lock
    finally:
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


def write_receipt(directory: str | Path, *, request_id: str, state: str, detail: str, **values) -> Path:
    target_dir = request_directory(directory)
    receipt = {}
    target = target_dir / "receipt.json"
    if target.is_file():
        try:
            prior = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(prior, dict):
                receipt.update(prior)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
    receipt.update({
        "version": REQUEST_VERSION,
        "request_id": request_id,
        "state": state,
        "detail": detail,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        **values,
    })
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(receipt, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, target)
    return target
