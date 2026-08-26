from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import time
from typing import Any

from .locking import RuntimeLock
from .model import IDENTIFIER, ValidationError, _load_registry, atomic_json, load_request, runtime_root
from .storage import load_receipt

PROJECTS_SCHEMA = "chat-courier-projects-v1"
PREPARED_SCHEMA = "chat-courier-prepared-v1"
ATTACHMENT_SCHEMA = "chat-courier-attachments-v1"
RECOVERY_ONLY_STATES = {
    "request_submitted", "waiting_for_response", "submission_unconfirmed",
    "response_timeout", "response_protocol_error", "queue_recovery_required",
}
TERMINAL_STATES = {
    "response_received", "chat_auth_required", "chat_access_denied",
    "chat_target_mismatch", "configuration_error", "browser_error", "courier_error",
}


def _json(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"invalid {description}: {path}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"{description} must be an object")
    return value


def projects_path() -> Path:
    return runtime_root() / "projects.json"


def prepared_path() -> Path:
    return runtime_root() / "prepared_requests.json"


def load_projects() -> dict[str, dict[str, Any]]:
    path = projects_path()
    if not path.exists():
        return {}
    value = _json(path, "project registry")
    if value.get("schema") != PROJECTS_SCHEMA or not isinstance(value.get("projects"), dict):
        raise ValidationError("invalid project registry schema")
    return value["projects"]


def project_policy(project_id: str) -> dict[str, Any]:
    if not IDENTIFIER.fullmatch(project_id):
        raise ValidationError("project_id is invalid")
    policy = load_projects().get(project_id)
    if not isinstance(policy, dict):
        raise ValidationError(f"project {project_id} has no configured outbox and attachment policy")
    required = {"outbox_root", "artifact_roots", "max_attachments", "max_single_bytes", "max_total_bytes"}
    if set(policy) != required:
        raise ValidationError("project policy schema mismatch")
    if project_id not in _load_registry():
        raise ValidationError(f"project {project_id} has no registered Chat conversation")
    return policy


def configure_project(project_id: str, outbox_root: str, artifact_roots: list[str],
                      max_attachments: int, max_single_bytes: int, max_total_bytes: int) -> dict[str, Any]:
    if not IDENTIFIER.fullmatch(project_id) or project_id not in _load_registry():
        raise ValidationError("register and confirm the project Chat conversation first")
    outbox = Path(outbox_root)
    roots = [Path(value) for value in artifact_roots]
    limits = (max_attachments, max_single_bytes, max_total_bytes)
    if not outbox.is_absolute() or not roots or not all(path.is_absolute() for path in roots):
        raise ValidationError("absolute outbox_root and artifact_roots are required")
    if not all(isinstance(value, int) and value >= 0 for value in limits):
        raise ValidationError("attachment limits must be non-negative")
    record = {
        "outbox_root": str(outbox.resolve(strict=False)),
        "artifact_roots": [str(path.resolve(strict=False)) for path in roots],
        "max_attachments": max_attachments,
        "max_single_bytes": max_single_bytes,
        "max_total_bytes": max_total_bytes,
    }
    with RuntimeLock("ChatCourier-RegistryState", runtime_root()):
        projects = load_projects()
        projects[project_id] = record
        atomic_json(projects_path(), {"schema": PROJECTS_SCHEMA, "projects": projects})
    return {"project_id": project_id, **record}


def _attachment(path: str, roots: list[Path]) -> Path:
    try:
        resolved = Path(path).resolve(strict=True)
    except OSError as exc:
        raise ValidationError(f"attachment is unavailable: {path}") from exc
    if resolved.is_symlink() or not resolved.is_file():
        raise ValidationError("attachment must be a regular non-symlink file")
    for root in roots:
        try:
            resolved.relative_to(root.resolve(strict=True))
            return resolved
        except (ValueError, OSError):
            pass
    raise ValidationError(f"attachment is outside configured artifact roots: {resolved}")


def _payload_sha(project_id: str, key: str, message: bytes, attachments: list[Path]) -> str:
    digest = hashlib.sha256(json.dumps(
        {"project_id": project_id, "idempotency_key": key},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8"))
    digest.update(message)
    for path in attachments:
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _prepared() -> dict[str, dict[str, Any]]:
    path = prepared_path()
    if not path.exists():
        return {}
    value = _json(path, "prepared request registry")
    if value.get("schema") != PREPARED_SCHEMA or not isinstance(value.get("requests"), dict):
        raise ValidationError("invalid prepared request registry schema")
    return value["requests"]


def prepare_request(project_id: str, idempotency_key: str, message_utf8: str,
                    attachments: list[str] | None = None, *,
                    workflow_window_seconds: int = 600, queue_wait_seconds: int = 3600,
                    task_difficulty: str = "normal", instruction_level: str = "normal") -> dict[str, Any]:
    if not IDENTIFIER.fullmatch(idempotency_key):
        raise ValidationError("idempotency_key is invalid")
    if not isinstance(message_utf8, str) or not message_utf8.strip():
        raise ValidationError("message_utf8 must not be empty")
    policy = project_policy(project_id)
    sources = [_attachment(path, [Path(root) for root in policy["artifact_roots"]])
               for path in (attachments or [])]
    sizes = [path.stat().st_size for path in sources]
    if len(sources) > policy["max_attachments"]:
        raise ValidationError("attachment count exceeds project policy")
    if any(size > policy["max_single_bytes"] for size in sizes) or sum(sizes) > policy["max_total_bytes"]:
        raise ValidationError("attachment size exceeds project policy")
    if len({path.name.casefold() for path in sources}) != len(sources):
        raise ValidationError("attachment basenames collide")
    message = message_utf8.encode("utf-8")
    payload_sha256 = _payload_sha(project_id, idempotency_key, message, sources)
    index_key = f"{project_id}:{idempotency_key}"
    with RuntimeLock("ChatCourier-PrepareState", runtime_root()):
        prepared = _prepared()
        previous = prepared.get(index_key)
        if previous:
            if previous.get("payload_sha256") != payload_sha256:
                raise ValidationError("idempotency key was already used with different content")
            request = load_request(previous["request_directory"])
            return {"state": "existing", **previous, "fingerprint": request.fingerprint}
        outbox = Path(policy["outbox_root"]).resolve(strict=False)
        outbox.mkdir(parents=True, exist_ok=True)
        request_id = f"{project_id}-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-{payload_sha256[:8]}"
        directory = outbox / request_id
        directory.mkdir(mode=0o700)
        (directory / "message.txt").write_bytes(message)
        copied, evidence = [], []
        if sources:
            target_root = directory / "attachments"
            target_root.mkdir(mode=0o700)
            for source, size in zip(sources, sizes):
                target = target_root / source.name
                shutil.copyfile(source, target)
                relative = f"attachments/{source.name}"
                copied.append(relative)
                evidence.append({"path": relative, "size_bytes": size,
                                 "sha256": hashlib.sha256(target.read_bytes()).hexdigest()})
        atomic_json(directory / "request.json", {
            "version": 1, "project_id": project_id, "request_id": request_id,
            "message_file": "message.txt", "attachments": copied,
            "workflow_window_seconds": workflow_window_seconds,
            "queue_wait_seconds": queue_wait_seconds,
            "task_difficulty": task_difficulty, "instruction_level": instruction_level,
        })
        atomic_json(directory / "attachment-attestation.json", {
            "schema": ATTACHMENT_SCHEMA, "project_id": project_id, "request_id": request_id,
            "attachments": evidence, "count": len(evidence), "total_bytes": sum(sizes),
        })
        request = load_request(directory)
        record = {"project_id": project_id, "idempotency_key": idempotency_key,
                  "payload_sha256": payload_sha256, "request_id": request_id,
                  "request_directory": str(directory), "fingerprint": request.fingerprint}
        prepared[index_key] = record
        atomic_json(prepared_path(), {"schema": PREPARED_SCHEMA, "requests": prepared})
        return {"state": "prepared", **record}


def request_status(request_directory: str | Path) -> dict[str, Any]:
    request = load_request(request_directory)
    receipt = load_receipt(request)
    state = receipt.get("state") if receipt else "prepared"
    return {
        "project_id": request.project_id, "request_id": request.request_id,
        "request_directory": str(request.directory), "state": state,
        "terminal": state in TERMINAL_STATES,
        "recovery_only_required": state in RECOVERY_ONLY_STATES,
        "response_path": str(request.directory / "response.txt") if state == "response_received" else None,
        "fingerprint": request.fingerprint,
    }


def wait_status(request_directory: str | Path, timeout: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        value = request_status(request_directory)
        if value["terminal"] or value["recovery_only_required"]:
            return value
        if time.monotonic() >= deadline:
            return {**value, "wait_timeout": True, "safe_next_action": "courier_status",
                    "retry_allowed": False}
        time.sleep(1)


def capabilities() -> dict[str, Any]:
    projects = load_projects()
    return {
        "schema": "chat-courier-capabilities-v1",
        "operations": ["courier_capabilities", "courier_prepare", "courier_dispatch",
                       "courier_status", "courier_wait", "courier_recover"],
        "arbitrary_url": False, "arbitrary_profile": False,
        "projects": sorted(projects), "project_policies": projects,
    }
