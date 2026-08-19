from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import time
from typing import Any, Iterator

from .config import CourierConfig, ProjectConfig, home_dir, load_config
from .protocol import valid_correlation_id


SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
DEFAULT_MATCH_LOOKBACK_SECONDS = 20 * 60
DEFAULT_POLL_MAX_SECONDS = 360


@dataclass(frozen=True)
class SubjectInfo:
    marker: str
    code: str | None
    kind: str
    title: str
    legacy: bool


@dataclass(frozen=True)
class DeliveryExpectation:
    """Exact identity required before a Gmail message may be consumed."""

    project_code: str
    task_id: str
    keyword: str
    attachment_filename: str = "result.json"
    correlation_id: str = ""


def now() -> str:
    return datetime.now(UTC).isoformat()


def safe_name(name: str, fallback: str = "attachment") -> str:
    value = Path(name.replace("\\", "/")).name.strip().rstrip(". ")
    value = re.sub(r"[<>:\\|?*\x00-\x1f]", "_", value)
    if not value or value.upper().split(".")[0] in RESERVED or value in {".", ".."}:
        return fallback
    return value[:180]


def extract_headers(message: dict[str, Any]) -> dict[str, str]:
    return {item["name"].lower(): item["value"] for item in message.get("payload", {}).get("headers", [])}


def parse_subject(subject: str, config: CourierConfig) -> SubjectInfo | None:
    standard = re.match(rf"^{re.escape(config.prefix)}\[([A-Z0-9_-]+)\](?:\[([A-Z0-9_-]+)\])?\s*(.*)$", subject.strip(), re.I)
    if standard:
        code, kind, title = standard.groups()
        return SubjectInfo(config.prefix, code.upper(), (kind or "TASK").upper(), title.strip(), False)
    if config.legacy_prefix:
        legacy = re.match(rf"^({re.escape(config.legacy_prefix)})(?:\[([A-Z0-9_-]+)\])?\s*(.*)$", subject.strip(), re.I)
        if legacy:
            marker, kind, title = legacy.groups()
            return SubjectInfo(marker.upper(), None, (kind or "TASK").upper(), title.strip(), True)
    return None


def is_protocol_message(message: dict[str, Any], config: CourierConfig) -> bool:
    headers = extract_headers(message)
    subject = headers.get("subject", "")
    sender = headers.get("from", "").lower()
    recipient = headers.get("to", "").lower()
    return parse_subject(subject, config) is not None and config.address.lower() in sender and config.address.lower() in recipient


def route_project(subject: SubjectInfo, config: CourierConfig) -> tuple[ProjectConfig | None, str | None]:
    if subject.legacy:
        candidates = config.legacy_projects(subject.marker)
    else:
        candidates = tuple(project for project in config.projects if subject.code in project.codes)
    if len(candidates) == 1:
        return candidates[0], None
    if not candidates:
        return None, "unknown-project-code"
    return None, "ambiguous-project-code"


def delivery_slug(subject: SubjectInfo, message_id: str) -> str:
    text = subject.title or f"message-{message_id}"
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-.")
    return slug[:80] or f"message-{message_id}"


def inbox_path(project: ProjectConfig) -> Path:
    """Resolve a project's isolated inbox, relative or explicitly external."""
    return (project.root / project.inbox).resolve()


def stored_unit_path(project: ProjectConfig, unit: Path) -> str:
    """Store a relative path for repository inboxes and an absolute path otherwise."""
    try:
        return unit.resolve().relative_to(project.root.resolve()).as_posix()
    except ValueError:
        return str(unit.resolve())


def parts(payload: dict[str, Any]):
    stack = [payload]
    visited = 0
    while stack:
        current = stack.pop()
        if not isinstance(current, dict):
            continue
        visited += 1
        if visited > 10000:
            raise ValueError("Gmail MIME tree exceeds 10000 parts")
        body = current.get("body", {})
        if current.get("filename") and isinstance(body, dict) and body.get("attachmentId"):
            yield current
        children = current.get("parts", [])
        if isinstance(children, list):
            stack.extend(reversed(children))


