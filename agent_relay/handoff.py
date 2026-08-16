from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import EXPECTED_CHAT_URL
from .storage import atomic_json, now


PROTOCOL = "AGENTRELAY_HANDOFF/1"


def evidence_path(project_storage: Path, lease_id: str) -> Path:
    return project_storage / "handoffs" / f"{lease_id}.json"


def write_evidence(
    project_storage: Path,
    *,
    lease_id: str,
    worker_id: str,
    handoff_token: str,
    chat_url: str,
    send_attempts: int = 1,
    navigation_attempts: int = 1,
    verification_attempts: int = 1,
    submission_verified: bool = True,
) -> Path:
    if chat_url != EXPECTED_CHAT_URL:
        raise ValueError("handoff URL does not match the fixed ChatGPT target")
    if not handoff_token or send_attempts != 1 or not 0 <= navigation_attempts <= 2 or not 0 <= verification_attempts <= 1 or submission_verified is not True:
        raise ValueError("handoff evidence exceeds the bounded certification contract")
    target = evidence_path(project_storage, lease_id)
    if target.exists():
        raise ValueError("handoff evidence already exists for this lease")
    atomic_json(target, {
        "protocol": PROTOCOL,
        "lease_id": lease_id,
        "worker_id": worker_id,
        "handoff_token": handoff_token,
        "chat_url": chat_url,
        "send_attempts": send_attempts,
        "navigation_attempts": navigation_attempts,
        "verification_attempts": verification_attempts,
        "submission_verified": True,
        "recorded_at": now(),
    })
    return target


def validate_evidence(project_storage: Path, active: dict[str, Any], *, handoff_token: str = "") -> dict[str, Any]:
    path = evidence_path(project_storage, str(active.get("lease_id", "")))
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("handoff evidence is missing or malformed") from exc
    expected_token = handoff_token or str(active.get("handoff_token", ""))
    if (
        value.get("protocol") != PROTOCOL
        or value.get("lease_id") != active.get("lease_id")
        or value.get("worker_id") != active.get("worker_id")
        or value.get("handoff_token") != expected_token
        or value.get("chat_url") != EXPECTED_CHAT_URL
        or value.get("send_attempts") != 1
        or not isinstance(value.get("navigation_attempts"), int) or not 0 <= value["navigation_attempts"] <= 2
        or not isinstance(value.get("verification_attempts"), int) or not 0 <= value["verification_attempts"] <= 1
        or value.get("submission_verified") is not True
    ):
        raise ValueError("handoff evidence identity or bounded submission proof is invalid")
    return value
