from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time

from .browser import BrowserError, ChatAccessDenied, ChatAuthenticationRequired, ChatComposerNotReady, ChatConversationMismatch, ChatSession, PreSubmissionError, ProfileConfigurationError, SubmissionUnconfirmed
from .owner import OwnerBusy, process_alive, read_owner
from .model import ACTIVE_SETUP_BUDGET_SECONDS, CALLER_GRACE_SECONDS, ValidationError, atomic_json, confirm_url_registration, load_request, minimum_caller_window_seconds, propose_url_registration
from .protocol import build_prompt, parse_reply
from .queue import CourierQueue, QueueIntegrityError, QueueStatus
from .storage import (event, load_receipt, load_response_capture, load_response_cursor,
                      receipt, save_response, save_response_capture)
from .workflow import (
    RECOVERY_ONLY_STATES, capabilities, configure_project, prepare_request,
    request_status, wait_status,
)


COURIER_SOURCE_ROOT = Path(__file__).resolve().parent.parent
_BUILD_COMPONENTS = (
    "cli.py", "browser.py", "model.py", "protocol.py", "queue.py",
    "owner.py", "liveness.py", "storage.py", "workflow.py",
)


def _source_build_id() -> str:
    """Short content ID so a caller can tell which local Courier it ran."""
    digest = hashlib.sha256()
    package = COURIER_SOURCE_ROOT / "chat_courier"
    for name in _BUILD_COMPONENTS:
        path = package / name
        digest.update(name.encode("utf-8"))
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<unavailable>")
    return digest.hexdigest()[:16]


COURIER_BUILD_ID = _source_build_id()


def emit(name: str, *, ok: bool, **values) -> None:
    print(json.dumps({"event": name, "ok": ok, "courier_source_root": str(COURIER_SOURCE_ROOT), "courier_build_id": COURIER_BUILD_ID, **values}, ensure_ascii=False, sort_keys=True), flush=True)


def _queue_fields(status: QueueStatus) -> dict[str, object]:
    return {
        "queue_ticket": status.ticket,
        "queue_position": status.position,
        "ahead_count": status.ahead,
        "queue_waited_seconds": status.waited_seconds,
        "estimated_wait_upper_bound_seconds": status.estimated_wait_upper_bound_seconds,
        "current_owner": status.current_owner,
    }


def validate_command(args: argparse.Namespace) -> int:
    try:
        request = load_request(args.request_directory)
    except ValidationError as exc:
        emit("validation_failed", ok=False, detail=str(exc), phase="validate")
        return 2
    emit("validation_passed", ok=True, phase="validate", project_id=request.project_id, request_id=request.request_id, request_directory=str(request.directory), attachments=[str(path.relative_to(request.directory)) for path in request.attachments], workflow_window_seconds=request.workflow_window_seconds, workflow_window_scope="post_submission_response", queue_wait_seconds=request.queue_wait_seconds, active_setup_budget_seconds=ACTIVE_SETUP_BUDGET_SECONDS, minimum_caller_window_seconds=minimum_caller_window_seconds(request.queue_wait_seconds, request.workflow_window_seconds))
    return 0


