from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

from .browser import BrowserError, ChatAccessDenied, ChatAuthenticationRequired, ChatComposerNotReady, ChatConversationMismatch, ChatDom, ChatRateLimited, ChatSession, PreSubmissionError, ProfileConfigurationError, SubmissionUnconfirmed
from .owner import OwnerBusy, process_alive, read_owner
from .model import ACTIVE_SETUP_BUDGET_SECONDS, CALLER_GRACE_SECONDS, ValidationError, atomic_json, commit_conversation_rollover, confirm_url_registration, load_request, minimum_caller_window_seconds, propose_url_registration, runtime_root, same_chat_project
from .protocol import REPLY_PROTOCOL, build_prompt, is_chat_ui_error, is_conversation_exhausted, parse_reply
from .queue import CourierQueue, QueueIntegrityError, QueueStatus
from .storage import (archive_response_capture, archive_target_generation, ensure_target_binding, event, load_receipt, load_response_capture,
                      evidence_retry_count, load_latest_probe, load_response_cursor, receipt,
                      request_events, request_was_submitted,
                      record_absence_observation,
                      save_latest_response_capture, save_response, save_response_capture,
                      save_latest_probe, save_response_cursor, submission_count)
from .workflow import (
    RECOVERY_ONLY_STATES, capabilities, configure_project, prepare_request,
    request_status, wait_status,
)


COURIER_SOURCE_ROOT = Path(__file__).resolve().parent.parent
CHAT_CONTENTION_WAIT_SECONDS = 600
CHAT_CONTENTION_POLL_SECONDS = 10
CHAT_CONTENTION_RECONNECT_ATTEMPTS = CHAT_CONTENTION_WAIT_SECONDS // CHAT_CONTENTION_POLL_SECONDS
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
    # The launcher can inherit a legacy Windows code page even though Courier
    # payloads and files are UTF-8. JSON escaping keeps diagnostic output
    # lossless and prevents a non-ASCII page title from aborting transport.
    print(json.dumps({"event": name, "ok": ok, "courier_source_root": str(COURIER_SOURCE_ROOT), "courier_build_id": COURIER_BUILD_ID, **values}, ensure_ascii=True, sort_keys=True), flush=True)


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
        ensure_target_binding(request)
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


def quiescence_command(_args: argparse.Namespace) -> int:
    """Read-only proof that no queue entry or browser owner remains."""
    queue_path = runtime_root() / "queue.json"
    try:
        if queue_path.exists():
            raw = json.loads(queue_path.read_text(encoding="utf-8"))
            if (not isinstance(raw, dict) or raw.get("version") != 1
                    or not isinstance(raw.get("entries"), list)):
                raise ValueError("invalid queue schema")
            entries = raw["entries"]
        else:
            entries = []
        owner = read_owner()
    except (OSError, ValueError, json.JSONDecodeError, OwnerBusy) as exc:
        emit("courier_quiescence", ok=False, phase="quiescence",
             quiescent=False, detail=str(exc))
        return 2

    queue_entries = [
        {
            "project_id": entry.get("project_id"),
            "request_id": entry.get("request_id"),
            "state": entry.get("state"),
            "process_live": process_alive(int(entry.get("pid", 0))),
        }
        for entry in entries if isinstance(entry, dict)
    ]
    owner_summary = None
    owner_live = browser_live = False
    if owner is not None:
        owner_live = process_alive(owner.owner_pid)
        browser_live = bool(owner.browser_pid and process_alive(owner.browser_pid))
        owner_summary = {
            "project_id": owner.project_id,
            "request_id": owner.request_id,
            "phase": owner.phase,
            "owner_live": owner_live,
            "browser_live": browser_live,
        }
    quiescent = not queue_entries and owner is None
    emit("courier_quiescence", ok=quiescent, phase="quiescence",
         quiescent=quiescent, queue_entries=queue_entries,
         owner=owner_summary, owner_live=owner_live, browser_live=browser_live)
    return 0 if quiescent else 1


def _capture_response(session: ChatSession, request, baseline: set[str] | None, deadline: float,
                      *, legacy_recovery: bool = False) -> str:
    """Capture one completed assistant turn before the browser is closed."""
    options: dict[str, object] = {"after_latest_user": legacy_recovery}
    if legacy_recovery:
        options["after_user_marker"] = f"REQUEST_ID={request.request_id}"
    candidate = session.wait_for_reply(baseline, deadline, **options)
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
    if is_chat_ui_error(text):
        return "response_ui_error", None, {**values, "protocol_detail": "Chat returned a UI error instead of a reply"}
    try:
        reply = parse_reply(text, request)
    except ValidationError as exc:
        return "response_protocol_error", None, {**values, "protocol_detail": str(exc)}
    return "response_received", reply.body, values


def _upload_status(request, name: str, **values) -> None:
    event(request, name, phase="upload", **values)
    failed = {"attachment_upload_failed", "attachment_upload_stalled", "browser_page_unresponsive", "page_closed_during_upload"}
    emit(name, ok=name not in failed, phase="upload", project_id=request.project_id, request_id=request.request_id, **values)


def _run_session_once(request, submitted: bool, reply_window_seconds: int, *, resend_once: bool = False) -> str:
    emit("browser_launch_requested", ok=True, phase="browser", project_id=request.project_id, request_id=request.request_id)
    event(request, "browser_launch_requested", phase="browser")
    with ChatSession(request, recovery=submitted, status_callback=lambda name, **values: _upload_status(request, name, **values)) as session:
        event(request, "browser_started", phase="browser", profile=str(session.profile), attached_existing=session.attached_existing)
        emit("browser_started", ok=True, phase="browser", project_id=request.project_id, request_id=request.request_id, attached_existing=session.attached_existing)
        if submitted and not resend_once:
            baseline = load_response_cursor(request)
            legacy_recovery = baseline is None
            receipt(request, "waiting_for_response", "Resuming read-only search for an already submitted request", attached_existing=session.attached_existing)
            emit("response_waiting", ok=True, phase="receive", project_id=request.project_id, request_id=request.request_id, resumed=True, attached_existing=session.attached_existing)
            deadline = time.monotonic() + reply_window_seconds
        else:
            baseline = session.submit(build_prompt(request), request.attachments)
            legacy_recovery = False
            attempt = submission_count(request) + 1
            receipt(request, "request_submitted", "ChatGPT user turn was visibly confirmed",
                    submission_attempt=attempt,
                    owner_pid=session.owner.record.owner_pid if session.owner.record else None,
                    owner_nonce=session.owner.record.owner_nonce if session.owner.record else None)
            event(request, "request_submitted", phase="submit", submission_attempt=attempt)
            emit("request_submitted", ok=True, phase="submit", project_id=request.project_id,
                 request_id=request.request_id, submission_attempt=attempt)
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
        "response_ui_error",
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
            if previous.get("state") != "submission_intent":
                return True
            # The intent record is deliberately written before browser launch.
            # If the process dies in that narrow gap, events prove no browser
            # side effect was even attempted and the same request may resume.
            events = request_events(request)
            last_intent = max((index for index, item in enumerate(events)
                               if item.get("event") == "submission_intent_written"),
                              default=-1)
            later = events[last_intent + 1:]
            return not any(item.get("event") in {
                "browser_launch_requested", "browser_started", "request_submitted",
                "chat_submission_unconfirmed",
            } for item in later)
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


def _chat_contention_snapshot(exc: Exception) -> dict[str, object] | None:
    """Return a sample only for positive streaming or composer-focus contention."""
    if isinstance(exc, ChatComposerNotReady):
        snapshots = [exc.snapshot]
    elif isinstance(exc, PreSubmissionError) and exc.failure_stage == "composer_not_ready":
        snapshots = list(reversed(exc.timeline))
    else:
        return None
    for snapshot in snapshots:
        if not isinstance(snapshot, dict):
            continue
        if snapshot.get("rate_limited") is True:
            return snapshot
        if snapshot.get("streaming") is True:
            return snapshot
        if (
            snapshot.get("visible") is True
            and snapshot.get("enabled") is True
            and snapshot.get("editable") is True
            and snapshot.get("streaming") is False
            and snapshot.get("focused") is False
            and snapshot.get("ready") is False
        ):
            return snapshot
    return None


