from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from chat_courier.model import ValidationError, load_request
from chat_courier.storage import event, receipt
from chat_courier.workflow import capabilities, configure_project, prepare_request, request_status


def configured(tmp_path: Path, monkeypatch, *, count: int = 3, single: int = 100, total: int = 200):
    runtime = tmp_path / "runtime"
    monkeypatch.setattr("chat_courier.workflow.runtime_root", lambda: runtime)
    monkeypatch.setattr("chat_courier.workflow._load_registry",
                        lambda: {"TEST": "https://chatgpt.com/c/test"})
    monkeypatch.setattr("chat_courier.model._load_registry",
                        lambda: {"TEST": "https://chatgpt.com/c/test"})
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    outbox = tmp_path / "outbox"
    configure_project("TEST", str(outbox), [str(artifacts)], count, single, total)
    return artifacts, outbox


def test_prepare_creates_v1_request_and_attachment_attestation(tmp_path, monkeypatch):
    artifacts, outbox = configured(tmp_path, monkeypatch)
    source = artifacts / "evidence.txt"
    source.write_text("evidence", encoding="utf-8")
    value = prepare_request("TEST", "STATUS-1", "hello", [str(source)])
    request = Path(value["request_directory"])
    manifest = json.loads((request / "request.json").read_text())
    attestation = json.loads((request / "attachment-attestation.json").read_text())
    assert request.parent == outbox and manifest["version"] == 1
    assert manifest["attachments"] == ["attachments/evidence.txt"]
    assert attestation["attachments"][0]["sha256"] == hashlib.sha256(b"evidence").hexdigest()
    assert request_status(request)["state"] == "prepared"


def test_prepare_is_idempotent_and_rejects_payload_drift(tmp_path, monkeypatch):
    configured(tmp_path, monkeypatch)
    first = prepare_request("TEST", "STATUS-1", "hello")
    second = prepare_request("TEST", "STATUS-1", "hello")
    assert first["request_id"] == second["request_id"] and second["state"] == "existing"
    with pytest.raises(ValidationError, match="different content"):
        prepare_request("TEST", "STATUS-1", "changed")


def test_prepare_rejects_attachment_escape_and_limits(tmp_path, monkeypatch):
    artifacts, _ = configured(tmp_path, monkeypatch, count=1, single=4, total=4)
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    with pytest.raises(ValidationError, match="outside"):
        prepare_request("TEST", "ESCAPE-1", "hello", [str(outside)])
    large = artifacts / "large.bin"
    large.write_bytes(b"12345")
    with pytest.raises(ValidationError, match="size"):
        prepare_request("TEST", "LARGE-1", "hello", [str(large)])


def test_capabilities_exposes_policy_not_url_or_profile(tmp_path, monkeypatch):
    configured(tmp_path, monkeypatch)
    value = capabilities()
    assert value["projects"] == ["TEST"]
    assert value["arbitrary_url"] is False and value["arbitrary_profile"] is False
    assert "chat_url" not in value["project_policies"]["TEST"]
    assert "courier_capture_latest" in value["operations"]
    assert "courier_retry_once" in value["operations"]


def test_post_submission_browser_close_remains_same_request_recoverable(tmp_path, monkeypatch):
    configured(tmp_path, monkeypatch)
    prepared = prepare_request("TEST", "RECOVER-1", "hello")
    request = load_request(prepared["request_directory"])
    event(request, "request_submitted", phase="submit")
    receipt(request, "courier_error", "page was closed")
    value = request_status(request.directory)
    assert value["state"] == "courier_error"
    assert value["terminal"] is False
    assert value["recovery_only_required"] is True


def test_status_proves_live_shared_chat_contention(tmp_path, monkeypatch):
    configured(tmp_path, monkeypatch)
    prepared = prepare_request("TEST", "BUSY-1", "hello")
    request = load_request(prepared["request_directory"])
    receipt(
        request,
        "chat_busy_waiting",
        "shared Chat is streaming",
        runner_pid=123,
        wait_deadline_at=2000.0,
        agent_action_required=False,
        safe_next_action="wait_for_same_request",
    )
    monkeypatch.setattr("chat_courier.workflow.process_alive", lambda pid: pid == 123)
    monkeypatch.setattr("chat_courier.workflow.time.time", lambda: 1500.0)

    value = request_status(request.directory)

    assert value["state"] == "chat_busy_waiting"
    assert value["state_class"] == "WAITING"
    assert value["contention_wait_active"] is True
    assert value["agent_action_required"] is False
    assert value["safe_next_action"] == "wait_for_same_request"
    assert value["next_check_at"] == 2000.0


def test_status_rejects_stale_shared_chat_contention(tmp_path, monkeypatch):
    configured(tmp_path, monkeypatch)
    prepared = prepare_request("TEST", "BUSY-STALE-1", "hello")
    request = load_request(prepared["request_directory"])
    receipt(
        request,
        "chat_busy_waiting",
        "shared Chat was streaming",
        runner_pid=123,
        wait_deadline_at=1000.0,
        agent_action_required=False,
        safe_next_action="wait_for_same_request",
    )
    monkeypatch.setattr("chat_courier.workflow.process_alive", lambda _pid: True)
    monkeypatch.setattr("chat_courier.workflow.time.time", lambda: 1500.0)

    assert request_status(request.directory)["contention_wait_active"] is False


def test_status_preserves_rate_limit_cooldown_if_runner_exits(tmp_path, monkeypatch):
    configured(tmp_path, monkeypatch)
    prepared = prepare_request("TEST", "LIMIT-1", "hello")
    request = load_request(prepared["request_directory"])
    receipt(
        request,
        "chat_busy_waiting",
        "temporary usage limit",
        contention_reason="chat_rate_limited",
        runner_pid=123,
        wait_deadline_at=2000.0,
        agent_action_required=False,
        safe_next_action="wait_for_same_request",
    )
    monkeypatch.setattr("chat_courier.workflow.process_alive", lambda _pid: False)
    monkeypatch.setattr("chat_courier.workflow.time.time", lambda: 1500.0)

    value = request_status(request.directory)

    assert value["state"] == "chat_busy_waiting"
    assert value["contention_wait_active"] is True
    assert value["agent_action_required"] is False
    assert value["safe_next_action"] == "wait_for_same_request"