def preflight_command(args: argparse.Namespace) -> int:
    """Verify the dedicated profile and fixed Chat conversation without sending."""
    try:
        request = load_request(args.request_directory)
    except ValidationError as exc:
        emit("validation_failed", ok=False, detail=str(exc), phase="validate")
        return 2
    try:
        queue_status = CourierQueue(request).observe()
    except (QueueIntegrityError, RuntimeError, OSError) as exc:
        emit("configuration_error", ok=False, phase="preflight", project_id=request.project_id, request_id=request.request_id, detail=str(exc))
        return 2
    if queue_status.state != "empty":
        emit("queue_waiting", ok=True, phase="preflight", project_id=request.project_id, request_id=request.request_id, browser_started=False, **_queue_fields(queue_status))
        return 0
    try:
        with ChatSession(request, prepare_only=True) as session:
            emit(
                "chat_ready",
                ok=True,
                phase="preflight",
                project_id=request.project_id,
                request_id=request.request_id,
                chat_url=request.chat_url,
                profile=str(session.profile),
                profile_directory=session.profile_directory,
            )
            return 0
    except ChatAuthenticationRequired as exc:
        emit(
            "chat_auth_required",
            ok=False,
            phase="preflight",
            project_id=request.project_id,
            request_id=request.request_id,
            detail=f"{exc}; profile={_profile_for_request(request)}",
        )
        return 1
    except ChatAccessDenied as exc:
        emit("chat_access_denied", ok=False, phase="preflight", project_id=request.project_id, request_id=request.request_id, detail=str(exc))
        return 1
    except ChatConversationMismatch as exc:
        emit("chat_target_mismatch", ok=False, phase="preflight", project_id=request.project_id, request_id=request.request_id, detail=str(exc))
        return 1
    except ChatComposerNotReady as exc:
        emit("chat_composer_not_ready", ok=False, phase="preflight", project_id=request.project_id, request_id=request.request_id, detail=str(exc), composer_snapshot=exc.snapshot)
        return 1
    except ProfileConfigurationError as exc:
        emit("configuration_error", ok=False, phase="preflight", project_id=request.project_id, request_id=request.request_id, detail=str(exc))
        return 2
    except OwnerBusy as exc:
        # A legacy/in-flight run may predate the durable queue.  It is busy,
        # not a browser failure, and must not prompt a replacement request.
        emit("queue_waiting", ok=True, phase="preflight", project_id=request.project_id, request_id=request.request_id, browser_started=False, detail=str(exc))
        return 0
    except BrowserError as exc:
        emit("browser_error", ok=False, phase="preflight", project_id=request.project_id, request_id=request.request_id, detail=str(exc))
        return 1


def register_command(args: argparse.Namespace) -> int:
    try:
        value = propose_url_registration(args.project_id, args.url)
    except ValidationError as exc:
        emit("configuration_error", ok=False, phase="register", detail=str(exc))
        return 2
    if value["state"] == "already_registered":
        emit("chat_url_registered", ok=True, phase="register", **value)
        return 0
    emit(
        "registration_confirmation_required",
        ok=False,
        phase="register",
        **value,
        next_command=(
            "chat-courier confirm-register "
            f"--project-id {value['project_id']} --confirmation-id {value['confirmation_id']} "
            "--basis user_direct|prior_authorization"
        ),
    )
    return 3


def confirm_register_command(args: argparse.Namespace) -> int:
    try:
        value = confirm_url_registration(args.project_id, args.confirmation_id, args.basis)
    except ValidationError as exc:
        emit("configuration_error", ok=False, phase="confirm_register", detail=str(exc))
        return 2
    emit("chat_url_registered", ok=True, phase="confirm_register", **value)
    return 0


def _capture_response(session: ChatSession, request, baseline: set[str] | None, deadline: float,
                      *, legacy_recovery: bool = False) -> str:
    """Capture one completed assistant turn before the browser is closed."""
    marker = f"REQUEST_ID={request.request_id}" if legacy_recovery else None
    candidate = session.wait_for_reply(baseline, deadline, after_user_marker=marker)
    if candidate is None:
        return "response_timeout"
    capture = save_response_capture(
        request, identity=candidate.identity, index=candidate.index, text=candidate.text,
    )
    values = {
        "raw_path": capture["raw_path"], "raw_sha256": capture["raw_sha256"],
        "assistant_identity": capture["assistant_identity"],
    }
    receipt(request, "response_captured", "A completed assistant turn was durably captured", **values)
    event(request, "response_captured", phase="receive", **values)
    emit("response_captured", ok=True, phase="receive", project_id=request.project_id,
         request_id=request.request_id, **values)
    return "response_captured"


def _parse_captured_response(request) -> tuple[str, str | None, dict[str, object]]:
    """Parse a durable capture without opening or retaining a browser."""
    loaded = load_response_capture(request)
    if loaded is None:
        return "response_capture_missing", None, {}
    capture, text = loaded
    values = {"raw_path": capture["raw_path"], "raw_sha256": capture["raw_sha256"],
              "assistant_identity": capture["assistant_identity"]}
    try:
        reply = parse_reply(text, request)
    except ValidationError as exc:
        return "response_protocol_error", None, {**values, "protocol_detail": str(exc)}
    return "response_received", reply.body, values