def _wait_for_shared_chat(request, exc: Exception, attempt: int = 1) -> None:
    snapshot = _chat_contention_snapshot(exc) or {}
    rate_limited = snapshot.get("rate_limited") is True
    contention_reason = (
        "chat_rate_limited" if rate_limited else
        "shared_chat_streaming" if snapshot.get("streaming") is True else
        "shared_chat_focus_contended"
    )
    wait_seconds = CHAT_CONTENTION_WAIT_SECONDS if rate_limited else CHAT_CONTENTION_POLL_SECONDS
    wait_started_at = time.time()
    values = {
        "contention_reason": contention_reason,
        "wait_seconds": wait_seconds,
        "wait_started_at": wait_started_at,
        "wait_deadline_at": wait_started_at + wait_seconds,
        "next_check_at": wait_started_at + wait_seconds,
        "runner_pid": os.getpid(),
        "reconnect_attempt": attempt,
        "maximum_reconnect_attempts": 1 if rate_limited else CHAT_CONTENTION_RECONNECT_ATTEMPTS,
        "agent_action_required": False,
        "safe_next_action": "wait_for_same_request",
        "same_request_preserved": True,
        "composer_snapshot": snapshot,
    }
    if rate_limited:
        detail = (
            "ChatGPT explicitly reports a temporary rate or usage limit. Courier will cool down "
            f"for {CHAT_CONTENTION_WAIT_SECONDS // 60} minutes before one reconnect attempt; "
            "the Agent must wait and must not retry, replace, recover, or escalate this request."
        )
    else:
        detail = (
            "The registered Chat is currently generating another turn. Courier will "
            f"check again in {CHAT_CONTENTION_POLL_SECONDS} seconds, for at most {CHAT_CONTENTION_WAIT_SECONDS // 60} minutes; "
            "the Agent must not retry, replace, or escalate this request while Courier is waiting."
        )
    receipt(request, "chat_busy_waiting", detail, **values)
    event(request, "chat_busy_waiting", phase="contention_wait", detail=detail, **values)
    emit(
        "chat_busy_waiting", ok=True, phase="contention_wait",
        project_id=request.project_id, request_id=request.request_id,
        detail=detail, **values,
    )
    time.sleep(wait_seconds)
    reconnect_values = {
        "contention_reason": contention_reason,
        "runner_pid": os.getpid(),
        "reconnect_attempt": attempt,
        "maximum_reconnect_attempts": 1 if rate_limited else CHAT_CONTENTION_RECONNECT_ATTEMPTS,
        "agent_action_required": False,
        "same_request_preserved": True,
        "rate_limited": rate_limited,
    }
    receipt(
        request, "chat_busy_reconnecting",
        "The bounded contention wait completed; Courier is reconnecting once with the same immutable request.",
        **reconnect_values,
    )
    event(request, "chat_busy_reconnecting", phase="contention_wait", **reconnect_values)
    emit(
        "chat_busy_reconnecting", ok=True, phase="contention_wait",
        project_id=request.project_id, request_id=request.request_id,
        **reconnect_values,
    )


def _run_after_queue(request, previous: dict | None, *, resend_once: bool = False) -> int:
    event(request, "request_validated", phase="validate")
    emit("request_validated", ok=True, phase="validate", project_id=request.project_id, request_id=request.request_id, workflow_window_seconds=request.workflow_window_seconds, workflow_window_scope="post_submission_response", queue_wait_seconds=request.queue_wait_seconds, active_setup_budget_seconds=ACTIVE_SETUP_BUDGET_SECONDS, minimum_caller_window_seconds=minimum_caller_window_seconds(request.queue_wait_seconds, request.workflow_window_seconds))
    deadline = time.monotonic() + request.workflow_window_seconds
    submitted = _submission_confirmed(previous) or request_was_submitted(request)
    if not submitted:
        emit("submission_intent_writing", ok=True, phase="submit", project_id=request.project_id, request_id=request.request_id, browser_started=False)
        event(request, "submission_intent_writing", phase="submit")
        receipt(request, "submission_intent", "Courier is about to submit; do not automatically resend if interrupted")
        emit("submission_intent_written", ok=True, phase="submit", project_id=request.project_id, request_id=request.request_id, browser_started=False)
        event(request, "submission_intent_written", phase="submit")
    try:
        if load_response_capture(request) is None:
            contention_reconnects = 0
            rate_limit_reconnects = 0
            while True:
                try:
                    outcome = _run_session_once(request, submitted, request.workflow_window_seconds,
                                                resend_once=resend_once)
                    break
                except (ChatComposerNotReady, PreSubmissionError) as exc:
                    snapshot = _chat_contention_snapshot(exc)
                    rate_limited = bool(snapshot and snapshot.get("rate_limited") is True)
                    if (snapshot is None or (
                            not rate_limited
                            and contention_reconnects >= CHAT_CONTENTION_RECONNECT_ATTEMPTS)):
                        raise
                    if rate_limited:
                        # The advertised rate-limit policy is one ten-minute
                        # cooldown followed by one reconnect, not an unbounded
                        # series of fresh cooldowns whenever the page flips
                        # between a limit card and another non-ready state.
                        if rate_limit_reconnects >= 1:
                            raise
                        rate_limit_reconnects += 1
                    else:
                        contention_reconnects += 1
                    _wait_for_shared_chat(
                        request, exc,
                        1 if rate_limited else contention_reconnects,
                    )
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


def _wait_for_queue(request, previous: dict | None, *, evidence_retry: bool = False) -> tuple[CourierQueue | None, int | None]:
    """Join one durable FIFO ticket and wait without touching Chrome."""
    queue = CourierQueue(request)
    try:
        status = queue.join(allow_active_recovery=(
            evidence_retry
            or _submission_confirmed(previous)
            or request_was_submitted(request)
            or _safe_pre_browser_turn_recovery(previous, request)
        ))
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
            blocked_directory = (status.current_owner or {}).get("request_directory")
            if isinstance(blocked_directory, str) and blocked_directory:
                event(request, "queue_head_reconcile_started", phase="queue",
                      blocked_request_directory=blocked_directory)
                reconcile_command(argparse.Namespace(request_directory=blocked_directory))
                continue
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
        ensure_target_binding(request)
        if bool(getattr(args, "use_retry_message", False)) and request.retry_message:
            request = replace(request, message=request.retry_message)
        previous = load_receipt(request)
    except ValidationError as exc:
        emit("validation_failed", ok=False, phase="validate", detail=str(exc))
        return 2
    if (previous and previous.get("state") == "response_received"
            and not bool(getattr(args, "resend_once", False))):
        emit("response_duplicate", ok=True, phase="complete", project_id=request.project_id, request_id=request.request_id, response_path=str(request.directory / "response.txt"))
        return 0
    try:
        queue, terminal = _wait_for_queue(
            request, previous, evidence_retry=bool(getattr(args, "evidence_retry", False))
        )
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
        result = _run_after_queue(request, previous,
                                  resend_once=bool(getattr(args, "resend_once", False)))
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
    return reconcile_command(args)


