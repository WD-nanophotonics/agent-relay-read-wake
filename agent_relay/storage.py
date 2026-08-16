from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any

from .protocol import ProtocolEnvelope


def now() -> str:
    return datetime.now(UTC).isoformat()


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=path.parent) as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
        temp = Path(handle.name)
    os.replace(temp, path)


def default_state() -> dict[str, Any]:
    return {"state": "STOPPED", "current_run": None, "expected_step": 1, "expected_parent": 0, "consumed_message_ids": [], "logical_steps": {}, "active_lease": None, "last": {}, "last_error": None}


class StateStore:
    def __init__(self, project_root: Path):
        self.root = project_root
        self.path = self.root / "state.json"

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return default_state()
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"malformed supervisor state: {self.path}") from exc
        if not isinstance(value, dict) or not isinstance(value.get("consumed_message_ids"), list):
            raise ValueError("malformed supervisor state fields")
        return {**default_state(), **value}

    def save(self, state: dict[str, Any]) -> None:
        atomic_json(self.path, state)


class Ledger:
    def __init__(self, project_root: Path):
        self.path = project_root / "ledger" / "events.jsonl"

    def append(self, event: str, **values: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {"timestamp": now(), "event": event, **{key: value for key, value in values.items() if value is not None}}
        # Protocol/event fields cannot contain OAuth material; never accept arbitrary exception reprs here.
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")


def stage_instruction(project_root: Path, message: Any, envelope: ProtocolEnvelope) -> Path:
    """Atomically stage a downloaded Gmail message and its attachments."""
    inbox = project_root / "inbox" / envelope.run_id / f"STEP-{envelope.step:04d}"
    if inbox.exists():
        raise FileExistsError(f"instruction path already exists: {inbox}")
    project_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="stage-", dir=project_root) as temp_root:
        temp = Path(temp_root) / "instruction"
        attachments_dir = temp / "attachments"
        attachments_dir.mkdir(parents=True)
        body = message.body.encode("utf-8")
        (temp / "message.txt").write_bytes(body)
        attachments = []
        for index, attachment in enumerate(message.attachments, 1):
            name = Path(attachment.filename.replace("\\", "/")).name or f"attachment-{index}"
            target = attachments_dir / name
            if target.exists():
                target = attachments_dir / f"{index}-{name}"
            target.write_bytes(attachment.data)
            attachments.append({"filename": target.name, "bytes": len(attachment.data), "sha256": hashlib.sha256(attachment.data).hexdigest()})
        content_hash = hashlib.sha256(body + b"\0" + b"\0".join(item.data for item in message.attachments)).hexdigest()
        manifest = {"gmail_message_id": message.message_id, "gmail_thread_id": message.thread_id, "received_at": message.received_at, "staged_at": now(), "protocol": asdict(envelope), "content_sha256": content_hash, "attachments": attachments}
        atomic_json(temp / "manifest.json", manifest)
        inbox.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temp, inbox)
    return inbox


def read_content_hash(staged_path: Path) -> str:
    return json.loads((staged_path / "manifest.json").read_text(encoding="utf-8"))["content_sha256"]