def _upload_status(request, name: str, **values) -> None:
    event(request, name, phase="upload", **values)
    failed = {"attachment_upload_failed", "attachment_upload_stalled", "browser_page_unresponsive", "page_closed_during_upload"}
    emit(name, ok=name not in failed, phase="upload", project_id=request.project_id, request_id=request.request_id, **values)


def _run_session_once(request, submitted: bool, reply_window_seconds: int) -> str:
    emit("browser_launch_requested", ok=True, phase="browser", project_id=request.project_id, request_id=request.request_id)
    event(request, "browser_launch_requested", phase="browser")
    with ChatSession(request, recovery=submitted, status_callback=lambda name, **values: _upload_status(request, name, **values)) as session:
        event(request, "browser_started", phase="browser", profile=str(session.profile), attached_existing=session.attached_existing)
        emit("browser_started", ok=True, phase="browser", project_id=request.project_id, request_id=request.request_id, attached_existing=session.attached_existing)
        if submitted:
            baseline = load_response_cursor(request)
            legacy_recovery = baseline is None
            receipt(request, "waiting_for_response", "Resuming read-only search for an already submitted request", attached_existing=session.attached_existing)
            emit("response_waiting", ok=True, phase="receive", project_id=request.project_id, request_id=request.request_id, resumed=True, attached_existing=session.attached_existing)
            deadline = time.monotonic() + reply_window_seconds
        else:
            baseline = session.submit(build_prompt(request), request.attachments)
            legacy_recovery = False
            receipt(request, "request_submitted", "ChatGPT user turn was visibly confirmed", owner_pid=session.owner.record.owner_pid if session.owner.record else None, owner_nonce=session.owner.record.owner_nonce if session.owner.record else None)
            event(request, "request_submitted", phase="submit")
            emit("request_submitted", ok=True, phase="submit", project_id=request.project_id, request_id=request.request_id)
            receipt(request, "waiting_for_response", "Waiting for one completed assistant reply")
            emit("response_waiting", ok=True, phase="receive", project_id=request.project_id, request_id=request.request_id, resumed=False)
            # The configured workflow window is the Chat response allowance,
            # not a budget consumed by Chrome launch, navigation, or uploads.
            deadline = time.monotonic() + reply_window_seconds
        return _capture_response(session, request, baseline, deadline, legacy_recovery=legacy_recovery)


def _submission_confirmed(previous: dict | None) -> bool:
    """Only explicit post-Send states may suppress another submission attempt."""
    return bool(previous and previous.get("state") in {
        "request_submitted", "waiting_for_response", "submission_unconfirmed",
        "response_timeout", "response_captured", "response_protocol_error",
    })


def _safe_pre_browser_turn_recovery(previous: dict | None, request=None) -> bool:
    """Permit only the durable gap before any browser ownership was recorded."""
    if not previous or previous.get("state") not in {"queue_turn_acquired", "submission_intent", "courier_interrupted"}:
        return False
    if previous.get("state") == "courier_interrupted" and previous.get("interruption_stage") != "pre_browser":
        return False
    if previous.get("state") == "submission_intent" and request is None:
        return False
    try:
        owner = read_owner()
        if owner is None:
            return previous.get("state") != "submission_intent"
        # A host can terminate Courier after it has written the initial owner
        # record but before Chrome is launched.  This exact shape is still a
        # pre-browser boundary: no browser PID and no CDP port were published,
        # and the owner PID is dead.  It is safe to reacquire the owner for the
        # same immutable request.  Any later phase remains fail-closed.
        return (
            (request is None or (
                getattr(owner, "project_id", None) == request.project_id
                and getattr(owner, "request_id", None) == request.request_id
            ))
            and
            getattr(owner, "phase", None) == "starting"
            and getattr(owner, "browser_pid", None) is None
            and getattr(owner, "cdp_port", None) is None
            and not process_alive(getattr(owner, "owner_pid", 0))
        )
    except OwnerBusy:
        return False


def _write_not_ready_diagnostic(request, exc: ChatComposerNotReady) -> str:
    path = request.directory / "transport_diagnostic.json"
    atomic_json(path, {
        "version": 1,
        "project_id": request.project_id,
        "request_id": request.request_id,
        "failure_stage": "composer_not_ready",
        "detail": str(exc),
        "next_action": "agent_decision_required",
        "safe_to_retry_same_request": True,
        "timeline": [exc.snapshot],
        "last_sample": exc.snapshot,
        "screenshot_error": "browser session closed before a screenshot could be captured",
    })
    return str(path)


