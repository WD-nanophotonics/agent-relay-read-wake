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


SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


@dataclass(frozen=True)
class SubjectInfo:
    marker: str
    code: str | None
    kind: str
    title: str
    legacy: bool


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


def parts(payload: dict[str, Any]):
    for part in payload.get("parts", []):
        yield from parts(part)
    if payload.get("filename") and payload.get("body", {}).get("attachmentId"):
        yield payload


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
    assert_project_repo(project)
    unit = project.root / row["unit_relpath"]
    if not unit.exists():
        raise RuntimeError(f"delivery directory is missing: {unit}")
    if not row["committed"]:
        relative = unit.relative_to(project.root)
        add = git(project, "add", "--", str(relative))
        if add.returncode:
            raise RuntimeError(f"git add failed for {project.code}: {add.stderr.strip()}")
        commit = git(project, "commit", "--only", "-m", f"Receive Gmail Courier delivery {unit.name}", "--", str(relative))
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
    inbox = project.root / project.inbox
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


def receive_message(service: Any, config: CourierConfig, state: State, summary: dict[str, str], home: Path | None = None) -> str:
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
    inbox = project.root / project.inbox
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
            "attachments": attachments,
        }
        atomic_json(stage / "manifest.json", manifest)
        inbox.mkdir(parents=True, exist_ok=True)
        os.replace(stage, final)
    state.record(message_id, project.code, str(final.relative_to(project.root)), "received")
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


def build_query(config: CourierConfig) -> str:
    return f'from:{config.address} to:{config.address} has:attachment {{subject:"{config.prefix}" subject:"{config.legacy_prefix}"}}'


def sync(home: Path | None = None) -> int:
    runtime = (home or home_dir()).resolve()
    config = load_config(runtime)
    configure_logging(runtime)
    logger = logging.getLogger("gmail_courier")
    with file_lock(runtime / "courier.lock"):
        state = State(runtime)
        try:
            service = gmail_service(runtime)
            response = service.users().messages().list(userId="me", q=build_query(config), maxResults=100).execute()
            received = 0
            for summary in response.get("messages", []):
                try:
                    result = receive_message(service, config, state, summary, runtime)
                    if result == "received":
                        received += 1
                    logger.info("message=%s result=%s", summary["id"], result)
                except Exception as exc:
                    logger.exception("message=%s delivery failed: %s", summary.get("id"), exc)
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
