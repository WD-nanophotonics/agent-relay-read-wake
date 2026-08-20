from __future__ import annotations
import json, os
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
def receipt(request: Request, state: str, detail: str, **values: Any) -> None:
    atomic_json(receipt_path(request), {"version": 1, "project_id": request.project_id, "request_id": request.request_id, "fingerprint": request.fingerprint, "state": state, "detail": detail, "workflow_window_seconds": request.workflow_window_seconds, **values})
def save_response(request: Request, body: str) -> Path:
    path = request.directory / "response.txt"; temporary = path.with_suffix(".txt.tmp"); temporary.write_text(body, encoding="utf-8", newline="\n"); os.replace(temporary, path); return path