def _run_after_queue(request, previous: dict | None) -> int:
    event(request, "request_validated", phase="validate")
    emit("request_validated", ok=True, phase="validate", project_id=request.project_id, request_id=request.request_id, workflow_window_seconds=request.workflow_window_seconds, workflow_window_scope="post_submission_response", queue_wait_seconds=request.queue_wait_seconds, active_setup_budget_seconds=ACTIVE_SETUP_BUDGET_SECONDS, minimum_caller_window_seconds=minimum_caller_window_seconds(request.queue_wait_seconds, request.workflow_window_seconds))
    deadline = time.monotonic() + request.workflow_window_seconds
    submitted = _submission_confirmed(previous)
    if not submitted:
        emit("submission_intent_writing", ok=True, phase="submit", project_id=request.project_id, request_id=request.request_id, browser_started=False)
        event(request, "submission_intent_writing", phase="submit")
        receipt(request, "submission_intent", "Courier is about to submit; do not automatically resend if interrupted")
        emit("submission_intent_written", ok=True, phase="submit", project_id=request.project_id, request_id=request.request_id, browser_started=False)
        event(request, "submission_intent_written", phase="submit")
    try:
        if load_response_capture(request) is None:
            while True:
                try:
                    outcome = _run_session_once(request, submitted, request.workflow_window_seconds)
                    break
                except OwnerBusy as exc:
                    if not submitted:
                        raise BrowserError(str(exc)) from exc
                    event(request, "owner_active", phase="recovery", detail=str(exc))
                    emit("owner_active", ok=True, phase="recovery", project_id=request.project_id, request_id=request.request_id, detail=str(exc))
                    if time.monotonic() >= deadline:
                        raise BrowserError("recovery deadline expired while another Courier owned the browser") from exc
                    time.sleep(1)
        else:
            outcome = "response_captured"
    except ChatAuthenticationRequired as exc:
        detail = f"{exc}; profile={_profile_for_request(request)}"
        receipt(request, "chat_auth_required", detail)
        event(request, "chat_auth_required", phase="browser", detail=detail)
        emit("chat_auth_required", ok=False, phase="browser", project_id=request.project_id, request_id=request.request_id, detail=detail)
        return 1
    except ChatAccessDenied as exc:
        detail = str(exc)
        receipt(request, "chat_access_denied", detail)
        event(request, "chat_access_denied", phase="browser", detail=detail)
        emit("chat_access_denied", ok=False, phase="browser", project_id=request.project_id, request_id=request.request_id, detail=detail)
        return 1
    except ChatConversationMismatch as exc:
        detail = str(exc)
        receipt(request, "chat_target_mismatch", detail)
        event(request, "chat_target_mismatch", phase="browser", detail=detail)
        emit("chat_target_mismatch", ok=False, phase="browser", project_id=request.project_id, request_id=request.request_id, detail=detail)
        return 1
    except ChatComposerNotReady as exc:
        detail = str(exc)
        values = {
            "failure_stage": "composer_not_ready",
            "next_action": "agent_decision_required",
            "safe_to_retry_same_request": True,
            "diagnostic_path": _write_not_ready_diagnostic(request, exc),
            "composer_snapshot": exc.snapshot,
        }
        receipt(request, "submission_not_started", detail, **values)
        event(request, "submission_not_started", phase="submit", detail=detail, **values)
        emit("submission_not_started", ok=False, phase="submit", project_id=request.project_id, request_id=request.request_id, detail=detail, **values)
        return 1
    except PreSubmissionError as exc:
        detail = str(exc)
        values = {
            "failure_stage": exc.failure_stage,
            "next_action": "agent_decision_required",
            "safe_to_retry_same_request": True,
            "diagnostic_path": str(exc.diagnostic_path) if exc.diagnostic_path else None,
        }
        receipt(request, "submission_not_started", detail, **values)
        event(request, "submission_not_started", phase="submit", detail=detail, **values)
        emit("submission_not_started", ok=False, phase="submit", project_id=request.project_id, request_id=request.request_id, detail=detail, **values)
        return 1
    except SubmissionUnconfirmed as exc:
        detail = str(exc)
        diagnostic_path = str(exc.diagnostic_path) if exc.diagnostic_path else None
        values = {"failure_stage": "send_state_uncertain", "next_action": "agent_decision_required", "safe_to_retry_same_request": False, "diagnostic_path": diagnostic_path}
        receipt(request, "submission_unconfirmed", detail, **values)
        event(request, "chat_submission_unconfirmed", phase="submit", detail=detail, **values)
        emit("chat_submission_unconfirmed", ok=False, phase="submit", project_id=request.project_id, request_id=request.request_id, detail=detail, **values)
        return 1
    except ProfileConfigurationError as exc:
        detail = str(exc)
        receipt(request, "configuration_error", detail)
        event(request, "configuration_error", phase="configuration", detail=detail)
        emit("configuration_error", ok=False, phase="configuration", project_id=request.project_id, request_id=request.request_id, detail=detail)
        return 2
    except BrowserError as exc:
        receipt(request, "browser_error", str(exc))
        event(request, "browser_error", phase="browser", detail=str(exc))
        emit("browser_error", ok=False, phase="browser", project_id=request.project_id, request_id=request.request_id, detail=str(exc))
        return 1
    except Exception as exc:
        receipt(request, "courier_error", f"{type(exc).__name__}: {exc}")
        emit("courier_error", ok=False, phase="run", project_id=request.project_id, request_id=request.request_id, detail=f"{type(exc).__name__}: {exc}")
        return 1
    capture_values: dict[str, object] = {}
    body: str | None = None
    if outcome == "response_captured":
        outcome, body, capture_values = _parse_captured_response(request)
    if outcome == "response_received" and body is not None:
        path = save_response(request, body)
        receipt(request, "response_received", "A completed assistant reply was captured and parsed", response_path=str(path), **capture_values)
        event(request, "response_received", phase="complete", response_path=str(path), **capture_values)
        emit("response_received", ok=True, phase="complete", project_id=request.project_id, request_id=request.request_id, response_path=str(path))
        return 0
    detail = ("no completed assistant reply arrived before the workflow deadline"
              if outcome == "response_timeout" else
              str(capture_values.get("protocol_detail", "captured assistant reply did not satisfy the optional envelope protocol")))
    receipt(request, outcome, detail, **capture_values)
    event(request, outcome, phase="receive", detail=detail, **capture_values)
    emit(outcome, ok=False, phase="receive", project_id=request.project_id, request_id=request.request_id, detail=detail)
    return 1


