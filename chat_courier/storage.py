from __future__ import annotations
import hashlib, json, os, time
from pathlib import Path
from typing import Any
from .model import Request, ValidationError, atomic_json

def _payload_id(request: Request) -> str:
    return getattr(request, "payload_fingerprint", getattr(request, "fingerprint", "legacy-test"))

def _belongs(value: dict[str, Any], request: Request) -> bool:
    """Accept v2 payload identity, while retaining current-target v1 evidence."""
    return (value.get("project_id") == request.project_id
            and value.get("request_id") == request.request_id
            and (value.get("payload_fingerprint") == _payload_id(request)
                 or ("payload_fingerprint" not in value
                     and value.get("fingerprint") == request.fingerprint)))

def target_binding_path(request: Request) -> Path:
    return request.directory / "target-binding.json"

def ensure_target_binding(request: Request, *, basis: str = "registered_target") -> dict[str, Any]:
    """Atomically establish v2 payload/transport identity and migrate v1 evidence."""
    path = target_binding_path(request)
    previous: dict[str, Any] = {}
    if path.exists():
        try: previous = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError(f"invalid target binding: {path}") from exc
        if (not isinstance(previous, dict)
                or previous.get("project_id") != request.project_id
                or previous.get("request_id") != request.request_id
                or previous.get("payload_fingerprint") != request.payload_fingerprint):
            raise ValidationError("target binding does not belong to this immutable payload")
    now = time.time()
    url_changed = bool(previous and previous.get("chat_url") != request.chat_url)
    value = {
        "version": 2, "project_id": request.project_id, "request_id": request.request_id,
        "payload_fingerprint": request.payload_fingerprint, "chat_url": request.chat_url,
        "chat_project_id": request.chat_url.split("/g/", 1)[1].split("/", 1)[0] if "/g/" in request.chat_url else None,
        "generation": int(previous.get("generation", 1)) + (1 if url_changed else 0),
        "authorization_basis": basis if url_changed or not previous else previous.get("authorization_basis", basis),
        "previous_url": previous.get("chat_url") if url_changed else previous.get("previous_url"),
        "created_at": previous.get("created_at", now), "updated_at": now,
        "legacy_fingerprints": sorted(set(previous.get("legacy_fingerprints", [])) | {request.fingerprint}),
    }
    atomic_json(path, value)
    # First-touch v1 migration. A v1 file is upgraded only when its complete
    # legacy digest still matches the current payload+target bytes.
    for name in ("receipt.json", "response-cursor.json", "response-capture.json",
                 "latest-response-capture.json", "latest-probe.json"):
        artifact = request.directory / name
        if not artifact.exists(): continue
        try: raw = json.loads(artifact.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError): continue
        if (isinstance(raw, dict) and "payload_fingerprint" not in raw
                and raw.get("project_id") == request.project_id
                and raw.get("request_id") == request.request_id
                and raw.get("fingerprint") == request.fingerprint):
            raw["payload_fingerprint"] = request.payload_fingerprint
            atomic_json(artifact, raw)
    return value

def receipt_path(request: Request) -> Path: return request.directory / "receipt.json"
def load_receipt(request: Request) -> dict[str, Any] | None:
    path = receipt_path(request)
    if not path.exists(): return None
    try: value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc: raise ValidationError(f"invalid receipt.json: {path}") from exc
    if not isinstance(value, dict) or not _belongs(value, request): raise ValidationError("receipt.json does not belong to this immutable request")
    return value
def event(request: Request, name: str, **values: Any) -> None:
    payload = {"event": name, "project_id": request.project_id, "request_id": request.request_id, **values}
    with (request.directory / "events.jsonl").open("a", encoding="utf-8", newline="\n") as handle: handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
def request_was_submitted(request: Request) -> bool:
    return submission_count(request) > 0

def request_events(request: Request) -> list[dict[str, Any]]:
    path = request.directory / "events.jsonl"
    if not path.exists(): return []
    result: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try: value = json.loads(line)
                except json.JSONDecodeError: continue
                if (isinstance(value, dict) and value.get("project_id") == request.project_id
                        and value.get("request_id") == request.request_id):
                    result.append(value)
    except OSError as exc:
        raise ValidationError(f"cannot read request events: {path}") from exc
    return result

def evidence_retry_count(request: Request) -> int:
    return sum(value.get("event") == "evidence_retry_authorized"
               for value in request_events(request))