def _message_text(payload: dict[str, Any]) -> str:
    chunks: list[str] = []
    stack = [(payload, 0)]
    visited = 0
    while stack:
        current, depth = stack.pop()
        if not isinstance(current, dict):
            continue
        visited += 1
        if depth > 1000 or visited > 10000:
            raise ValueError("Gmail MIME tree exceeds safe parsing depth")
        body = current.get("body", {})
        data = body.get("data") if isinstance(body, dict) else None
        mime_type = str(current.get("mimeType", "")).lower()
        if data and mime_type == "text/plain":
            raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
            chunks.append(raw.decode("utf-8"))
        children = current.get("parts", [])
        if isinstance(children, list):
            stack.extend((child, depth + 1) for child in reversed(children))
    return "\n".join(chunk for chunk in chunks if chunk)


def _ascii_text(value: str) -> bool:
    return value.isascii() and all(char in "\r\n\t" or 32 <= ord(char) <= 126 for char in value)


def _contains_term(text: str, term: str) -> bool:
    if not term:
        return False
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", text, re.I) is not None


def _message_epoch(raw: dict[str, Any]) -> int | None:
    try:
        return int(raw.get("internalDate", "")) // 1000
    except (TypeError, ValueError):
        return None


def _is_recent(raw: dict[str, Any], not_before_epoch: int | None) -> bool:
    if not_before_epoch is None:
        return True
    message_epoch = _message_epoch(raw)
    return message_epoch is not None and message_epoch >= not_before_epoch


def _valid_expected_correlation(config: CourierConfig, expected: DeliveryExpectation) -> bool:
    project = config.project_by_code(expected.project_code)
    if not expected.correlation_id or project is None:
        return False
    return valid_correlation_id(project.code, expected.correlation_id, project.aliases)


def _expected_project_terms(config: CourierConfig, expected: DeliveryExpectation) -> tuple[str, ...]:
    project = config.project_by_code(expected.project_code)
    if project is None:
        return (expected.project_code,)
    return tuple(dict.fromkeys(project.codes))


def inspect_candidate(service: Any, config: CourierConfig, message_id: str, expected: DeliveryExpectation, not_before_epoch: int | None = None) -> dict[str, Any] | None:
    """Identify a possibly relevant self-mail without claiming it is accepted."""
    raw = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    if not _is_recent(raw, not_before_epoch):
        return None
    headers = extract_headers(raw)
    account = config.address.lower()
    if account not in headers.get("from", "").lower() or account not in headers.get("to", "").lower():
        return None
    subject = headers.get("subject", "")
    try:
        body = _message_text(raw.get("payload", {}))
    except (UnicodeDecodeError, ValueError):
        body = ""
    attachment_parts = list(parts(raw.get("payload", {})))
    filenames = [str(part.get("filename", "")) for part in attachment_parts]
    searchable = "\n".join((subject, body, *filenames))
    terms = _expected_project_terms(config, expected)
    project_evidence = next((term for term in terms if _contains_term(searchable, term)), None)
    other_project_evidence = [
        term
        for project in config.projects
        if project.code.upper() != expected.project_code.upper()
        for term in project.codes
        if _contains_term(searchable, term)
    ]
    if project_evidence is None and other_project_evidence:
        return None
    reasons: list[str] = []
    if parse_subject(subject, config) is None:
        reasons.append("nonstandard-subject")
    if not _contains_term(subject, expected.task_id) and not _contains_term(body, expected.task_id):
        reasons.append("task-id-not-exact")
    if not _contains_term(subject, expected.keyword) and not _contains_term(body, expected.keyword):
        reasons.append("keyword-not-exact")
    if len([name for name in filenames if name.lower() == expected.attachment_filename.lower()]) != 1:
        reasons.append("attachment-name-not-exact")
    if not _ascii_text(subject) or not _ascii_text(body):
        reasons.append("non-ascii-content")
    classification = "project-related" if project_evidence is not None else "unclassified"
    if project_evidence is None:
        reasons.insert(0, "no-project-evidence")
    return {
        "message_id": message_id,
        "thread_id": raw.get("threadId"),
        "project_code": expected.project_code,
        "project_evidence": project_evidence,
        "classification": classification,
        "subject": subject,
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "body_preview": body[:20000],
        "attachment_filenames": filenames,
        "reasons": reasons or ["strict-contract-not-confirmed"],
        "expected": {"project_code": expected.project_code, "task_id": expected.task_id, "keyword": expected.keyword, "attachment_filename": expected.attachment_filename, "correlation_id": expected.correlation_id},
        "internal_date": raw.get("internalDate"),
        "raw_message": raw,
    }