def _wait_for_queue(request, previous: dict | None) -> tuple[CourierQueue | None, int | None]:
    """Join one durable FIFO ticket and wait without touching Chrome."""
    queue = CourierQueue(request)
    try:
        status = queue.join(allow_active_recovery=_submission_confirmed(previous) or _safe_pre_browser_turn_recovery(previous, request))
    except (QueueIntegrityError, RuntimeError, OSError) as exc:
        receipt(request, "configuration_error", str(exc))
        emit("configuration_error", ok=False, phase="queue", project_id=request.project_id, request_id=request.request_id, detail=str(exc))
        return None, 2
    fields = _queue_fields(status)
    if status.state == "duplicate_runner":
        emit("queue_duplicate_runner", ok=True, phase="queue", project_id=request.project_id, request_id=request.request_id, browser_started=False, **fields)
        return None, 0
    if status.state == "recovery_required":
        values = {**fields, "next_action": "agent_decision_required", "safe_to_retry_same_request": False}
        receipt(request, "queue_recovery_required", "a prior active Courier request ended without a safe terminal state", **values)
        event(request, "queue_recovery_required", phase="queue", **values)
        emit("queue_recovery_required", ok=False, phase="queue", project_id=request.project_id, request_id=request.request_id, browser_started=False, **values)
        return None, 1
    values = {**fields, "queue_wait_seconds": request.queue_wait_seconds, "browser_started": False}
    joined_event = "queue_recovery_started" if status.state == "recovery_rejoined" else "queue_joined"
    joined_detail = "Re-acquired a pre-browser queue turn for the same immutable request" if status.state == "recovery_rejoined" else "Waiting for the shared Courier browser"
    receipt(request, "queued", joined_detail, **values)
    event(request, joined_event, phase="queue", **values)
    emit(joined_event, ok=True, phase="queue", project_id=request.project_id, request_id=request.request_id, **values)
    next_notice = 0.0
    while True:
        try:
            status = queue.poll()
        except (QueueIntegrityError, RuntimeError, OSError) as exc:
            receipt(request, "configuration_error", str(exc))
            emit("configuration_error", ok=False, phase="queue", project_id=request.project_id, request_id=request.request_id, detail=str(exc))
            return None, 2
        fields = _queue_fields(status)
        if status.state == "turn_acquired":
            values = {**fields, "execution_started_at": time.time(), "browser_started": False}
            receipt(request, "queue_turn_acquired", "Courier acquired the shared browser turn", **values)
            event(request, "queue_turn_acquired", phase="queue", **values)
            emit("queue_turn_acquired", ok=True, phase="queue", project_id=request.project_id, request_id=request.request_id, **values)
            return queue, None
        if status.state == "timeout":
            values = {**fields, "next_action": "agent_decision_required", "safe_to_retry_same_request": True, "browser_started": False}
            receipt(request, "queue_timeout", "Courier did not reach the browser before queue_wait_seconds elapsed", **values)
            event(request, "queue_timeout", phase="queue", **values)
            emit("queue_timeout", ok=False, phase="queue", project_id=request.project_id, request_id=request.request_id, **values)
            return None, 1
        if status.state == "recovery_required":
            values = {**fields, "next_action": "agent_decision_required", "safe_to_retry_same_request": False, "browser_started": False}
            receipt(request, "queue_recovery_required", "a prior active Courier request requires its original recovery", **values)
            event(request, "queue_recovery_required", phase="queue", **values)
            emit("queue_recovery_required", ok=False, phase="queue", project_id=request.project_id, request_id=request.request_id, **values)
            return None, 1
        if status.state == "duplicate_runner":
            emit("queue_duplicate_runner", ok=True, phase="queue", project_id=request.project_id, request_id=request.request_id, browser_started=False, **fields)
            return None, 0
        if time.monotonic() >= next_notice:
            values = {**fields, "queue_wait_seconds": request.queue_wait_seconds, "browser_started": False}
            receipt(request, "queued", "Waiting for the shared Courier browser", **values)
            event(request, "queue_waiting", phase="queue", **values)
            emit("queue_waiting", ok=True, phase="queue", project_id=request.project_id, request_id=request.request_id, **values)
            next_notice = time.monotonic() + 10
        time.sleep(1)