def submission_count(request: Request, *, total: bool = False) -> int:
    path = request.directory / "events.jsonl"
    if not path.exists(): return 0
    count = 0
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try: value = json.loads(line)
                except json.JSONDecodeError: continue
                if (not total and value.get("event") == "target_rollover_authorized"
                        and value.get("project_id") == request.project_id
                        and value.get("request_id") == request.request_id):
                    count = 0
                if (value.get("event") == "request_submitted"
                        and value.get("project_id") == request.project_id
                        and value.get("request_id") == request.request_id):
                    count += 1
    except OSError as exc:
        raise ValidationError(f"cannot read request events: {path}") from exc
    return count

def archive_target_generation(request: Request) -> Path:
    """Preserve the exhausted target's state before a user-authorized rollover."""
    generation = 1
    while (request.directory / f"target-generation-{generation}").exists():
        generation += 1
    target = request.directory / f"target-generation-{generation}"
    target.mkdir()
    for name in (
        "receipt.json", "response.txt", "response.raw.txt", "response-capture.json",
        "latest-response.raw.txt", "latest-response-capture.json", "response-cursor.json",
    ):
        source = request.directory / name
        if source.exists():
            os.replace(source, target / name)
    return target
def receipt(request: Request, state: str, detail: str, **values: Any) -> None:
    # Queue provenance survives later state transitions such as
    # request_submitted and response_received, so an Agent can audit both the
    # waiting and browser portions from the final receipt.
    preserved: dict[str, Any] = {}
    path = receipt_path(request)
    try:
        old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if isinstance(old, dict) and _belongs(old, request):
            preserved = {key: value for key, value in old.items() if key.startswith("queue_") or key in {"ahead_count", "estimated_wait_upper_bound_seconds", "current_owner", "execution_started_at"}}
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    atomic_json(path, {"version": 2, "project_id": request.project_id, "request_id": request.request_id, "fingerprint": request.fingerprint, "payload_fingerprint": request.payload_fingerprint, "target_url": request.chat_url, "state": state, "detail": detail, "workflow_window_seconds": request.workflow_window_seconds, "queue_wait_seconds": request.queue_wait_seconds, **preserved, **values})
def save_response(request: Request, body: str) -> Path:
    path = request.directory / "response.txt"; temporary = path.with_suffix(".txt.tmp"); temporary.write_text(body, encoding="utf-8", newline="\n"); os.replace(temporary, path); return path

def response_cursor_path(request: Request) -> Path: return request.directory / "response-cursor.json"
def save_response_cursor(request: Request, identities: set[str]) -> Path:
    path = response_cursor_path(request)
    atomic_json(path, {
        "version": 1, "project_id": request.project_id, "request_id": request.request_id,
        "fingerprint": request.fingerprint, "payload_fingerprint": request.payload_fingerprint, "assistant_identities": sorted(identities),
        "captured_at": time.time(),
    })
    return path