def resend_once_command(args: argparse.Namespace) -> int:
    """Resend the same immutable request once after bounded read-only recovery."""
    try:
        request = load_request(args.request_directory)
        count = submission_count(request)
        events = request_events(request)
        # `courier_resend_once` is the explicit Supervisor recovery command.
        # Do not require an earlier ordinary evidence-retry event as a second
        # authorization token: that creates a circular deadlock when the
        # original Send was unconfirmed and the ordinary retry correctly
        # refused the uncertain state.  Fresh exclusive absence evidence and
        # the one-shot Supervisor event are the complete mechanical gate.
        supervisor_zero_submission = count == 0
        if supervisor_zero_submission:
            probe = load_latest_probe(request)
            previous = load_receipt(request)
            owner = read_owner()
            owner_live = bool(owner and (process_alive(owner.owner_pid)
                                        or (owner.browser_pid and process_alive(owner.browser_pid))))
            if (time.time() - float(probe.get("captured_at", 0)) > 300
                    or probe.get("fingerprint") != request.fingerprint
                    or probe.get("latest_user_turn_found")
                    or probe.get("post_submission_reply_found")
                    or probe.get("live_owner_found") or owner_live
                    or any(value.get("event") == "supervisor_retry_authorized" for value in events)):
                raise ValidationError("Supervisor zero-submission retry lacks fresh exclusive evidence")
            if previous is None or previous.get("state") not in {
                    "submission_unconfirmed", "submission_not_started", "submission_intent",
                    "queue_recovery_required", "chat_auth_required"}:
                raise ValidationError("Supervisor zero-submission retry is not at a recoverable send boundary")
            event(request, "supervisor_retry_authorized", phase="resend",
                  probe_captured_at=probe["captured_at"], probe_fingerprint=probe["fingerprint"])
        elif count != 1:
            emit("courier_resend_refused", ok=False, phase="resend",
                 error_code="COURIER_RESEND_LIMIT_OR_STATE", retry_allowed=False,
                 project_id=request.project_id, request_id=request.request_id,
                 submission_count=count)
            return 2
        if count == 1:
            archive_response_capture(request, 1)
    except ValidationError as exc:
        emit("courier_resend_refused", ok=False, phase="resend", detail=str(exc))
        return 2
    args.resend_once = True
    args.evidence_retry = supervisor_zero_submission
    args.use_retry_message = bool(request.retry_message)
    return run_command(args)