def run_command(args: argparse.Namespace) -> int:
    try:
        request = load_request(args.request_directory)
        previous = load_receipt(request)
    except ValidationError as exc:
        emit("validation_failed", ok=False, phase="validate", detail=str(exc))
        return 2
    if previous and previous.get("state") == "response_received":
        emit("response_duplicate", ok=True, phase="complete", project_id=request.project_id, request_id=request.request_id, response_path=str(request.directory / "response.txt"))
        return 0
    try:
        queue, terminal = _wait_for_queue(request, previous)
    except KeyboardInterrupt:
        # The request has not crossed the browser boundary. Re-open its own
        # queue identity only to remove the abandoned queued ticket.
        cleanup = CourierQueue(request)
        try:
            cleanup.join(allow_active_recovery=True)
            cleanup.complete()
        except (QueueIntegrityError, RuntimeError, OSError):
            pass
        values = {
            "interruption_stage": "pre_browser",
            "interruption_signal": "SIGINT/CTRL_C",
            "courier_pid": os.getpid(),
            "parent_pid": os.getppid(),
            "browser_started": False,
            "safe_to_retry_same_request": True,
        }
        receipt(request, "courier_interrupted", "Courier received an external interrupt before browser ownership", **values)
        event(request, "courier_interrupted", phase="interrupt", **values)
        emit("courier_interrupted", ok=False, phase="interrupt", project_id=request.project_id, request_id=request.request_id, **values)
        return 130
    if terminal is not None:
        return terminal
    assert queue is not None
    try:
        result = _run_after_queue(request, previous)
    except KeyboardInterrupt:
        try:
            current = load_receipt(request)
        except ValidationError:
            current = previous
        pre_browser = _safe_pre_browser_turn_recovery(current, request)
        values = {
            "interruption_stage": "pre_browser" if pre_browser else "external_interrupt_after_browser_boundary",
            "interruption_signal": "SIGINT/CTRL_C",
            "courier_pid": os.getpid(),
            "parent_pid": os.getppid(),
            "browser_started": False if pre_browser else None,
            "safe_to_retry_same_request": pre_browser,
        }
        event(request, "courier_interrupted", phase="interrupt", **values)
        emit("courier_interrupted", ok=False, phase="interrupt", project_id=request.project_id, request_id=request.request_id, **values)
        if pre_browser:
            receipt(request, "courier_interrupted", "Courier received an external interrupt before browser ownership", **values)
            queue.complete()
        else:
            queue.mark_recovery_required("active run received KeyboardInterrupt after the browser boundary")
        # The receipt/event above are the authoritative interruption result.
        # Do not re-raise: a traceback makes a host-originated Ctrl+C look like
        # an internal Courier crash and prevents simple PowerShell wrappers
        # from reliably printing the final receipt.
        return 130
    except BaseException as exc:
        # Do not unblock another project after an interrupt/crash boundary.
        # A later run of this same immutable request is the only safe recovery.
        queue.mark_recovery_required(f"active run exited unexpectedly: {type(exc).__name__}: {exc}")
        raise
    else:
        queue.complete()
        return result