def quarantine_candidate(home: Path, candidate: dict[str, Any], service: Any) -> Path:
    """Persist a candidate and its readable attachments outside the project inbox."""
    message_id = safe_name(str(candidate["message_id"]), "candidate")
    target = home / "quarantine" / "candidates" / message_id
    target.mkdir(parents=True, exist_ok=True)
    (target / "body.txt").write_text(str(candidate.get("body_preview", "")), encoding="utf-8")
    attachment_dir = target / "attachments"
    attachment_dir.mkdir(parents=True, exist_ok=True)
    saved_attachments: list[dict[str, Any]] = []
    raw = candidate.get("raw_message", {})
    for index, part in enumerate(parts(raw.get("payload", {})), 1):
        filename = safe_name(str(part.get("filename", "")), f"attachment-{index}")
        destination = attachment_dir / filename
        if destination.exists():
            destination = attachment_dir / f"{index}-{filename}"
        attachment_id = (part.get("body") or {}).get("attachmentId")
        item = {"filename": filename, "path": str(destination), "saved": False}
        try:
            encoded = service.users().messages().attachments().get(userId="me", messageId=candidate["message_id"], id=attachment_id).execute()["data"]
            data = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            destination.write_bytes(data)
            item.update({"saved": True, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        except Exception as exc:
            item["error"] = f"{type(exc).__name__}: {exc}"
        saved_attachments.append(item)
    manifest = {key: value for key, value in candidate.items() if key != "raw_message"}
    manifest["candidate"] = True
    manifest["saved_attachments"] = saved_attachments
    atomic_json(target / "candidate.json", manifest)
    return target


def matching_document(service: Any, config: CourierConfig, message_id: str, expected: DeliveryExpectation, not_before_epoch: int | None = None) -> dict[str, Any] | None:
    """Return a validated delivery, with the per-round correlation ID first."""
    raw = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    if not _is_recent(raw, not_before_epoch):
        return None
    headers = extract_headers(raw)
    account = config.address.lower()
    if account not in headers.get("from", "").lower() or account not in headers.get("to", "").lower():
        return None
    subject = headers.get("subject", "")

    # Primary route: the caller-supplied ID is the only subject identity
    # required. This deliberately permits natural-language subjects and
    # arbitrary attachment names; sender/recipient, recency, ASCII content,
    # and a non-empty attachment set remain mandatory safety gates.
    if _valid_expected_correlation(config, expected) and _contains_term(subject, expected.correlation_id):
        try:
            body = _message_text(raw.get("payload", {}))
        except (UnicodeDecodeError, ValueError):
            return None
        attachment_parts = list(parts(raw.get("payload", {})))
        if not _ascii_text(subject) or not _ascii_text(body) or not attachment_parts:
            return None
        return {
            "project_id": expected.project_code,
            "task_id": expected.task_id,
            "keyword": expected.keyword,
            "correlation_id": expected.correlation_id,
            "match_mode": "correlation_id",
            "subject": subject,
        }

    info = parse_subject(subject, config)
    if info is None or info.code != expected.project_code.upper() or not _ascii_text(subject):
        return None
    if expected.task_id not in subject or expected.keyword not in subject:
        return None
    try:
        body = _message_text(raw.get("payload", {}))
    except (UnicodeDecodeError, ValueError):
        return None
    if not _ascii_text(body) or expected.project_code not in body or expected.keyword not in body or expected.task_id not in body:
        return None
    matching_parts = [
        part for part in parts(raw.get("payload", {}))
        if str(part.get("filename", "")).lower() == expected.attachment_filename.lower()
    ]
    if len(matching_parts) != 1:
        return None
    attachment_id = matching_parts[0].get("body", {}).get("attachmentId")
    if not attachment_id:
        return None
    try:
        encoded = service.users().messages().attachments().get(
            userId="me", messageId=message_id, id=attachment_id
        ).execute()["data"]
        attachment_text = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
        if not _ascii_text(attachment_text):
            return None
        document = json.loads(attachment_text)
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(document, dict) or document.get("project_id") != expected.project_code or document.get("task_id") != expected.task_id or document.get("keyword") != expected.keyword:
        return None
    return document


def matches_expectation(service: Any, config: CourierConfig, message_id: str, expected: DeliveryExpectation) -> bool:
    return matching_document(service, config, message_id, expected) is not None


class State:
    def __init__(self, home: Path):
        self.home = home
        home.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(home / "state.sqlite")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS deliveries ("
            "message_id TEXT PRIMARY KEY, project_code TEXT NOT NULL, unit_relpath TEXT NOT NULL, "
            "status TEXT NOT NULL, committed INTEGER NOT NULL DEFAULT 0, pushed INTEGER NOT NULL DEFAULT 0, "
            "error TEXT, received_at TEXT NOT NULL)"
        )
        self.conn.commit()

    def get(self, message_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM deliveries WHERE message_id=?", (message_id,)).fetchone()

    def record(self, message_id: str, project_code: str, unit_relpath: str, status: str = "received") -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO deliveries(message_id,project_code,unit_relpath,status,received_at) VALUES(?,?,?,?,?)",
            (message_id, project_code, unit_relpath, status, now()),
        )
        self.conn.commit()

    def mark(self, message_id: str, **values: Any) -> None:
        allowed = {"status", "committed", "pushed", "error"}
        values = {key: value for key, value in values.items() if key in allowed}
        if not values:
            return
        assignments = ", ".join(f"{key}=?" for key in values)
        self.conn.execute(f"UPDATE deliveries SET {assignments} WHERE message_id=?", (*values.values(), message_id))
        self.conn.commit()

    def pending_push(self):
        return self.conn.execute("SELECT * FROM deliveries WHERE committed=1 AND pushed=0 AND status='received'").fetchall()

    def close(self) -> None:
        self.conn.close()


@contextmanager
def file_lock(path: Path, timeout: float = 30.0) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise RuntimeError(f"lock timeout: {path}")
            time.sleep(0.1)
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
        yield
    finally:
        os.close(fd)
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=path.parent) as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for key in ("GIT_HTTP_PROXY", "GIT_HTTPS_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        environment.pop(key, None)
    return environment


def git(project: ProjectConfig, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "http.proxy=", "-c", "https.proxy=", "-C", str(project.root), *args],
        capture_output=True,
        text=True,
        check=False,
        env=git_environment(),
    )


def assert_project_repo(project: ProjectConfig) -> None:
    if not project.root.exists():
        raise RuntimeError(f"project root does not exist: {project.root}")
    top = git(project, "rev-parse", "--show-toplevel")
    if top.returncode or Path(top.stdout.strip()).resolve() != project.root.resolve():
        raise RuntimeError(f"configured root is not the exact Git worktree: {project.root}")
    branch = git(project, "branch", "--show-current").stdout.strip()
    if branch != project.branch:
        raise RuntimeError(f"refusing Git operation for {project.code}: expected branch {project.branch}, got {branch or '<detached>'}")
    if project.push:
        remote = git(project, "remote", "get-url", project.remote)
        if remote.returncode:
            raise RuntimeError(f"Git remote not found for {project.code}: {project.remote}")
        if project.remote_url and remote.stdout.strip() != project.remote_url:
            raise RuntimeError(f"Git remote mismatch for {project.code}: {remote.stdout.strip()}")


def commit_and_push(project: ProjectConfig, state: State, row: sqlite3.Row) -> None:
    unit = project.root / row["unit_relpath"]
    if not unit.exists():
        raise RuntimeError(f"delivery directory is missing: {unit}")
    try:
        relative = unit.resolve().relative_to(project.root.resolve())
    except ValueError:
        if project.push:
            raise RuntimeError(f"external inbox cannot be pushed for {project.code}: {unit}")
        state.mark(row["message_id"], committed=1, pushed=1, status="received", error="")
        return
    assert_project_repo(project)
    if not row["committed"]:
        add = git(project, "add", "--", str(relative))
        if add.returncode:
            raise RuntimeError(f"git add failed for {project.code}: {add.stderr.strip()}")
        commit = git(project, "commit", "--only", "-m", f"Receive automated delivery {unit.name}", "--", str(relative))
        if commit.returncode:
            raise RuntimeError(f"git commit failed for {project.code}: {commit.stderr.strip()}")
        state.mark(row["message_id"], committed=1, status="received", error="")
        row = state.get(row["message_id"])
    if not project.push:
        state.mark(row["message_id"], pushed=1, status="received", error="")
        return
    push = git(project, "push", project.remote, project.branch)
    if push.returncode:
        state.mark(row["message_id"], error=f"pending push: {push.stderr.strip()[-500:]}")
        return
    state.mark(row["message_id"], pushed=1, status="received", error="")


def quarantine(home: Path, message_id: str, subject: str, reason: str, headers: dict[str, str]) -> None:
    atomic_json(
        home / "quarantine" / f"{message_id}.json",
        {"message_id": message_id, "subject": subject, "reason": reason, "from": headers.get("from", ""), "to": headers.get("to", ""), "quarantined_at": now()},
    )


def existing_unit(project: ProjectConfig, message_id: str) -> Path | None:
    inbox = inbox_path(project)
    if not inbox.exists():
        return None
    for manifest in inbox.glob("*/manifest.json"):
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("message_id") == message_id or data.get("gmail_message_id") == message_id:
            return manifest.parent
    return None


def receive_message(
    service: Any,
    config: CourierConfig,
    state: State,
    summary: dict[str, str],
    home: Path | None = None,
    project_hint: str | None = None,
    correlation_id: str | None = None,
) -> str:
    runtime = home or home_dir()
    message_id = summary["id"]
    prior = state.get(message_id)
    if prior:
        if prior["status"] == "received":
            project = config.project_by_code(prior["project_code"])
            if project and (not prior["committed"] or (project.push and not prior["pushed"])):
                commit_and_push(project, state, prior)
        return "duplicate"
    message = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    headers = extract_headers(message)
    subject = headers.get("subject", "")
    subject_info = parse_subject(subject, config)
    if subject_info is None and project_hint:
        project = config.project_by_code(project_hint)
        if project is not None:
            # matching_document has already authenticated the correlation ID;
            # this synthetic route only makes a natural-language subject
            # deliverable without weakening ordinary daemon routing.
            subject_info = SubjectInfo(config.prefix, project.code, "TASK", subject, False)
    if subject_info is None or config.address.lower() not in headers.get("from", "").lower() or config.address.lower() not in headers.get("to", "").lower():
        return "ignored"
    project, reason = route_project(subject_info, config)
    if project is None:
        quarantine(runtime, message_id, subject, reason or "unroutable", headers)
        state.record(message_id, "__quarantine__", f"quarantine/{message_id}.json", "quarantined")
        return "quarantined"
    attachment_parts = list(parts(message.get("payload", {})))
    if not attachment_parts:
        quarantine(runtime, message_id, subject, "no-attachments", headers)
        state.record(message_id, project.code, f"quarantine/{message_id}.json", "quarantined")
        return "quarantined"
    inbox = inbox_path(project)
    unit_name = delivery_slug(subject_info, message_id)
    final = inbox / unit_name
    if final.exists():
        unit_name = f"{unit_name}-{message_id[:12]}"
        final = inbox / unit_name
    runtime.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="delivery-", dir=runtime) as temporary_dir:
        stage = Path(temporary_dir) / unit_name
        stage.mkdir()
        attachments = []
        used: set[str] = set()
        for index, part in enumerate(attachment_parts, 1):
            filename = safe_name(part["filename"], f"attachment-{index}")
            base = Path(filename)
            while filename.lower() in used:
                filename = f"{base.stem}-{index}{base.suffix}"
            used.add(filename.lower())
            blob = service.users().messages().attachments().get(userId="me", messageId=message_id, id=part["body"]["attachmentId"]).execute()["data"]
            data = base64.urlsafe_b64decode(blob + "=" * (-len(blob) % 4))
            target = stage / filename
            target.write_bytes(data)
            attachments.append({"filename": filename, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
        manifest = {
            "message_id": message_id,
            "gmail_message_id": message_id,
            "thread_id": message.get("threadId"),
            "subject": subject,
            "project_code": project.code,
            "type": subject_info.kind,
            "received_at": message.get("internalDate"),
            "processed_at": now(),
            "protocol": subject_info.marker,
            "correlation_id": correlation_id,
            "attachments": attachments,
        }
        atomic_json(stage / "manifest.json", manifest)
        inbox.mkdir(parents=True, exist_ok=True)
        os.replace(stage, final)
    state.record(message_id, project.code, stored_unit_path(project, final), "received")
    row = state.get(message_id)
    commit_and_push(project, state, row)
    return "received"


def gmail_service(home: Path):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    token = home / "token.json"
    if not token.exists():
        raise RuntimeError(f"not authorized; run gmail-courier auth (token expected at {token})")
    credentials = Credentials.from_authorized_user_file(token, SCOPES)
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        token.write_text(credentials.to_json(), encoding="utf-8")
    if not credentials.valid:
        raise RuntimeError("OAuth token is invalid; run gmail-courier auth")
    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def configure_logging(home: Path) -> None:
    logger = logging.getLogger("gmail_courier")
    if logger.handlers:
        return
    log_path = home / "logs" / "courier.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def _gmail_phrase(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_query(config: CourierConfig, expected: DeliveryExpectation | None = None, phase: str = "broad") -> str:
    """Build one retrieval phase without making the broad phase lossy."""
    base = f'from:{config.address} to:{config.address} has:attachment -in:spam -in:trash'
    if expected is None or phase == "broad":
        return base
    if phase == "correlation":
        if not expected.correlation_id:
            return base
        return f'{base} subject:"{_gmail_phrase(expected.correlation_id)}"'
    if phase == "strict":
        return f'{base} subject:"{_gmail_phrase(expected.task_id)}" subject:"{_gmail_phrase(expected.keyword)}"'
    raise ValueError(f"unknown Gmail query phase: {phase}")


def recorded_unit_path(config: CourierConfig, row: sqlite3.Row) -> Path:
    project = config.project_by_code(str(row["project_code"]))
    if project is None:
        return Path(str(row["unit_relpath"]))
    return project.root / str(row["unit_relpath"])


def sync(home: Path | None = None, *, expected: DeliveryExpectation | None = None, matched_message_ids: list[str] | None = None, matched_documents: list[dict[str, Any]] | None = None, matched_inbox_paths: list[str] | None = None, candidate_messages: list[dict[str, Any]] | None = None, not_before_epoch: int | None = None) -> int:
    runtime = (home or home_dir()).resolve()
    config = load_config(runtime)
    configure_logging(runtime)
    logger = logging.getLogger("gmail_courier")
    if expected is not None and not _valid_expected_correlation(config, expected):
        raise ValueError("expected.correlation_id must start with the configured project code or alias and contain a digit")
    with file_lock(runtime / "courier.lock"):
        state = State(runtime)
        try:
            service = gmail_service(runtime)
            received = 0
            phases = ["broad"] if expected is None else ["correlation", "strict", "broad"]
            seen_ids: set[str] = set()
            for phase in phases:
                response = service.users().messages().list(
                    userId="me", q=build_query(config, expected, phase), maxResults=100
                ).execute()
                summaries = [item for item in response.get("messages", []) if item.get("id") not in seen_ids]
                seen_ids.update(item.get("id") for item in summaries if item.get("id"))
                phase_had_relevant = False
                for summary in summaries:
                    try:
                        document = None
                        if expected:
                            document = matching_document(service, config, summary["id"], expected, not_before_epoch)
                            if document is not None:
                                phase_had_relevant = True
                            if document is None:
                                candidate = inspect_candidate(service, config, summary["id"], expected, not_before_epoch)
                                if candidate is not None:
                                    phase_had_relevant = True
                                    candidate["match_phase"] = phase
                                    candidate_path = quarantine_candidate(runtime, candidate, service)
                                    candidate["candidate_path"] = str(candidate_path.resolve())
                                    candidate.pop("raw_message", None)
                                    if candidate_messages is not None:
                                        candidate_messages.append(candidate)
                                    logger.warning("gmail_candidate message=%s project=%s phase=%s reasons=%s path=%s", summary["id"], expected.project_code, phase, ",".join(candidate["reasons"]), candidate_path)
                                continue
                        result = receive_message(
                            service,
                            config,
                            state,
                            summary,
                            runtime,
                            project_hint=expected.project_code if expected and document and document.get("match_mode") == "correlation_id" else None,
                            correlation_id=expected.correlation_id if expected and document and document.get("match_mode") == "correlation_id" else None,
                        )
                        if result == "received":
                            received += 1
                            if matched_message_ids is not None:
                                matched_message_ids.append(summary["id"])
                            if matched_documents is not None and document is not None:
                                matched_documents.append(document)
                            if matched_inbox_paths is not None:
                                row = state.get(summary["id"])
                                if row:
                                    matched_inbox_paths.append(str(recorded_unit_path(config, row).resolve()))
                        logger.info("message=%s phase=%s result=%s", summary["id"], phase, result)
                    except Exception as exc:
                        logger.exception("message=%s phase=%s delivery failed: %s", summary.get("id"), phase, exc)
                # A returned correlation/strict search is authoritative for
                # this round. Do not widen the search and risk consuming a
                # different message after a phase has produced a hit.
                if phase_had_relevant or expected is None:
                    break
            for row in state.pending_push():
                project = config.project_by_code(row["project_code"])
                if not project:
                    state.mark(row["message_id"], error="project removed from registry")
                    continue
                try:
                    commit_and_push(project, state, row)
                except Exception as exc:
                    logger.exception("message=%s retry failed: %s", row["message_id"], exc)
            return received
        finally:
            state.close()


def sync_until_received(
    home: Path | None = None,
    *,
    max_seconds: int = DEFAULT_POLL_MAX_SECONDS,
    interval_seconds: int = 10,
    on_poll=None,
    expected: DeliveryExpectation | None = None,
    lookback_seconds: int = DEFAULT_MATCH_LOOKBACK_SECONDS,
    initial_delay_seconds: int = 0,
    stop_event=None,
) -> dict[str, object]:
    """Poll Gmail in this process until a new delivery is received or timed out.

    ``on_poll`` receives a JSON-serializable status dictionary after each
    attempt. The caller can stream it to an Agent, so receipt is known without
    another watcher guessing from filesystem state.
    """
    if max_seconds <= 0 or interval_seconds <= 0 or lookback_seconds <= 0 or initial_delay_seconds < 0:
        raise ValueError("max_seconds, interval_seconds, and lookback_seconds must be positive; initial_delay_seconds cannot be negative")
    if expected is not None:
        config = load_config(home or home_dir())
        if not _valid_expected_correlation(config, expected):
            raise ValueError("expected.correlation_id must start with the configured project code or alias and contain a digit")
    started = time.monotonic()
    not_before_epoch = int(time.time()) - lookback_seconds
    attempts = 0
    errors: list[str] = []
    candidate_history: list[dict[str, Any]] = []
    if initial_delay_seconds:
        delay_deadline = time.monotonic() + initial_delay_seconds
        while time.monotonic() < delay_deadline:
            if stop_event is not None and stop_event.is_set():
                return {
                    "event": "gmail_poll_cancelled",
                    "attempt": 0,
                    "received": 0,
                    "matched_message_ids": [],
                    "matched_documents": [],
                    "matched_inbox_paths": [],
                    "candidate_messages": [],
                    "ok": False,
                    "elapsed_seconds": round(time.monotonic() - started, 1),
                    "errors": [],
                }
            time.sleep(min(0.25, max(0.01, delay_deadline - time.monotonic())))
    while True:
        if stop_event is not None and stop_event.is_set():
            return {
                "event": "gmail_poll_cancelled",
                "attempt": attempts,
                "received": 0,
                "matched_message_ids": [],
                "matched_documents": [],
                "matched_inbox_paths": [],
                "candidate_messages": candidate_history,
                "ok": False,
                "elapsed_seconds": round(time.monotonic() - started, 1),
                "errors": errors[-5:],
            }
        attempts += 1
        matched_message_ids: list[str] = []
        matched_documents: list[dict[str, Any]] = []
        matched_inbox_paths: list[str] = []
        candidate_messages: list[dict[str, Any]] = []
        try:
            received = sync(home, expected=expected, matched_message_ids=matched_message_ids, matched_documents=matched_documents, matched_inbox_paths=matched_inbox_paths, candidate_messages=candidate_messages, not_before_epoch=not_before_epoch)
            for candidate in candidate_messages:
                if not any(item.get("message_id") == candidate.get("message_id") for item in candidate_history):
                    candidate_history.append(candidate)
            status = {"event": "gmail_candidate" if candidate_messages and not received else "gmail_poll", "attempt": attempts, "received": received, "matched_message_ids": matched_message_ids, "matched_documents": matched_documents, "matched_inbox_paths": matched_inbox_paths, "candidate_messages": candidate_messages, "error": None}
            if received > 0:
                status = status | {"event": "gmail_received", "ok": True, "elapsed_seconds": round(time.monotonic() - started, 1)}
                if on_poll:
                    on_poll(status)
                return status
            if on_poll:
                on_poll(status)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            errors.append(error)
            status = {"event": "gmail_poll", "attempt": attempts, "received": 0, "matched_message_ids": [], "matched_documents": [], "matched_inbox_paths": [], "candidate_messages": [], "error": error}
            if on_poll:
                on_poll(status)
        elapsed = time.monotonic() - started
        remaining = max_seconds - elapsed
        if remaining <= 0:
            return {
                "event": "gmail_poll_timeout",
                "attempt": attempts,
                "received": 0,
                "matched_message_ids": [],
                "matched_documents": [],
                "matched_inbox_paths": [],
                "candidate_messages": candidate_history,
                "ok": False,
                "elapsed_seconds": round(elapsed, 1),
                "errors": errors[-5:],
            }
        sleep_deadline = time.monotonic() + min(interval_seconds, remaining)
        while time.monotonic() < sleep_deadline:
            if stop_event is not None and stop_event.is_set():
                return {
                    "event": "gmail_poll_cancelled",
                    "attempt": attempts,
                    "received": 0,
                    "matched_message_ids": [],
                    "matched_documents": [],
                    "matched_inbox_paths": [],
                    "candidate_messages": candidate_history,
                    "ok": False,
                    "elapsed_seconds": round(time.monotonic() - started, 1),
                    "errors": errors[-5:],
                }
            time.sleep(min(0.25, max(0.01, sleep_deadline - time.monotonic())))