def load_response_cursor(request: Request) -> set[str] | None:
    path = response_cursor_path(request)
    if not path.exists(): return None
    try: value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc: raise ValidationError(f"invalid response cursor: {path}") from exc
    identities = value.get("assistant_identities") if isinstance(value, dict) else None
    if (not isinstance(value, dict) or not _belongs(value, request) or not isinstance(identities, list)
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
        "fingerprint": request.fingerprint, "payload_fingerprint": request.payload_fingerprint, "assistant_identity": identity,
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

def latest_probe_path(request: Request) -> Path: return request.directory / "latest-probe.json"
def save_latest_probe(request: Request, *, user_turn_found: bool,
                      reply_found: bool, live_owner_found: bool) -> dict[str, Any]:
    value = {
        "version": 1, "project_id": request.project_id, "request_id": request.request_id,
        "fingerprint": request.fingerprint, "payload_fingerprint": request.payload_fingerprint, "captured_at": time.time(),
        "latest_user_turn_found": bool(user_turn_found),
        "post_submission_reply_found": bool(reply_found),
        "live_owner_found": bool(live_owner_found),
        "submission_count": submission_count(request), "message_sent": False,
    }
    atomic_json(latest_probe_path(request), value)
    return value
def load_latest_probe(request: Request) -> dict[str, Any]:
    path = latest_probe_path(request)
    try: value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError("a valid latest-response probe is required") from exc
    if not isinstance(value, dict) or not _belongs(value, request):
        raise ValidationError("latest-response probe does not belong to this request")
    return value

def archive_response_capture(request: Request, attempt: int) -> None:
    """Preserve a rejected capture before a bounded resend overwrites it."""
    for name in ("response.txt", "response.raw.txt", "response-capture.json"):
        source = request.directory / name
        if source.exists():
            target = request.directory / f"attempt-{attempt}-{name}"
            serial = 2
            while target.exists():
                target = request.directory / f"attempt-{attempt}-{serial}-{name}"
                serial += 1
            os.replace(source, target)
def load_response_capture(request: Request) -> tuple[dict[str, Any], str] | None:
    path = response_capture_path(request)
    if not path.exists(): return None
    try: value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc: raise ValidationError(f"invalid response capture: {path}") from exc
    if not isinstance(value, dict) or not _belongs(value, request):
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

def merge_conversation_ledger(request: Request, snapshot: dict[str, Any], *,
                              streaming: bool, composer_ready: bool,
                              session_id: str) -> dict[str, Any]:
    """Merge partial DOM observations; never discard older conversation facts."""
    path = request.directory / "conversation-ledger.json"
    now = time.time(); ledger: dict[str, Any] = {}
    if path.exists():
        try: ledger = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError): ledger = {}
    payload_id = _payload_id(request)
    if (not isinstance(ledger, dict) or ledger.get("payload_fingerprint") not in {None, payload_id}):
        raise ValidationError("conversation ledger does not belong to this immutable payload")
    existing = {str(item.get("identity")): item for item in ledger.get("messages", [])
                if isinstance(item, dict) and item.get("identity")}
    for observed in snapshot.get("messages", []):
        if not isinstance(observed, dict) or not observed.get("identity"): continue
        identity = str(observed["identity"]); old = existing.get(identity, {})
        item = {**old, **observed}
        item["first_observed_at"] = old.get("first_observed_at", now)
        item["last_observed_at"] = now
        item["observation_count"] = int(old.get("observation_count", 0)) + 1
        existing[identity] = item
    messages = sorted(existing.values(), key=lambda item: (
        int(item.get("ordinal", 0)), float(item.get("first_observed_at", now))))
    current_request_id: str | None = None
    for item in messages:
        request_ids = item.get("request_ids", [])
        if item.get("role") == "user":
            current_request_id = request_ids[-1] if isinstance(request_ids, list) and request_ids else None
        elif item.get("role") == "assistant":
            exact = request_ids[-1] if isinstance(request_ids, list) and request_ids else None
            if exact:
                item["in_reply_to_request_id"] = exact
            elif current_request_id and not item.get("in_reply_to_request_id"):
                item["in_reply_to_request_id"] = current_request_id
    ledger = {
        "version": 1, "project_id": request.project_id, "request_id": request.request_id,
        "payload_fingerprint": payload_id, "chat_url": getattr(request, "chat_url", None),
        "first_observed_at": ledger.get("first_observed_at", now), "last_observed_at": now,
        "last_session_id": session_id, "streaming": bool(streaming),
        "composer_ready": bool(composer_ready), "dom_message_count": snapshot.get("message_count"),
        "messages": messages,
    }
    atomic_json(path, ledger)
    return ledger

def ledger_reply(request: Request) -> dict[str, Any] | None:
    path = request.directory / "conversation-ledger.json"
    if not path.exists(): return None
    try: ledger = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError): return None
    if ledger.get("payload_fingerprint") != _payload_id(request): return None
    rejected = {(str(item.get("assistant_identity")), str(item.get("raw_sha256")))
                for item in request_events(request)
                if item.get("event") in {"response_protocol_error", "response_ui_error"}
                and item.get("assistant_identity") and item.get("raw_sha256")}
    exact = []; positional = []
    for message in ledger.get("messages", []):
        if not isinstance(message, dict) or message.get("role") != "assistant": continue
        if (str(message.get("identity")), str(message.get("text_sha256"))) in rejected: continue
        if (request.request_id in message.get("request_ids", [])
                and "CHAT_COURIER_REPLY/1" in str(message.get("text", ""))):
            exact.append(message)
        elif message.get("in_reply_to_request_id") == request.request_id:
            positional.append(message)
    return exact[-1] if exact else (positional[0] if positional else None)

def record_absence_observation(request: Request, *, session_id: str,
                               streaming: bool, composer_ready: bool,
                               anchor_found: bool) -> list[dict[str, Any]]:
    path = request.directory / "absence-observations.json"
    try: value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError): value = {}
    observations = value.get("observations", []) if isinstance(value, dict) else []
    observations = [item for item in observations if isinstance(item, dict)
                    and item.get("payload_fingerprint") == request.payload_fingerprint]
    observations.append({"observed_at": time.time(), "session_id": session_id,
                         "payload_fingerprint": request.payload_fingerprint,
                         "chat_url": request.chat_url, "streaming": bool(streaming),
                         "composer_ready": bool(composer_ready), "anchor_found": bool(anchor_found)})
    atomic_json(path, {"version": 1, "project_id": request.project_id,
                       "request_id": request.request_id, "observations": observations[-10:]})
    return observations[-10:]