def capabilities_command(args: argparse.Namespace) -> int:
    emit("courier_capabilities", ok=True, phase="control", **capabilities())
    return 0


def configure_project_command(args: argparse.Namespace) -> int:
    try:
        value = configure_project(args.project_id, args.outbox_root, args.artifact_root,
                                  args.max_attachments, args.max_single_bytes, args.max_total_bytes)
    except ValidationError as exc:
        emit("configuration_error", ok=False, phase="configure_project", detail=str(exc))
        return 2
    emit("courier_project_configured", ok=True, phase="configure_project", **value)
    return 0


def prepare_command(args: argparse.Namespace) -> int:
    try:
        message = Path(args.message_file).read_text(encoding="utf-8-sig")
        value = prepare_request(
            args.project_id, args.idempotency_key, message, args.attachment,
            workflow_window_seconds=args.workflow_window_seconds,
            queue_wait_seconds=args.queue_wait_seconds,
            task_difficulty=args.task_difficulty, instruction_level=args.instruction_level,
        )
    except (OSError, UnicodeDecodeError, ValidationError) as exc:
        emit("courier_prepare_failed", ok=False, phase="prepare", detail=str(exc),
             error_code="COURIER_PREPARE_INVALID", retry_allowed=False,
             safe_next_action="correct_prepare_input")
        return 2
    emit("courier_prepared", ok=True, phase="prepare", **value)
    return 0


def status_command(args: argparse.Namespace) -> int:
    try:
        value = request_status(args.request_directory)
    except ValidationError as exc:
        emit("courier_status_failed", ok=False, phase="status", detail=str(exc))
        return 2
    emit("courier_status", ok=True, phase="status", **value)
    return 0


def wait_command(args: argparse.Namespace) -> int:
    try:
        value = wait_status(args.request_directory, args.timeout)
    except ValidationError as exc:
        emit("courier_wait_failed", ok=False, phase="wait", detail=str(exc))
        return 2
    emit("courier_wait", ok=not value.get("wait_timeout", False), phase="wait", **value)
    return 2 if value.get("wait_timeout") else 0


def recover_command(args: argparse.Namespace) -> int:
    try:
        value = request_status(args.request_directory)
    except ValidationError as exc:
        emit("courier_recover_failed", ok=False, phase="recover", detail=str(exc))
        return 2
    if value["state"] == "response_received":
        emit("response_duplicate", ok=True, phase="complete", **value)
        return 0
    if value["state"] not in RECOVERY_ONLY_STATES:
        emit("courier_recover_failed", ok=False, phase="recover",
             error_code="COURIER_RECOVERY_NOT_ALLOWED", retry_allowed=False,
             safe_next_action="courier_status", **value)
        return 2
    return run_command(args)

def _profile_for_request(request) -> str:
    """Report the same deterministic profile selection used by ChatSession."""
    import os
    from pathlib import Path
    from .model import runtime_root
    configured = os.environ.get("CHAT_COURIER_PROFILE") or os.environ.get("AGENT_RELAY_CHATGPT_PROFILE")
    legacy = Path(os.environ.get("LOCALAPPDATA", "")) / "CodexOrchestrator" / "profiles" / "chatgpt"
    return str(Path(configured) if configured else (legacy if legacy.exists() else runtime_root() / "profile"))


