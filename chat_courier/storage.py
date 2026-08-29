from __future__ import annotations
import hashlib, json, os, time
from pathlib import Path
from typing import Any
from .model import Request, ValidationError, atomic_json

def receipt_path(request: Request) -> Path: return request.directory / "receipt.json"
def load_receipt(request: Request) -> dict[str, Any] | None:
    path = receipt_path(request)
    if not path.exists(): return None
    try: value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc: raise ValidationError(f"invalid receipt.json: {path}") from exc
    if not isinstance(value, dict) or value.get("request_id") != request.request_id: raise ValidationError("receipt.json does not belong to this request")
    if value.get("fingerprint") != request.fingerprint: raise ValidationError("request directory was reused with different content")
    return value
def event(request: Request, name: str, **values: Any) -> None:
    payload = {"event": name, "project_id": request.project_id, "request_id": request.request_id, **values}
    with (request.directory / "events.jsonl").open("a", encoding="utf-8", newline="\n") as handle: handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
def request_was_submitted(request: Request) -> bool:
    return submission_count(request) > 0

def submission_count(request: Request) -> int:
    path = request.directory / "events.jsonl"
    if not path.exists(): return 0
    count = 0
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try: value = json.loads(line)
                except json.JSONDecodeError: continue
                if (value.get("event") == "request_submitted"
                        and value.get("project_id") == request.project_id
                        and value.get("request_id") == request.request_id):
                    count += 1
    except OSError as exc:
        raise ValidationError(f"cannot read request events: {path}") from exc
    return count
def receipt(request: Request, state: str, detail: str, **values: Any) -> None:
    # Queue provenance survives later state transitions such as
    # request_submitted and response_received, so an Agent can audit both the
    # waiting and browser portions from the final receipt.
    preserved: dict[str, Any] = {}
    path = receipt_path(request)
    try:
        old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if old.get("fingerprint") == request.fingerprint:
            preserved = {key: value for key, value in old.items() if key.startswith("queue_") or key in {"ahead_count", "estimated_wait_upper_bound_seconds", "current_owner", "execution_started_at"}}
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    atomic_json(path, {"version": 1, "project_id": request.project_id, "request_id": request.request_id, "fingerprint": request.fingerprint, "state": state, "detail": detail, "workflow_window_seconds": request.workflow_window_seconds, "queue_wait_seconds": request.queue_wait_seconds, **preserved, **values})
def save_response(request: Request, body: str) -> Path:
    path = request.directory / "response.txt"; temporary = path.with_suffix(".txt.tmp"); temporary.write_text(body, encoding="utf-8", newline="\n"); os.replace(temporary, path); return path

def response_cursor_path(request: Request) -> Path: return request.directory / "response-cursor.json"
def save_response_cursor(request: Request, identities: set[str]) -> Path:
    path = response_cursor_path(request)
    atomic_json(path, {
        "version": 1, "project_id": request.project_id, "request_id": request.request_id,
        "fingerprint": request.fingerprint, "assistant_identities": sorted(identities),
        "captured_at": time.time(),
    })
    return path
def load_response_cursor(request: Request) -> set[str] | None:
    path = response_cursor_path(request)
    if not path.exists(): return None
    try: value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc: raise ValidationError(f"invalid response cursor: {path}") from exc
    identities = value.get("assistant_identities") if isinstance(value, dict) else None
    if (value.get("project_id") != request.project_id or value.get("request_id") != request.request_id
            or value.get("fingerprint") != request.fingerprint or not isinstance(identities, list)
            or not all(isinstance(item, str) for item in identities)):
        raise ValidationError("response cursor does not belong to this request")
    return set(identities)

def response_capture_path(request: Request) -> Path: return request.directory / "response-capture.json"
def _save_capture(request: Request, *, identity: str, index: int, text: str,
                  raw_name: str, manifest_name: str) -> dict[str, Any]:
    raw_path = request.directory / raw_name
    temporary = raw_path.with_suffix(".txt.tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, raw_path)
    payload = {
        "version": 1, "project_id": request.project_id, "request_id": request.request_id,
        "fingerprint": request.fingerprint, "assistant_identity": identity,
        "assistant_index": index, "captured_at": time.time(),
        "raw_path": raw_path.name, "raw_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }
    atomic_json(request.directory / manifest_name, payload)
    return payload
def save_response_capture(request: Request, *, identity: str, index: int, text: str) -> dict[str, Any]:
    return _save_capture(request, identity=identity, index=index, text=text,
                         raw_name="response.raw.txt", manifest_name="response-capture.json")
def save_latest_response_capture(request: Request, *, identity: str, index: int, text: str,
                                 user_turn_found: bool = True) -> dict[str, Any]:
    value = _save_capture(request, identity=identity, index=index, text=text,
                          raw_name="latest-response.raw.txt", manifest_name="latest-response-capture.json")
    value.update({"latest_user_turn_found": user_turn_found,
                  "post_submission_reply_found": True})
    atomic_json(request.directory / "latest-response-capture.json", value)
    return value

def archive_response_capture(request: Request, attempt: int) -> None:
    """Preserve a rejected capture before a bounded resend overwrites it."""
    for name in ("response.txt", "response.raw.txt", "response-capture.json"):
        source = request.directory / name
        if source.exists():
            target = request.directory / f"attempt-{attempt}-{name}"
            if not target.exists():
                os.replace(source, target)
def load_response_capture(request: Request) -> tuple[dict[str, Any], str] | None:
    path = response_capture_path(request)
    if not path.exists(): return None
    try: value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc: raise ValidationError(f"invalid response capture: {path}") from exc
    if (not isinstance(value, dict) or value.get("project_id") != request.project_id
            or value.get("request_id") != request.request_id or value.get("fingerprint") != request.fingerprint):
        raise ValidationError("response capture does not belong to this request")
    raw_name = value.get("raw_path")
    if not isinstance(raw_name, str) or Path(raw_name).name != raw_name:
        raise ValidationError("response capture raw path is invalid")
    raw_path = request.directory / raw_name
    try: text = raw_path.read_text(encoding="utf-8")
    except OSError as exc: raise ValidationError("captured response raw text is unavailable") from exc
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != value.get("raw_sha256"):
        raise ValidationError("captured response raw text hash does not match")
    return value, text