def retry_once_command(args: argparse.Namespace) -> int:
    """Retry one apparently unsent immutable request after a fresh read-only probe."""
    try:
        request = load_request(args.request_directory)
        ensure_target_binding(request)
        probe = load_latest_probe(request)
        events = request_events(request)
        owner = read_owner()
        owner_live = bool(owner and (process_alive(owner.owner_pid)
                                    or (owner.browser_pid and process_alive(owner.browser_pid))))
        forbidden = {"request_submitted", "chat_submission_unconfirmed", "submission_unconfirmed"}
        # A pre-submit authentication failure is recoverable after the user
        # signs in.  It has the same safety proof as queue recovery: a fresh
        # read-only probe must show no matching user turn, no reply, no live
        # owner, and the durable submission count must still be zero.
        recoverable_unsent_states = {
            "queue_recovery_required", "chat_auth_required", "submission_not_started",
        }
        if time.time() - float(probe.get("captured_at", 0)) > 300:
            raise ValidationError("latest-response probe is stale")
        if probe.get("fingerprint") != request.fingerprint:
            raise ValidationError("request fingerprint changed after the read-only probe")
        if probe.get("latest_user_turn_found") or probe.get("post_submission_reply_found"):
            raise ValidationError("Chat already contains this request or its reply")
        if probe.get("live_owner_found") or owner_live:
            raise ValidationError("a live Courier or browser owner still exists")
        try:
            previous = load_receipt(request)
            if (previous is not None and previous.get("target_url") is not None
                    and previous.get("target_url") != request.chat_url):
                raise ValidationError("receipt belongs to the prior registered target")
        except ValidationError as original:
            intent_path = request.directory / "target-rollover.json"
            try:
                intent = json.loads(intent_path.read_text(encoding="utf-8"))
                old_receipt = json.loads((request.directory / "receipt.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise original from exc
            if (not isinstance(intent, dict) or intent.get("version") != 1
                    or intent.get("basis") != "user_direct" or intent.get("phase") != "prepared"
                    or intent.get("project_id") != request.project_id
                    or intent.get("request_id") != request.request_id
                    or old_receipt.get("fingerprint") != intent.get("source_fingerprint")
                    or request.chat_url == intent.get("source_url")
                    or not same_chat_project(intent.get("source_url"), request.chat_url)
                    or submission_count(request, total=True) != intent.get("prior_total_submission_count")):
                raise original
            archive = request.directory / str(intent.get("archive_directory", ""))
            if not archive.is_dir():
                raise ValidationError("rollover archive is missing")
            cursor = request.directory / "response-cursor.json"
            if cursor.exists():
                os.replace(cursor, archive / "unresolved-successor-response-cursor.json")
            os.replace(intent_path, archive / "target-rollover-prepared.json")
            event(request, "target_rollover_authorized", phase="target_rollover",
                  prior_total_submission_count=intent["prior_total_submission_count"],
                  prior_fingerprint=intent["source_fingerprint"],
                  active_fingerprint=request.fingerprint,
                  source_url=intent["source_url"], successor_url=request.chat_url,
                  archive_directory=archive.name, basis="user_direct",
                  manual_successor=True, unresolved_confirmed_successor=True)
            receipt(request, "queue_recovery_required",
                    "User-confirmed same-Project successor is empty and ready for one evidence-bound retry",
                    successor_url=request.chat_url)
            previous = load_receipt(request)
            events = request_events(request)
        interrupted_before_intent = (
            any(value.get("event") == "submission_intent_writing" for value in events)
            and not any(value.get("event") in {
                "submission_intent_written", "request_submitted",
                "chat_submission_unconfirmed", "submission_unconfirmed",
            } for value in events)
        )
        if (previous is None or previous.get("state") not in recoverable_unsent_states) \
                and not interrupted_before_intent:
            raise ValidationError(
                "evidence retry requires queue recovery or a resolved pre-submit authentication failure"
            )
        last_rollover = max(
            (index for index, value in enumerate(events)
             if value.get("event") == "target_rollover_authorized"),
            default=-1,
        )
        current_events = events[last_rollover + 1:]
        if submission_count(request) or any(value.get("event") in forbidden for value in current_events):
            raise ValidationError("submission evidence forbids an evidence retry")
        if (request.directory / "response.txt").exists():
            raise ValidationError("a saved response forbids an evidence retry")
        if evidence_retry_count(request):
            raise ValidationError("the evidence retry budget is exhausted")
    except (OSError, TypeError, ValueError, ValidationError, OwnerBusy) as exc:
        emit("courier_retry_refused", ok=False, phase="retry", detail=str(exc),
             error_code="COURIER_EVIDENCE_RETRY_REFUSED", retry_allowed=False)
        return 2
    event(request, "evidence_retry_authorized", phase="retry",
          probe_captured_at=probe["captured_at"], probe_fingerprint=probe["fingerprint"])
    emit("courier_evidence_retry_authorized", ok=True, phase="retry",
         project_id=request.project_id, request_id=request.request_id,
         probe_captured_at=probe["captured_at"], submission_count=0)
    args.evidence_retry = True
    args.resend_once = False
    return run_command(args)


def rollover_target_command(args: argparse.Namespace) -> int:
    """Create one same-Project successor after Chat proves the source is exhausted."""
    root = Path(args.request_directory).resolve()
    intent_path = root / "target-rollover.json"
    basis = getattr(args, "basis", "verified_context_capacity")
    user_direct = basis == "user_direct"
    prepared_retry_authorized = bool(getattr(args, "prepared_retry_authorized", False))
    try:
        if intent_path.exists():
            intent = json.loads(intent_path.read_text(encoding="utf-8"))
            if not isinstance(intent, dict) or intent.get("version") != 1:
                raise ValidationError("invalid target-rollover.json")
            target_url = intent.get("successor_url")
            if not isinstance(target_url, str):
                request = load_request(root)
                previous = load_receipt(request)
                queue, terminal = _wait_for_queue(request, previous)
                if terminal is not None:
                    return terminal
                assert queue is not None
                try:
                    with ChatSession(request, inspect_project=True) as session:
                        retry_sent = False
                        try:
                            target_url = session.recover_successor_url(request.request_id)
                        except BrowserError:
                            if not prepared_retry_authorized:
                                raise
                            if any(
                                value.get("event") == "target_rollover_prepared_retry_authorized"
                                for value in request_events(request)
                            ):
                                raise ValidationError(
                                    "prepared rollover retry budget is exhausted"
                                )
                            diagnostic_path = root / "rollover-recovery-diagnostic.json"
                            try:
                                diagnostic = json.loads(diagnostic_path.read_text(encoding="utf-8"))
                            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                                raise ValidationError(
                                    "prepared rollover retry is missing its read-only absence diagnostic"
                                ) from exc
                            if (not isinstance(diagnostic, dict)
                                    or diagnostic.get("match_count") != 0
                                    or diagnostic.get("authentication_required") is not False
                                    or diagnostic.get("access_denied") is not False
                                    or diagnostic.get("rate_limited") is not False
                                    or not same_chat_project(intent["source_url"],
                                                             str(diagnostic.get("page_url", "")))):
                                raise ValidationError(
                                    "prepared rollover retry lacks a clean same-Project absence observation"
                                )
                            event(request, "target_rollover_prepared_retry_authorized",
                                  phase="target_rollover", basis=intent.get("basis"),
                                  duplicate_risk_accepted=True,
                                  diagnostic_path=str(diagnostic_path))
                            session.prepare_successor_project_chat()
                            recovery_prompt = (
                                "CHAT_COURIER_ROLLOVER_RECOVERY_NOTICE/1\n"
                                f"PROJECT_ID={request.project_id}\n"
                                f"REQUEST_ID={request.request_id}\n"
                                "The prior successor creation was interrupted before its URL was proven. "
                                "This is an explicitly authorized retry of the same logical request, not a new "
                                "work order. If an equivalent turn is already visible, treat this copy as the "
                                "authoritative continuation and do not duplicate project work.\n\n"
                                + build_prompt(request)
                            )
                            baseline = session.submit(recovery_prompt, request.attachments)
                            target_url = session.wait_for_successor_url()
                            retry_sent = True
                finally:
                    queue.complete()
                if not retry_sent:
                    baseline = load_response_cursor(request)
                    if baseline is None:
                        raise ValidationError("rollover recovery is missing its response cursor")
                intent = {
                    **intent,
                    "phase": "submitted",
                    "successor_url": target_url,
                    "assistant_identities": sorted(baseline),
                    "prepared_retry_authorized": retry_sent,
                }
                atomic_json(intent_path, intent)
            request = load_request(root)
            response_path = root / "response.txt"
            intent_basis = intent.get("basis", "verified_context_capacity")
            exhausted_again = (intent_basis == "verified_context_capacity"
                               and request.chat_url == target_url and response_path.is_file()
                               and is_conversation_exhausted(
                                   response_path.read_text(encoding="utf-8-sig")))
            if exhausted_again:
                prior_archive = root / str(intent.get("archive_directory", ""))
                if not prior_archive.is_dir():
                    raise ValidationError("prior rollover archive is missing")
                os.replace(intent_path, prior_archive / "target-rollover.json")
            else:
                commit_conversation_rollover(
                    intent["project_id"], intent["source_url"], target_url,
                    basis=intent_basis,
                )
                request = load_request(root)
                baseline = intent.get("assistant_identities")
                if not isinstance(baseline, list) or not all(isinstance(item, str) for item in baseline):
                    raise ValidationError("rollover recovery is missing its response cursor")
                save_response_cursor(request, set(baseline))
                rollover_recorded = any(
                    value.get("event") == "target_rollover_authorized"
                    and value.get("successor_url") == target_url
                    for value in request_events(request)
                )
                if not rollover_recorded:
                    event(request, "target_rollover_authorized", phase="target_rollover",
                          source_url=intent["source_url"], successor_url=target_url,
                          archive_directory=intent.get("archive_directory"),
                          basis=intent_basis)
                    event(request, "request_submitted", phase="submit", submission_attempt=1,
                          target_rollover=True)
                    receipt(request, "waiting_for_response",
                            "Recovering the confirmed first turn in a same-Project successor chat")
                return run_command(args)

        request = load_request(args.request_directory)
        response_path = request.directory / "response.txt"
        if user_direct:
            events = request_events(request)
            prior_count = submission_count(request, total=True)
            prior = load_receipt(request)
            pristine = prior is None and not events and prior_count == 0
            proven_unsent = (
                prior_count == 0
                and any(
                    value.get("event") == "submission_not_started"
                    and value.get("safe_to_retry_same_request") is True
                    for value in events
                )
                and not any(value.get("event") in {
                    "request_submitted", "chat_submission_unconfirmed",
                    "response_received",
                } for value in events)
            )
            # A failed Enter/send action is still mechanically proven unsent
            # when the immutable marker remains in the composer and no user
            # turn contains it.  With explicit user-direct rollover authority,
            # this should move to a fresh same-Project chat instead of becoming
            # an irreversible dead end.
            try:
                submission_diagnostic = json.loads(
                    (request.directory / "submission_diagnostic.json").read_text(
                        encoding="utf-8"
                    )
                )
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                submission_diagnostic = {}
            proven_unconfirmed_unsent = (
                prior_count == 0
                and any(value.get("event") == "chat_submission_unconfirmed"
                        for value in events)
                and not any(value.get("event") in {
                    "request_submitted", "response_received",
                } for value in events)
                and isinstance(submission_diagnostic, dict)
                and submission_diagnostic.get("request_id") == request.request_id
                and submission_diagnostic.get("marker") == f"REQUEST_ID={request.request_id}"
                and submission_diagnostic.get("composer_contains_marker") is True
                and submission_diagnostic.get("user_turns_with_marker") == []
                and submission_diagnostic.get("page_url") == request.chat_url
            )
            last_confirmed_submission = max(
                (index for index, value in enumerate(events)
                 if value.get("event") in {
                     "request_submitted", "interrupted_reply_recovery_resend_submitted",
                 }),
                default=-1,
            )
            last_submission_uncertainty = max(
                (index for index, value in enumerate(events)
                 if value.get("event") in {
                     "chat_submission_unconfirmed",
                     "interrupted_reply_recovery_submission_unconfirmed",
                 }),
                default=-1,
            )
            last_response_received = max(
                (index for index, value in enumerate(events)
                 if value.get("event") == "response_received"),
                default=-1,
            )
            last_ui_error_reclassified = max(
                (index for index, value in enumerate(events)
                 if value.get("event") == "accepted_ui_error_reclassified"),
                default=-1,
            )
            confirmed_pending = (
                prior_count >= 1 and prior is not None
                and prior.get("state") in {
                    "waiting_for_response", "response_timeout", "queue_recovery_required",
                    "chat_busy_waiting", "chat_busy_reconnecting",
                }
                and last_confirmed_submission > last_submission_uncertainty
                and last_ui_error_reclassified >= last_response_received
            )
            if response_path.exists() or not (
                    pristine or proven_unsent or proven_unconfirmed_unsent
                    or confirmed_pending):
                raise ValidationError(
                    "user-direct rollover requires a fresh request, a proven-unsent request, "
                    "or one confirmed pending submission"
                )
        else:
            if not response_path.is_file() or not is_conversation_exhausted(
                response_path.read_text(encoding="utf-8-sig")
            ):
                raise ValidationError("the prior target is not proven conversation-exhausted")
            prior = json.loads((request.directory / "receipt.json").read_text(encoding="utf-8"))
            if (not isinstance(prior, dict) or prior.get("project_id") != request.project_id
                    or prior.get("request_id") != request.request_id
                    or prior.get("fingerprint") != request.fingerprint
                    or prior.get("state") != "response_received"):
                raise ValidationError("the exhausted response is not bound to the active target")
            prior_count = submission_count(request, total=True)
        source_url = request.chat_url
        queue, terminal = _wait_for_queue(request, prior)
        if terminal is not None:
            return terminal
        assert queue is not None
        try:
            with ChatSession(
                request, inspect_project=True,
                status_callback=lambda name, **values: _upload_status(request, name, **values),
            ) as session:
                session.prepare_successor_project_chat()
                archive = archive_target_generation(request) if prior is not None else None
                if archive is not None:
                    # _wait_for_queue wrote an active queue receipt. Preserve the
                    # original terminal receipt as the archived generation proof.
                    atomic_json(archive / "receipt.json", prior)
                atomic_json(intent_path, {
                    "version": 1, "phase": "prepared", "project_id": request.project_id,
                    "request_id": request.request_id, "source_url": source_url,
                    "source_fingerprint": request.fingerprint,
                    "archive_directory": archive.name if archive is not None else None,
                    "basis": basis,
                    "prior_total_submission_count": prior_count,
                })
                baseline = session.submit(build_prompt(request), request.attachments)
                successor_url = session.wait_for_successor_url()
                atomic_json(intent_path, {
                    "version": 1, "phase": "submitted", "project_id": request.project_id,
                    "request_id": request.request_id, "source_url": source_url,
                    "source_fingerprint": request.fingerprint,
                    "successor_url": successor_url,
                    "archive_directory": archive.name if archive is not None else None,
                    "basis": basis,
                    "prior_total_submission_count": prior_count,
                    "assistant_identities": sorted(baseline),
                })
                commit_conversation_rollover(
                    request.project_id, source_url, successor_url, basis=basis,
                )
                active_request = load_request(args.request_directory)
                save_response_cursor(active_request, baseline)
                event(active_request, "target_rollover_authorized", phase="target_rollover",
                      prior_total_submission_count=prior_count,
                      prior_fingerprint=request.fingerprint,
                      active_fingerprint=active_request.fingerprint,
                      source_url=source_url, successor_url=successor_url,
                      archive_directory=archive.name if archive is not None else None,
                      basis=basis)
                event(active_request, "request_submitted", phase="submit", submission_attempt=1,
                      target_rollover=True)
                receipt(active_request, "waiting_for_response",
                        "Waiting for one completed reply in the same-Project successor chat",
                        successor_url=successor_url)
                outcome = _capture_response(
                    session, active_request, baseline,
                    time.monotonic() + active_request.workflow_window_seconds,
                    legacy_recovery=False,
                )
        finally:
            queue.complete()
        if outcome == "response_captured":
            outcome, body, values = _parse_captured_response(active_request)
        else:
            body, values = None, {}
        if outcome == "response_received" and body is not None:
            path = save_response(active_request, body)
            receipt(active_request, "response_received",
                    "A completed assistant reply was captured from the successor chat",
                    response_path=str(path), successor_url=successor_url, **values)
            event(active_request, "response_received", phase="complete",
                  response_path=str(path), successor_url=successor_url, **values)
            emit("response_received", ok=True, phase="complete",
                 project_id=active_request.project_id, request_id=active_request.request_id,
                 response_path=str(path), successor_url=successor_url)
            return 0
        detail = ("no completed assistant reply arrived before the workflow deadline"
                  if outcome == "response_timeout" else
                  str(values.get("protocol_detail", "successor reply was not usable")))
        receipt(active_request, outcome, detail, successor_url=successor_url, **values)
        event(active_request, outcome, phase="receive", detail=detail,
              successor_url=successor_url, **values)
        emit(outcome, ok=False, phase="receive", project_id=active_request.project_id,
             request_id=active_request.request_id, detail=detail, successor_url=successor_url)
        return 1
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
        emit("courier_target_rollover_refused", ok=False, phase="target_rollover",
             detail=str(exc), error_code="COURIER_TARGET_ROLLOVER_NOT_PROVEN")
        return 2
    except (BrowserError, OwnerBusy) as exc:
        emit("courier_target_rollover_failed", ok=False, phase="target_rollover",
             detail=str(exc), error_code="COURIER_TARGET_ROLLOVER_FAILED",
             retry_allowed=False)
        return 1


def capture_latest_command(args: argparse.Namespace) -> int:
    """Capture the latest completed assistant turn without sending anything."""
    try:
        request = load_request(args.request_directory)
        ensure_target_binding(request)
        owner = read_owner()
        if owner is not None and (process_alive(owner.owner_pid)
                                  or (owner.browser_pid and process_alive(owner.browser_pid))):
            probe = save_latest_probe(
                request, user_turn_found=False, reply_found=False, live_owner_found=True,
            )
            emit("courier_capture_latest_busy", ok=False, phase="capture_latest",
                 error_code="COURIER_BROWSER_BUSY", retry_allowed=True,
                 safe_next_action="courier_capture_latest", project_id=request.project_id,
                 request_id=request.request_id, fingerprint=probe["fingerprint"],
                 captured_at=probe["captured_at"], latest_user_turn_found=False,
                 post_submission_reply_found=False, live_owner_found=True,
                 submission_count=probe["submission_count"], message_sent=False)
            return 1
        # Read-only capture must be able to inspect a request while Chat is
        # still generating. Composer readiness is a send gate, not a read
        # prerequisite; wait_for_reply records the streaming/busy evidence.
        with ChatSession(request, recovery=True, require_composer=False) as session:
            candidate = session.wait_for_reply(
                None, time.monotonic() + min(int(getattr(args, "timeout", 60)), request.workflow_window_seconds),
                after_user_marker=f"REQUEST_ID={request.request_id}",
            )
            if candidate is None:
                diagnostic_path = request.directory / "response-diagnostic.json"
                try: diagnostic = json.loads(diagnostic_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError): diagnostic = {}
                probe = save_latest_probe(
                    request, user_turn_found=bool(diagnostic.get("anchor_found")),
                    reply_found=False, live_owner_found=False,
                )
                if (diagnostic.get("streaming") is False
                        and diagnostic.get("composer_ready") is True
                        and not diagnostic.get("anchor_found")):
                    record_absence_observation(
                        request, session_id=session.session_id, streaming=False,
                        composer_ready=True, anchor_found=False,
                    )
                emit("courier_capture_latest_empty", ok=False, phase="capture_latest",
                      error_code="LATEST_ASSISTANT_REPLY_NOT_FOUND", retry_allowed=True,
                      safe_next_action="courier_retry_once" if not probe["latest_user_turn_found"] else "courier_recover",
                      project_id=request.project_id, request_id=request.request_id,
                      fingerprint=probe["fingerprint"], captured_at=probe["captured_at"],
                      latest_user_turn_found=probe["latest_user_turn_found"],
                      post_submission_reply_found=False, live_owner_found=False,
                      submission_count=probe["submission_count"], message_sent=False)
                return 1
            capture = save_latest_response_capture(
                request, identity=candidate.identity, index=candidate.index, text=candidate.text,
                user_turn_found=True,
            )
    except (ValidationError, OwnerBusy, BrowserError) as exc:
        emit("courier_capture_latest_failed", ok=False, phase="capture_latest",
             error_code="LATEST_CAPTURE_FAILED", retry_allowed=True,
             safe_next_action="courier_capture_latest", detail=str(exc))
        return 1

    # The browser context is closed before any content interpretation occurs.
    envelope_present = REPLY_PROTOCOL in candidate.text
    request_match: bool | None = None
    protocol_detail: str | None = None
    accepted_body: str | None = None
    if envelope_present:
        try:
            accepted_body = parse_reply(candidate.text, request).body
        except ValidationError as exc:
            request_match = False; protocol_detail = str(exc)
        else:
            request_match = True
    values = {
        "raw_path": str(request.directory / capture["raw_path"]),
        "raw_sha256": capture["raw_sha256"], "assistant_identity": capture["assistant_identity"],
        "envelope_present": envelope_present, "request_match": request_match,
        "protocol_detail": protocol_detail, "message_sent": False,
        "latest_user_turn_found": capture["latest_user_turn_found"],
        "post_submission_reply_found": capture["post_submission_reply_found"],
        "submission_count": submission_count(request),
    }
    save_latest_probe(request, user_turn_found=True, reply_found=True, live_owner_found=False)
    if request_match and accepted_body is not None:
        try:
            queue = CourierQueue(request)
            queue.join(allow_active_recovery=True)
            queue.complete()
            response_path = save_response(request, accepted_body)
            receipt(request, "response_received", "Accepted an exact reply found by read-only recovery",
                    response_path=str(response_path))
            event(request, "latest_response_adopted", phase="capture_latest",
                  response_path=str(response_path), raw_sha256=capture["raw_sha256"])
            values["response_path"] = str(response_path)
            values["reconciled"] = True
        except (QueueIntegrityError, RuntimeError, OSError) as exc:
            emit("courier_capture_latest_failed", ok=False, phase="capture_latest",
                 error_code="LATEST_CAPTURE_RECONCILIATION_FAILED", retry_allowed=True,
                 safe_next_action="courier_capture_latest", detail=str(exc),
                 project_id=request.project_id, request_id=request.request_id)
            return 1
    event(request, "latest_response_captured", phase="capture_latest", **values)
    emit("courier_latest_response_captured", ok=True, phase="capture_latest",
         project_id=request.project_id, request_id=request.request_id, **values)
    return 0


def _latest_conflicting_envelope(request) -> dict[str, object] | None:
    """Return a proven post-request reply envelope that names another request."""
    manifest_path = request.directory / "latest-response-capture.json"
    raw_path = request.directory / "latest-response.raw.txt"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        text = raw_path.read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError):
        manifest = None
    if (isinstance(manifest, dict)
            and manifest.get("project_id") == request.project_id
            and manifest.get("request_id") == request.request_id
            and manifest.get("payload_fingerprint") == request.payload_fingerprint
            and manifest.get("latest_user_turn_found") is True
            and manifest.get("post_submission_reply_found") is True
            and manifest.get("raw_path") == raw_path.name
            and manifest.get("raw_sha256") == hashlib.sha256(text.encode("utf-8")).hexdigest()
            and REPLY_PROTOCOL in text):
        request_ids = sorted(set(re.findall(
            r"REQUEST_ID=([A-Za-z0-9][A-Za-z0-9._:-]{0,127})", text,
        )))
        if request_ids and request.request_id not in request_ids:
            return {
                "assistant_identity": manifest.get("assistant_identity"),
                "raw_sha256": manifest.get("raw_sha256"),
                "conflicting_request_ids": request_ids,
                "evidence_source": "latest_response_capture",
            }

    # A rejected identity is intentionally omitted from later capture results,
    # but the append-only conversation ledger still retains the observed fact.
    # Use it only when the current request anchor and conflicting assistant turn
    # are the final two conversation turns in an idle, ready Chat.
    ledger_path = request.directory / "conversation-ledger.json"
    try:
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    messages = ledger.get("messages", []) if isinstance(ledger, dict) else []
    if (ledger.get("project_id") != request.project_id
            or ledger.get("request_id") != request.request_id
            or ledger.get("payload_fingerprint") != request.payload_fingerprint
            or ledger.get("streaming") is not False
            or ledger.get("composer_ready") is not True
            or not isinstance(messages, list)
            or len(messages) < 2):
        return None
    user, assistant = messages[-2], messages[-1]
    if (not isinstance(user, dict) or not isinstance(assistant, dict)
            or user.get("role") != "user"
            or request.request_id not in user.get("request_ids", [])
            or assistant.get("role") != "assistant"):
        return None
    text = str(assistant.get("text", ""))
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    request_ids = sorted(set(re.findall(
        r"REQUEST_ID=([A-Za-z0-9][A-Za-z0-9._:-]{0,127})", text,
    )))
    if (REPLY_PROTOCOL not in text or not request_ids
            or request.request_id in request_ids
            or assistant.get("text_sha256") != digest):
        return None
    return {
        "assistant_identity": assistant.get("identity"),
        "raw_sha256": digest,
        "conflicting_request_ids": request_ids,
        "evidence_source": "conversation_ledger",
    }


def _regenerate_conflicting_envelope_once(request, conflict: dict[str, object]) -> int:
    """Repair one terminal wrong-ID reply with one bounded Chat-side action."""
    prior = [item for item in request_events(request)
             if item.get("event") in {"response_regeneration_click_intent",
                                      "conflicting_reply_recovery_resend_intent"}]
    if prior:
        receipt(request, "request_frozen",
                "The one page-native response regeneration was already attempted or became uncertain",
                agent_action_required=True, safe_next_action="notify_supervisor")
        event(request, "response_regeneration_refused", phase="reconcile",
              reason="regeneration_budget_consumed")
        emit("response_regeneration_refused", ok=False, phase="reconcile",
             project_id=request.project_id, request_id=request.request_id,
             retry_allowed=False, reason="regeneration_budget_consumed")
        return 1

    archive_response_capture(request, max(1, evidence_retry_count(request) + 1))
    intent = {
        "assistant_identity": conflict.get("assistant_identity"),
        "raw_sha256": conflict.get("raw_sha256"),
        "conflicting_request_ids": conflict.get("conflicting_request_ids"),
        "same_user_request_preserved": True,
        "maximum_attempts": 1,
    }
    event(request, "response_regeneration_intent", phase="reconcile", **intent)
    receipt(request, "response_regeneration_intent",
            "A conflicting reply envelope will be regenerated once without resending the user request",
            **intent)
    emit("response_regeneration_intent", ok=True, phase="reconcile",
         project_id=request.project_id, request_id=request.request_id, **intent)
    try:
        with ChatSession(request, recovery=True, require_composer=False) as session:
            dom = ChatDom(session.page)
            if dom.interrupted_generation():
                raise BrowserError("the visible interrupted generation must be stopped before recovery")
            def before_click(values: dict[str, object]) -> None:
                event(request, "response_regeneration_click_intent", phase="reconcile", **values)
                receipt(request, "response_regeneration_click_intent",
                        "Courier is about to click one page-native regenerate control", **values)
            action = dom.regenerate_conflicting_reply(
                f"REQUEST_ID={request.request_id}", request.request_id,
                before_click=before_click,
            )
    except (ValidationError, OwnerBusy, BrowserError) as exc:
        # If no native action crossed its click boundary, use one explicit
        # recovery turn.  It repeats the same logical request ID, labels the
        # stale envelope, and never mutates the immutable request directory.
        clicked = any(item.get("event") == "response_regeneration_click_intent"
                      for item in request_events(request))
        native_control_unavailable = (
            isinstance(exc, BrowserError)
            and str(exc).startswith("no unambiguous page-native regenerate control")
        )
        if native_control_unavailable and not clicked:
            recovery_prompt = (
                "CHAT_COURIER_RECOVERY_NOTICE/1\n"
                f"PROJECT_ID={request.project_id}\n"
                f"REQUEST_ID={request.request_id}\n"
                "The immediately preceding assistant reply used a stale REQUEST_ID and was rejected. "
                "This is the same logical request, not a new work order. Ignore that stale reply and "
                "answer the exact request below using the required current REQUEST_ID envelope.\n\n"
                + build_prompt(request)
            )
            recovery_values = {
                "conflicting_request_ids": conflict.get("conflicting_request_ids"),
                "same_logical_request": True,
                "maximum_attempts": 1,
            }
            event(request, "conflicting_reply_recovery_resend_intent",
                  phase="reconcile", **recovery_values)
            receipt(request, "conflicting_reply_recovery_resend_intent",
                    "Courier will send one labeled recovery turn for the same logical request",
                    **recovery_values)
            emit("conflicting_reply_recovery_resend_intent", ok=True, phase="reconcile",
                 project_id=request.project_id, request_id=request.request_id,
                 **recovery_values)
            try:
                with ChatSession(request, recovery=True) as session:
                    session.submit(recovery_prompt, ())
            except (ValidationError, OwnerBusy, BrowserError) as resend_exc:
                receipt(request, "request_frozen",
                        f"The one labeled recovery turn could not be proven submitted: {resend_exc}",
                        agent_action_required=True, safe_next_action="notify_supervisor")
                event(request, "conflicting_reply_recovery_resend_failed",
                      phase="reconcile", detail=str(resend_exc))
                emit("conflicting_reply_recovery_resend_failed", ok=False, phase="reconcile",
                     project_id=request.project_id, request_id=request.request_id,
                     retry_allowed=False, detail=str(resend_exc))
                return 1
            event(request, "conflicting_reply_recovery_resend_submitted",
                  phase="reconcile", **recovery_values)
            receipt(request, "waiting_for_response",
                    "One labeled recovery turn was submitted for the same logical request",
                    **recovery_values)
            emit("conflicting_reply_recovery_resend_submitted", ok=True, phase="reconcile",
                 project_id=request.project_id, request_id=request.request_id,
                 **recovery_values)
            return run_command(argparse.Namespace(request_directory=str(request.directory)))
        receipt(request, "request_frozen",
                f"The single page-native regeneration could not be proven safe or complete: {exc}",
                agent_action_required=True, safe_next_action="notify_supervisor")
        event(request, "response_regeneration_failed", phase="reconcile", detail=str(exc))
        emit("response_regeneration_failed", ok=False, phase="reconcile",
             project_id=request.project_id, request_id=request.request_id,
             retry_allowed=False, detail=str(exc))
        return 1
    event(request, "response_regeneration_started", phase="reconcile", **action)
    receipt(request, "waiting_for_response",
            "ChatGPT started one page-native regeneration for the same immutable user request",
            **action)
    emit("response_regeneration_started", ok=True, phase="reconcile",
         project_id=request.project_id, request_id=request.request_id, **action)
    return run_command(argparse.Namespace(request_directory=str(request.directory)))

def _regenerate_interrupted_response_once(request) -> int:
    """Use ChatGPT's native Retry once for a terminal connection-error card."""
    events = request_events(request)
    if any(item.get("event") == "response_ui_retry_click_intent" for item in events):
        return 1
    last_unconfirmed = max(
        (index for index, item in enumerate(events)
         if item.get("event") == "interrupted_reply_recovery_submission_unconfirmed"),
        default=-1,
    )
    last_recovery_submitted = max(
        (index for index, item in enumerate(events)
         if item.get("event") == "interrupted_reply_recovery_resend_submitted"),
        default=-1,
    )
    if last_unconfirmed > last_recovery_submitted:
        try:
            diagnostic = json.loads(
                (request.directory / "submission_diagnostic.json").read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            diagnostic = {}
        proven_unsent = bool(
            diagnostic.get("composer_contains_marker") is True
            and not diagnostic.get("user_turns_with_marker")
        )
        if not proven_unsent:
            return 1
    try:
        with ChatSession(request, recovery=True) as session:
            def before_click(values: dict[str, object]) -> None:
                event(request, "response_ui_retry_click_intent", phase="reconcile", **values)
                receipt(request, "response_ui_retry_click_intent",
                        "Courier is about to use ChatGPT's native Retry for the interrupted response",
                        **values)
            action = ChatDom(session.page).regenerate_conflicting_reply(
                f"REQUEST_ID={request.request_id}", request.request_id,
                before_click=before_click, allow_ui_error=True,
            )
    except (ValidationError, OwnerBusy, BrowserError) as exc:
        clicked = any(item.get("event") == "response_ui_retry_click_intent"
                      for item in request_events(request))
        native_unavailable = isinstance(exc, BrowserError) and (
            str(exc).startswith("the exact Courier request has no assistant reply")
            or str(exc).startswith("no unambiguous page-native regenerate control")
            or str(exc).startswith("the visible interrupted generation must be stopped")
        )
        if native_unavailable and not clicked:
            if any(item.get("event") == "interrupted_reply_recovery_resend_submitted"
                   for item in request_events(request)):
                return 1
            recovery_prompt = (
                "CHAT_COURIER_RECOVERY_NOTICE/1\n"
                f"PROJECT_ID={request.project_id}\n"
                f"REQUEST_ID={request.request_id}\n"
                "The response generation for this same logical request was interrupted, or its "
                "original turn is not visible in the currently loaded conversation branch. This is "
                "not a new work order. Answer the exact immutable request below once, using the same "
                "REQUEST_ID envelope.\n\n" + build_prompt(request)
            )
            values = {"same_logical_request": True, "maximum_attempts": 1}
            event(request, "interrupted_reply_recovery_resend_intent",
                  phase="reconcile", **values)
            receipt(request, "interrupted_reply_recovery_resend_intent",
                    "Courier will send one labeled same-request recovery turn", **values)
            emit("interrupted_reply_recovery_resend_intent", ok=True, phase="reconcile",
                 project_id=request.project_id, request_id=request.request_id, **values)
            try:
                contention_reconnects = 0
                rate_limit_reconnects = 0
                while True:
                    try:
                        with ChatSession(request, recovery=True, require_composer=False) as session:
                            dom = ChatDom(session.page)
                            if dom.interrupted_generation():
                                def before_stop(stop_values: dict[str, object]) -> None:
                                    event(request, "interrupted_generation_stop_intent",
                                          phase="reconcile", **stop_values)
                                    receipt(request, "interrupted_generation_stop_intent",
                                            "Courier is about to stop the known connection-interrupted generation",
                                            **stop_values)
                                stopped = dom.stop_interrupted_generation(before_click=before_stop)
                                event(request, "interrupted_generation_stopped",
                                      phase="reconcile", **stopped)
                            session.submit(recovery_prompt, request.attachments)
                        break
                    except SubmissionUnconfirmed as uncertain_exc:
                        event(request, "interrupted_reply_recovery_submission_unconfirmed",
                              phase="reconcile", detail=str(uncertain_exc))
                        raise
                    except (ChatComposerNotReady, PreSubmissionError) as busy_exc:
                        snapshot = _chat_contention_snapshot(busy_exc)
                        rate_limited = bool(snapshot and snapshot.get("rate_limited") is True)
                        if snapshot is None:
                            raise
                        if rate_limited:
                            if rate_limit_reconnects >= 1:
                                raise
                            rate_limit_reconnects += 1
                        else:
                            if contention_reconnects >= CHAT_CONTENTION_RECONNECT_ATTEMPTS:
                                raise
                            contention_reconnects += 1
                        _wait_for_shared_chat(
                            request, busy_exc,
                            1 if rate_limited else contention_reconnects,
                        )
            except (ValidationError, OwnerBusy, BrowserError) as resend_exc:
                event(request, "interrupted_reply_recovery_resend_failed",
                      phase="reconcile", detail=str(resend_exc))
                emit("interrupted_reply_recovery_resend_failed", ok=False,
                     phase="reconcile", project_id=request.project_id,
                     request_id=request.request_id, retry_allowed=False,
                     detail=str(resend_exc))
                return 1
            event(request, "interrupted_reply_recovery_resend_submitted",
                  phase="reconcile", **values)
            receipt(request, "waiting_for_response",
                    "One labeled recovery turn was submitted for the same logical request",
                    **values)
            emit("interrupted_reply_recovery_resend_submitted", ok=True,
                 phase="reconcile", project_id=request.project_id,
                 request_id=request.request_id, **values)
            return run_command(argparse.Namespace(request_directory=str(request.directory)))
        event(request, "response_ui_retry_unavailable", phase="reconcile", detail=str(exc))
        emit("response_ui_retry_unavailable", ok=False, phase="reconcile",
             project_id=request.project_id, request_id=request.request_id,
             retry_allowed=False, detail=str(exc))
        return 1
    event(request, "response_ui_retry_started", phase="reconcile", **action)
    receipt(request, "waiting_for_response",
            "ChatGPT started one page-native Retry for the interrupted response", **action)
    emit("response_ui_retry_started", ok=True, phase="reconcile",
         project_id=request.project_id, request_id=request.request_id, **action)
    return run_command(argparse.Namespace(request_directory=str(request.directory)))

def reconcile_command(args: argparse.Namespace) -> int:
    """Converge one immutable request from durable transport facts."""
    try:
        request = load_request(args.request_directory)
        binding = ensure_target_binding(request)
        current = load_receipt(request)
    except ValidationError as exc:
        emit("courier_reconcile_failed", ok=False, phase="reconcile", detail=str(exc))
        return 2
    state = current.get("state") if current else "prepared"
    event(request, "reconcile_started", phase="reconcile", state=state,
          target_generation=binding["generation"])
    accepted_ui_error = False
    if state == "response_received":
        response_path = request.directory / "response.txt"
        try:
            accepted_text = response_path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            emit("courier_reconcile_failed", ok=False, phase="reconcile",
                 project_id=request.project_id, request_id=request.request_id,
                 detail=f"saved response is unreadable: {exc}")
            return 1
        if not is_chat_ui_error(accepted_text):
            emit("response_duplicate", ok=True, phase="complete",
                 project_id=request.project_id, request_id=request.request_id,
                 response_path=str(response_path))
            return 0
        accepted_ui_error = True
        archived = request.directory / "rejected-response-ui-error.txt"
        if archived.exists():
            archived = request.directory / f"rejected-response-ui-error-{int(time.time())}.txt"
        response_path.replace(archived)
        receipt(request, "response_ui_error",
                "A previously accepted response was reclassified as a Chat UI error",
                archived_response_path=str(archived))
        event(request, "accepted_ui_error_reclassified", phase="reconcile",
              archived_response_path=str(archived))
        state = "response_ui_error"

    events = request_events(request)
    last_reclassified = max(
        (index for index, item in enumerate(events)
         if item.get("event") == "accepted_ui_error_reclassified"),
        default=-1,
    )
    last_native_retry = max(
        (index for index, item in enumerate(events)
         if item.get("event") == "response_ui_retry_click_intent"),
        default=-1,
    )

    # A rejected capture is an observation, not a permanent input. Preserve it
    # and re-read the conversation before making another protocol decision.
    interrupted_ui_error = accepted_ui_error or last_reclassified > last_native_retry
    if state == "response_ui_error":
        loaded = load_response_capture(request)
        interrupted_ui_error = interrupted_ui_error or bool(
            loaded is not None and is_chat_ui_error(loaded[1])
        )
    if state in {"response_protocol_error", "response_ui_error"}:
        archive_response_capture(request, max(1, evidence_retry_count(request) + 1))
        event(request, "rejected_capture_archived", phase="reconcile", prior_state=state)
    if interrupted_ui_error:
        recovered = _regenerate_interrupted_response_once(request)
        if recovered == 0:
            return 0

    sent = request_was_submitted(request)
    last_rollover = max((index for index, item in enumerate(events)
                         if item.get("event") == "target_rollover_authorized"), default=-1)
    uncertain = any(item.get("event") in {"chat_submission_unconfirmed", "submission_unconfirmed"}
                    for item in events[last_rollover + 1:])
    if (not sent and not uncertain
            and state in {"submission_intent", "browser_error", "courier_error",
                          "queue_recovery_required", "submission_not_started", "queue_timeout"}):
        event(request, "reconcile_proven_unsent", phase="reconcile", prior_state=state)
        return run_command(argparse.Namespace(request_directory=args.request_directory))

    externally_possible = state in {
        "submission_intent", "request_submitted", "waiting_for_response",
        "submission_unconfirmed", "response_timeout", "response_protocol_error",
        "response_ui_error", "queue_recovery_required", "browser_error", "courier_error",
    } or sent
    if externally_possible:
        captured = capture_latest_command(argparse.Namespace(request_directory=args.request_directory, timeout=10))
        try: refreshed = load_receipt(request)
        except ValidationError: refreshed = None
        if refreshed and refreshed.get("state") == "response_received":
            return 0

        conflict = _latest_conflicting_envelope(request)
        if conflict is not None:
            return _regenerate_conflicting_envelope_once(request, conflict)

        if state == "submission_unconfirmed" and captured != 0:
            path = request.directory / "absence-observations.json"
            def valid_absences() -> list[dict[str, object]]:
                try: observations = json.loads(path.read_text(encoding="utf-8")).get("observations", [])
                except (OSError, json.JSONDecodeError, AttributeError): observations = []
                return [item for item in observations if isinstance(item, dict)
                        and item.get("payload_fingerprint") == request.payload_fingerprint
                        and item.get("chat_url") == request.chat_url
                        and item.get("streaming") is False
                        and item.get("composer_ready") is True
                        and item.get("anchor_found") is False]
            valid = valid_absences()
            if valid and (len(valid) < 2
                          or valid[-1].get("session_id") == valid[-2].get("session_id")
                          or float(valid[-1].get("observed_at", 0)) - float(valid[-2].get("observed_at", 0)) < 30):
                delay = max(0.0, 30.0 - (time.time() - float(valid[-1].get("observed_at", 0))))
                if delay: time.sleep(delay)
                second = capture_latest_command(argparse.Namespace(request_directory=args.request_directory, timeout=10))
                if second == 0:
                    return 0
                valid = valid_absences()
            distinct = len(valid) >= 2 and valid[-1].get("session_id") != valid[-2].get("session_id")
            separated = distinct and float(valid[-1].get("observed_at", 0)) - float(valid[-2].get("observed_at", 0)) >= 30
            already = any(item.get("event") == "uncertain_auto_resend_authorized"
                          for item in request_events(request))
            if separated and not already:
                event(request, "uncertain_auto_resend_authorized", phase="reconcile",
                      first_observed_at=valid[-2]["observed_at"],
                      second_observed_at=valid[-1]["observed_at"])
                resend_args = argparse.Namespace(request_directory=args.request_directory,
                                                 resend_once=True, evidence_retry=True,
                                                 use_retry_message=False)
                result = run_command(resend_args)
                latest = load_receipt(request)
                if latest and latest.get("state") == "submission_unconfirmed":
                    receipt(request, "request_frozen",
                            "One evidence-based automatic resend remains uncertain; Supervisor review is required",
                            agent_action_required=True, safe_next_action="notify_supervisor")
                    event(request, "request_frozen", phase="reconcile",
                          reason="automatic_resend_uncertain")
                return result
            if already:
                receipt(request, "request_frozen",
                        "Automatic uncertainty retry was already consumed; Supervisor review is required",
                        agent_action_required=True, safe_next_action="notify_supervisor")
                event(request, "request_frozen", phase="reconcile",
                      reason="automatic_resend_already_consumed")
            return captured

        # Confirmed sends stay read-only; continue the same wait after the
        # quick ledger search instead of constructing a replacement request.
        if state in {"request_submitted", "waiting_for_response", "response_timeout",
                     "response_protocol_error", "response_ui_error"} or request_was_submitted(request):
            return run_command(argparse.Namespace(request_directory=args.request_directory))
        return captured
    return run_command(argparse.Namespace(request_directory=args.request_directory))

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
    quiescence = sub.add_parser("courier_quiescence", help="read-only handoff safety check")
    quiescence.set_defaults(handler=quiescence_command)
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
    dispatch = sub.add_parser("courier_dispatch", help="dispatch or mechanically reconcile one request")
    dispatch.add_argument("request_directory"); dispatch.set_defaults(handler=reconcile_command)
    typed_status = sub.add_parser("courier_status", help="read one request state")
    typed_status.add_argument("request_directory"); typed_status.set_defaults(handler=status_command)
    typed_wait = sub.add_parser("courier_wait", help="wait without dispatching")
    typed_wait.add_argument("request_directory"); typed_wait.add_argument("--timeout", type=int, default=30)
    typed_wait.set_defaults(handler=wait_command)
    typed_recover = sub.add_parser("courier_recover", help="recover an already submitted request")
    typed_recover.add_argument("request_directory"); typed_recover.set_defaults(handler=recover_command)
    reconcile = sub.add_parser("courier_reconcile", help="reconcile one request from durable transport facts")
    reconcile.add_argument("request_directory"); reconcile.set_defaults(handler=reconcile_command)
    resend_once = sub.add_parser("courier_resend_once", help="resend the same immutable request once")
    resend_once.add_argument("request_directory"); resend_once.set_defaults(handler=resend_once_command)
    retry_once = sub.add_parser("courier_retry_once", help="retry one apparently unsent immutable request after read-only proof")
    retry_once.add_argument("request_directory"); retry_once.set_defaults(handler=retry_once_command)
    rollover = sub.add_parser("courier_rollover_target", help="send an authorized handoff request to a same-Project successor")
    rollover.add_argument("request_directory")
    rollover.add_argument("--basis", choices=["verified_context_capacity", "user_direct"],
                          default="verified_context_capacity")
    rollover.add_argument("--prepared-retry-authorized", action="store_true",
                          help="retry one prepared rollover after read-only same-Project absence proof")
    rollover.set_defaults(handler=rollover_target_command)
    capture_latest = sub.add_parser("courier_capture_latest", help="capture the latest completed assistant reply without sending")
    capture_latest.add_argument("request_directory"); capture_latest.set_defaults(handler=capture_latest_command)
    args = parser.parse_args(argv)
    return args.handler(args)

if __name__ == "__main__": raise SystemExit(main())