def main(argv: list[str] | None = None) -> int:
    expected_source = os.environ.get("CHAT_COURIER_EXPECTED_SOURCE_ROOT")
    if expected_source:
        try:
            expected_path = Path(expected_source).resolve()
        except OSError:
            expected_path = None
        if expected_path != COURIER_SOURCE_ROOT:
            emit(
                "configuration_error", ok=False, phase="startup",
                detail=("Courier source root does not match the launcher expectation; "
                        f"expected={expected_source!r}; actual={str(COURIER_SOURCE_ROOT)!r}"),
            )
            return 2
    parser = argparse.ArgumentParser(prog="chat-courier", description="Bounded local ChatGPT request/response transport")
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate", help="validate a request directory without Chrome or network")
    validate.add_argument("request_directory"); validate.set_defaults(handler=validate_command)
    preflight = sub.add_parser("preflight", help="verify the dedicated ChatGPT profile and conversation without sending")
    preflight.add_argument("request_directory"); preflight.set_defaults(handler=preflight_command)
    run = sub.add_parser("run", help="send a request and receive one matching ChatGPT reply")
    run.add_argument("request_directory"); run.set_defaults(handler=run_command)
    register = sub.add_parser("register", help="propose a fixed ChatGPT conversation URL; does not change the active registration")
    register.add_argument("--project-id", required=True); register.add_argument("--url", required=True); register.set_defaults(handler=register_command)
    confirm = sub.add_parser("confirm-register", help="explicitly confirm a pending ChatGPT URL registration")
    confirm.add_argument("--project-id", required=True); confirm.add_argument("--confirmation-id", required=True)
    confirm.add_argument("--basis", required=True, choices=["user_direct", "prior_authorization"]); confirm.set_defaults(handler=confirm_register_command)
    typed_capabilities = sub.add_parser("courier_capabilities", help="show typed Courier capabilities")
    typed_capabilities.set_defaults(handler=capabilities_command)
    configure = sub.add_parser("configure-project", help="administratively bind one project policy")
    configure.add_argument("--project-id", required=True); configure.add_argument("--outbox-root", required=True)
    configure.add_argument("--artifact-root", action="append", required=True)
    configure.add_argument("--max-attachments", type=int, required=True)
    configure.add_argument("--max-single-bytes", type=int, required=True)
    configure.add_argument("--max-total-bytes", type=int, required=True)
    configure.set_defaults(handler=configure_project_command)
    prepare = sub.add_parser("courier_prepare", help="idempotently construct an immutable request")
    prepare.add_argument("--project-id", required=True); prepare.add_argument("--idempotency-key", required=True)
    prepare.add_argument("--message-file", required=True); prepare.add_argument("--attachment", action="append", default=[])
    prepare.add_argument("--workflow-window-seconds", type=int, default=600)
    prepare.add_argument("--queue-wait-seconds", type=int, default=3600)
    prepare.add_argument("--task-difficulty", choices=["normal", "hard", "challenge"], default="normal")
    prepare.add_argument("--instruction-level", choices=["normal", "detailed", "manual_book"], default="normal")
    prepare.set_defaults(handler=prepare_command)
    dispatch = sub.add_parser("courier_dispatch", help="dispatch or automatically resume one request")
    dispatch.add_argument("request_directory"); dispatch.set_defaults(handler=run_command)
    typed_status = sub.add_parser("courier_status", help="read one request state")
    typed_status.add_argument("request_directory"); typed_status.set_defaults(handler=status_command)
    typed_wait = sub.add_parser("courier_wait", help="wait without dispatching")
    typed_wait.add_argument("request_directory"); typed_wait.add_argument("--timeout", type=int, default=30)
    typed_wait.set_defaults(handler=wait_command)
    typed_recover = sub.add_parser("courier_recover", help="recover an already submitted request")
    typed_recover.add_argument("request_directory"); typed_recover.set_defaults(handler=recover_command)
    args = parser.parse_args(argv)
    return args.handler(args)

if __name__ == "__main__": raise SystemExit(main())
